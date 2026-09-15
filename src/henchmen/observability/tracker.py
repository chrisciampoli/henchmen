"""TaskTracker — persists task execution telemetry to a DocumentStore.

Concurrency notes:
    Counter-style fields (``total_input_tokens``, ``estimated_cost_usd``,
    ``recovery_attempts``, ``ci_fix_attempts``, ...) are incremented
    through the ``DocumentStore.increment`` primitive, which every
    provider implements using a server-side atomic operation:
    Firestore ``Increment`` transforms, DynamoDB ``UpdateExpression``
    ``ADD``, and a per-doc asyncio lock in SQLite. This eliminates both
    intra-process and cross-process races between concurrent
    invocations (Pub/Sub at-least-once delivery, watchdog
    double-publish) — no more read-modify-write clobbering.

    Non-counter structured fields (``node_metrics`` dict,
    ``nodes_executed`` list, ``files_changed`` list) still require a
    merged update. We hold a per-doc ``asyncio.Lock`` only around that
    portion of ``record_node_result`` to serialize in-process writers;
    cross-process races on those fields remain possible but are bounded
    in impact (at worst we lose a file from ``files_changed``, not a
    counter value).
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from henchmen.models.operative import OperativeReport
from henchmen.models.task import HenchmenTask
from henchmen.providers.interfaces.document_store import DocumentStore
from henchmen.providers.pricing import estimate_cost_for_settings, lookup_price
from henchmen.providers.tiers import active_llm_provider, resolve_model_name

if TYPE_CHECKING:
    from henchmen.config.settings import Settings
    from henchmen.models.evaluation import EvaluationResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def _resolved_settings(settings: "Settings | None" = None) -> "Settings | None":
    """The Settings to price against, or ``None`` when they cannot be built.

    Observability must never raise, and ``Settings()`` can legitimately fail
    (e.g. ``HENCHMEN_PROVIDER=gcp`` with no project id), so callers degrade to
    provider-agnostic pricing rather than blowing up a task.
    """
    if settings is not None:
        return settings
    try:
        from henchmen.config.settings import get_settings

        return get_settings()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Cost estimation could not load Settings: %s", exc)
        return None


def estimate_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    settings: "Settings | None" = None,
) -> float:
    """Estimate USD cost for a model call. Returns 0.0 for unpriced models.

    Prices come from :mod:`henchmen.providers.pricing`, the single source of
    truth. Tier names (``default/complex``, ``default/light``,
    ``default/reasoning``) are resolved through the *configured* provider, so a
    Gemini deployment is never priced at Anthropic rates and a free Ollama run
    costs 0.0 — which is what lets the guardrails fall back to their wall-clock
    ceiling.

    ``input_tokens`` is the TOTAL prompt size including ``cached_input_tokens``
    and ``cache_write_tokens``; those are re-priced at the provider's cache
    rates rather than the full input rate.
    """
    active = _resolved_settings(settings)
    if active is None:
        from henchmen.providers.pricing import estimate_cost as _price_only

        return _price_only(model_name, input_tokens, output_tokens, cached_input_tokens, cache_write_tokens)

    concrete = resolve_model_name(active, model_name)
    if lookup_price(concrete) is None:
        # Local models are free by design, so an unpriced name there is expected.
        if (input_tokens > 0 or output_tokens > 0) and active_llm_provider(active) != "local":
            logger.warning("Unknown model for cost estimation: %s (resolved to %s)", model_name, concrete)
        return 0.0
    return estimate_cost_for_settings(
        active,
        model_name,
        input_tokens,
        output_tokens,
        cached_input_tokens,
        cache_write_tokens,
    )


# ---------------------------------------------------------------------------
# TaskTracker
# ---------------------------------------------------------------------------

_COLLECTION = "task_executions"
_RETENTION_DAYS = 30

# Final statuses that mean "the task did what it was asked to do". The
# executor reports ``pr_created`` on the happy path and ``completed`` when a
# scheme finished without opening a PR; both count as success everywhere.
SUCCESS_STATUSES: frozenset[str] = frozenset({"completed", "pr_created"})

# ``execution_state`` values the stalled-task watchdog must ignore. The
# watchdog only looks for ``running``, so every terminal transition has to move
# the task out of that state or a finished task keeps getting "recovered".
EXECUTION_STATE_COMPLETED = "completed"
EXECUTION_STATE_ESCALATED = "escalated"

# Terminal: Mastermind will never resume, heartbeat, or otherwise touch a task
# once its execution_state reaches one of these. Anything that would write
# onto a task document on behalf of an operative (e.g. the desktop-only
# internal task routes) must treat one of these as final rather than
# reviving a finished task -- reuse this set instead of hardcoding a new list.
TERMINAL_EXECUTION_STATES: frozenset[str] = frozenset({EXECUTION_STATE_COMPLETED, EXECUTION_STATE_ESCALATED})


class TaskTracker:
    """Persists task execution telemetry to a DocumentStore.

    All methods silently catch exceptions — observability must never block task
    execution. The DocumentStore is injected; when it is omitted the configured
    one is built from ``ProviderRegistry``, so the tracker never reaches for a
    provider-specific client of its own.

    Timestamps are stored as ISO-8601 UTC strings rather than native datetimes.
    Aware ISO-8601 strings sort lexicographically in the same order as the
    instants they denote, so range filters (``last_heartbeat <``,
    ``created_at >=``, ``expires_at <``) work identically on Firestore,
    DynamoDB and the local SQLite store — the SQLite store JSON-encodes
    datetimes on write and would otherwise raise ``TypeError`` comparing a
    string column against a ``datetime`` filter value.
    """

    def __init__(self, settings: "Settings", document_store: DocumentStore | None = None) -> None:
        # Per-document asyncio locks serialize the non-counter portion of
        # ``record_node_result`` (node_metrics dict, files_changed list).
        # Counter-style increments go through ``DocumentStore.increment``
        # and are atomic across replicas, so the lock is only needed for
        # the structured-merge branch.
        self._doc_locks: dict[str, asyncio.Lock] = {}
        self._settings = settings
        if document_store is None:
            # Build the configured store rather than hard-coding Firestore:
            # the old fallback imported google.cloud.firestore on every
            # provider and silently degraded to a no-op store on failure,
            # which made telemetry vanish without an error.
            from henchmen.providers.registry import ProviderRegistry

            document_store = ProviderRegistry(settings).get_document_store()
        self._store = document_store

    def _get_lock(self, doc_id: str) -> asyncio.Lock:
        """Return the per-document asyncio.Lock, creating it on first use.

        The dict grows unbounded over the lifetime of the process, but the
        entries are tiny and the doc_id space is bounded by active task
        volume, so this is acceptable for the in-process scope of the K4
        mitigation.
        """
        lock = self._doc_locks.get(doc_id)
        if lock is None:
            lock = asyncio.Lock()
            self._doc_locks[doc_id] = lock
        return lock

    async def start_task(self, task: HenchmenTask, scheme_id: str) -> None:
        """Create the initial task execution document.

        Persists the full task payload so ``resume_task()`` can reconstruct
        the HenchmenTask without requiring the original Pub/Sub message.
        """
        try:
            now = datetime.now(UTC)
            doc: dict[str, Any] = {
                "task_id": task.id,
                "title": task.title,
                "source": task.source.value,
                "scheme_id": scheme_id,
                "task_payload": task.model_dump(mode="json"),
                "created_at": now.isoformat(),
                "completed_at": None,
                "final_status": None,
                "pr_url": None,
                "pr_number": None,
                "ci_passed": None,
                "nodes_executed": [],
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_model_calls": 0,
                "total_tool_calls": 0,
                "estimated_cost_usd": 0.0,
                "wall_clock_seconds": 0.0,
                "node_metrics": {},
                "rag_chunks_retrieved": 0,
                "files_changed": [],
                "confidence_score": 0.0,
                "expires_at": (now + timedelta(days=_RETENTION_DAYS)).isoformat(),
                "ci_fix_attempts": 0,
                "ci_fix_in_progress": False,
                "execution_state": "running",
                "current_node_id": None,
                "last_heartbeat": now.isoformat(),
                "recovery_attempts": 0,
                "escalation_reason": None,
                "escalation_node": None,
            }
            await self._store.set(_COLLECTION, task.id, doc)
            logger.info("Started tracking task %s", task.id)
        except Exception as exc:
            logger.warning("Failed to start tracking task %s: %s", task.id, exc)

    def _node_cost(self, report: OperativeReport, model_name: str) -> float:
        """USD cost of one node, preferring the figure the provider billed.

        Providers price each call with the exact cache read/write split, which
        the report's token counters cannot reproduce (a report has no
        cache-write counter, so re-deriving the cost bills Anthropic cache
        writes at the plain input rate). When the report carries the
        provider-summed ``estimated_cost_usd`` it is persisted as-is so the
        stored cost matches the ceiling the guardrails enforced; otherwise the
        cost is re-estimated from the token counters.
        """
        if report.estimated_cost_usd > 0:
            return report.estimated_cost_usd
        return estimate_cost(
            model_name,
            report.total_input_tokens,
            report.total_output_tokens,
            cached_input_tokens=report.cached_input_tokens,
            settings=self._settings,
        )

    async def record_node_result(self, task_id: str, node_id: str, report: OperativeReport) -> None:
        """Record metrics from an agentic node's OperativeReport.

        Counter fields (tokens, model/tool calls, cost, wall clock) are
        incremented through ``DocumentStore.increment`` so concurrent
        replicas can't clobber each other's additions. Structured
        fields (node_metrics dict, files_changed list) still use a
        merged update guarded by a per-doc asyncio lock.

        This is the *only* writer of ``estimated_cost_usd``.
        ``TaskCostAccumulator`` tracks the same spend in memory inside the
        operative so the task-level ceiling can fire mid-node, but it does not
        persist — otherwise every node's cost would land twice.
        """
        try:
            raw_model = getattr(report, "model_name", "") or ""
            # Reports may still carry a tier name ("default/complex"); record
            # the model that actually ran so cost_by_model and the experiment
            # params name a real model.
            model_name = resolve_model_name(self._settings, raw_model) if raw_model else ""
            cost = self._node_cost(report, model_name)
            node_data = {
                "input_tokens": report.total_input_tokens,
                "output_tokens": report.total_output_tokens,
                "cached_input_tokens": report.cached_input_tokens,
                "model_calls": report.model_calls,
                "tool_calls": report.tool_calls_count,
                "wall_clock_seconds": report.wall_clock_seconds,
                "cost_usd": cost,
                "model_name": model_name,
                "status": report.status.value,
                "confidence_score": report.confidence_score,
                "steps_used": report.steps_used,
                "context_tokens_at_start": report.context_tokens_at_start,
                "context_tokens_at_end": report.context_tokens_at_end,
            }

            # 1) Atomic counter increments — safe under cross-process concurrency.
            increments: dict[str, int | float] = {
                "total_input_tokens": report.total_input_tokens,
                "total_output_tokens": report.total_output_tokens,
                "total_model_calls": report.model_calls,
                "total_tool_calls": report.tool_calls_count,
                "estimated_cost_usd": cost,
                "wall_clock_seconds": report.wall_clock_seconds,
            }
            # Drop zero deltas so providers don't do work for no-ops.
            increments = {k: v for k, v in increments.items() if v}
            if increments:
                await self._store.increment(_COLLECTION, task_id, increments)

            # 2) Structured-field merge — lock only this portion in-process.
            async with self._get_lock(task_id):
                current = await self._store.get(_COLLECTION, task_id) or {}
                nodes_executed = list(current.get("nodes_executed", []))
                if node_id not in nodes_executed:
                    nodes_executed.append(node_id)

                files_changed = list(current.get("files_changed", []))
                if report.files_changed:
                    for f in report.files_changed:
                        if f not in files_changed:
                            files_changed.append(f)

                node_metrics = dict(current.get("node_metrics", {}))
                node_metrics[node_id] = node_data

                update_data: dict[str, Any] = {
                    "node_metrics": node_metrics,
                    "nodes_executed": nodes_executed,
                    "confidence_score": report.confidence_score,
                }
                if report.files_changed:
                    update_data["files_changed"] = files_changed

                await self._store.update(_COLLECTION, task_id, update_data)
            logger.info("Recorded node %s for task %s (cost=$%.3f)", node_id, task_id, cost)
        except Exception as exc:
            logger.warning("Failed to record node %s for task %s: %s", node_id, task_id, exc)

    async def record_rag_chunks(self, task_id: str, count: int) -> None:
        """Atomically set the number of RAG chunks retrieved for a task."""
        try:
            await self._store.increment(_COLLECTION, task_id, {"rag_chunks_retrieved": count})
            logger.info("Recorded %d RAG chunks for task %s", count, task_id)
        except Exception as exc:
            logger.warning("Failed to record RAG chunks for task %s: %s", task_id, exc)

    async def record_evaluation(self, task_id: str, result: "EvaluationResult") -> None:
        """Persist post-operative evaluation scores on the task document.

        Written unconditionally — a result carrying ``evaluation_error`` still
        holds the diff-signal fallback scores, which are the only quality
        signal available when the evaluation API is unreachable.
        """
        try:
            await self._store.update(
                _COLLECTION,
                task_id,
                {
                    "evaluation_scores": {
                        "fulfillment": result.fulfillment_score,
                        "tool_call_valid": result.tool_call_valid_score,
                        "safety": result.safety_score,
                        "overall_quality": result.overall_quality,
                    },
                    "evaluation_error": result.evaluation_error,
                },
            )
            logger.info(
                "Recorded evaluation for task %s: quality=%.2f",
                task_id,
                result.overall_quality,
            )
        except Exception as exc:
            logger.warning("Failed to record evaluation for task %s: %s", task_id, exc)

    async def record_ci_result(self, task_id: str, ci_passed: bool) -> None:
        """Update CI pass/fail status."""
        try:
            await self._store.update(_COLLECTION, task_id, {"ci_passed": ci_passed})
            logger.info("Recorded CI result for task %s: %s", task_id, "passed" if ci_passed else "failed")
        except Exception as exc:
            logger.warning("Failed to record CI result for task %s: %s", task_id, exc)

    async def finalize_task(
        self, task_id: str, final_status: str, pr_url: str | None = None, pr_number: int | None = None
    ) -> None:
        """Mark task as completed with final status.

        Also moves ``execution_state`` out of ``running`` so the stalled-task
        watchdog stops "recovering" tasks that already finished.
        """
        try:
            execution_state = EXECUTION_STATE_ESCALATED if final_status == "escalated" else EXECUTION_STATE_COMPLETED
            await self._store.update(
                _COLLECTION,
                task_id,
                {
                    "completed_at": datetime.now(UTC).isoformat(),
                    "final_status": final_status,
                    "pr_url": pr_url,
                    "pr_number": pr_number,
                    "execution_state": execution_state,
                    "current_node_id": None,
                },
            )
            logger.info("Finalized task %s: %s", task_id, final_status)
        except Exception as exc:
            logger.warning("Failed to finalize task %s: %s", task_id, exc)

    # ------------------------------------------------------------------
    # Durable execution state
    # ------------------------------------------------------------------

    async def update_execution_state(
        self,
        task_id: str,
        current_node_id: str,
        node_results: dict[str, Any],
        retry_counts: dict[str, int],
    ) -> None:
        """Checkpoint durable execution state after each scheme node."""
        try:
            await self._store.update(
                _COLLECTION,
                task_id,
                {
                    "current_node_id": current_node_id,
                    "node_results": node_results,
                    "retry_counts": retry_counts,
                    "execution_state": "running",
                    "last_heartbeat": datetime.now(UTC).isoformat(),
                },
            )
        except Exception as exc:
            logger.warning("Failed to checkpoint task %s: %s", task_id, exc)

    async def update_heartbeat(self, task_id: str) -> None:
        """Refresh the heartbeat timestamp for a running task."""
        try:
            await self._store.update(
                _COLLECTION,
                task_id,
                {"last_heartbeat": datetime.now(UTC).isoformat()},
            )
        except Exception as exc:
            logger.debug("Heartbeat update failed (non-fatal): %s", exc)

    async def mark_stalled(self, task_id: str) -> None:
        """Mark a task as stalled (heartbeat expired)."""
        try:
            await self._store.update(_COLLECTION, task_id, {"execution_state": "stalled"})
            logger.info("Marked task %s as stalled", task_id)
        except Exception as exc:
            logger.warning("Failed to mark task %s as stalled: %s", task_id, exc)

    async def mark_escalated(self, task_id: str, reason: str = "", escalation_node: str | None = None) -> None:
        """Mark a task as escalated (unrecoverable)."""
        try:
            update_data: dict[str, Any] = {
                "execution_state": EXECUTION_STATE_ESCALATED,
                "final_status": "escalated",
                "escalation_reason": reason,
                "completed_at": datetime.now(UTC).isoformat(),
            }
            if escalation_node is not None:
                update_data["escalation_node"] = escalation_node
            await self._store.update(
                _COLLECTION,
                task_id,
                update_data,
            )
            logger.info("Escalated task %s: %s", task_id, reason)
        except Exception as exc:
            logger.warning("Failed to escalate task %s: %s", task_id, exc)

    async def increment_recovery_attempts(self, task_id: str) -> None:
        """Atomically increment the recovery attempt count for a stalled task."""
        try:
            await self._store.increment(_COLLECTION, task_id, {"recovery_attempts": 1})
        except Exception as exc:
            logger.warning("Failed to increment recovery for task %s: %s", task_id, exc)

    async def get_stalled_tasks(self, heartbeat_threshold_minutes: int = 10) -> list[dict[str, Any]]:
        """Find tasks with execution_state='running' whose heartbeat has expired.

        Fail-closed, unlike the other tracker methods: a failed query is logged
        at ERROR and re-raised. On Firestore this query needs a composite index;
        swallowing the error and returning ``[]`` made the watchdog report "0
        stalled" forever while stalled tasks were never recovered.
        """
        cutoff = datetime.now(UTC) - timedelta(minutes=heartbeat_threshold_minutes)
        try:
            return await self._store.query(
                _COLLECTION,
                filters=[
                    ("execution_state", "==", "running"),
                    ("last_heartbeat", "<", cutoff.isoformat()),
                ],
            )
        except Exception as exc:
            logger.error("Failed to query stalled tasks (the watchdog cannot see stalled work): %s", exc)
            raise

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Read a single task execution record."""
        try:
            return await self._store.get(_COLLECTION, task_id)
        except Exception as exc:
            logger.warning("Failed to get task %s: %s", task_id, exc)
            return None

    async def get_recent_tasks(self, days: int = 7) -> list[dict[str, Any]]:
        """Query tasks created within the last N days."""
        try:
            cutoff = datetime.now(UTC) - timedelta(days=days)
            return await self._store.query(
                _COLLECTION,
                filters=[("created_at", ">=", cutoff.isoformat())],
                order_by="created_at",
                order_direction="DESCENDING",
            )
        except Exception as exc:
            logger.warning("Failed to query recent tasks: %s", exc)
            return []

    async def get_metrics_summary(self, days: int = 7) -> dict[str, Any]:
        """Aggregate metrics across recent tasks for cost and quality correlation analysis."""
        try:
            tasks = await self.get_recent_tasks(days)
            if not tasks:
                return {"total_tasks": 0, "days": days}

            total = len(tasks)
            succeeded = sum(1 for t in tasks if str(t.get("final_status") or "").lower() in SUCCESS_STATUSES)
            escalated = sum(1 for t in tasks if t.get("final_status") == "escalated")
            total_cost = sum(t.get("estimated_cost_usd", 0) for t in tasks)
            total_tokens_in = sum(t.get("total_input_tokens", 0) for t in tasks)
            total_tokens_out = sum(t.get("total_output_tokens", 0) for t in tasks)

            # Cost by model — aggregated from per-node metrics stored by record_node_result
            cost_by_model: dict[str, float] = {}
            for t in tasks:
                for _node_id, nm in t.get("node_metrics", {}).items():
                    model = nm.get("model_name") or "unknown"
                    cost_by_model[model] = cost_by_model.get(model, 0.0) + nm.get("cost_usd", 0.0)

            # Escalation reason frequency
            escalation_reasons: dict[str, int] = {}
            for t in tasks:
                reason = t.get("escalation_reason") or ""
                if reason:
                    escalation_reasons[reason] = escalation_reasons.get(reason, 0) + 1

            return {
                "total_tasks": total,
                "success_rate": succeeded / total if total else 0.0,
                "escalation_rate": escalated / total if total else 0.0,
                "avg_cost_usd": total_cost / total if total else 0.0,
                "total_cost_usd": total_cost,
                "total_tokens": {"input": total_tokens_in, "output": total_tokens_out},
                "cost_by_model": cost_by_model,
                "escalation_reasons": escalation_reasons,
                "days": days,
            }
        except Exception as exc:
            logger.warning("Failed to compute metrics summary: %s", exc)
            return {"total_tasks": 0, "error": str(exc), "days": days}

    async def record_ci_fix_attempt(self, task_id: str) -> None:
        """Atomically increment ``ci_fix_attempts`` and mark in-progress."""
        try:
            await self._store.increment(_COLLECTION, task_id, {"ci_fix_attempts": 1})
            await self._store.update(_COLLECTION, task_id, {"ci_fix_in_progress": True})
            logger.info("Recorded CI fix attempt for task %s", task_id)
        except Exception as exc:
            logger.warning("Failed to record CI fix attempt for task %s: %s", task_id, exc)

    async def clear_ci_fix_in_progress(self, task_id: str) -> None:
        """Clear the ci_fix_in_progress flag once a fix attempt completes."""
        try:
            await self._store.update(_COLLECTION, task_id, {"ci_fix_in_progress": False})
            logger.info("Cleared ci_fix_in_progress for task %s", task_id)
        except Exception as exc:
            logger.warning("Failed to clear ci_fix_in_progress for task %s: %s", task_id, exc)

    async def cleanup_expired(self, batch_size: int = 100) -> int:
        """Delete task execution documents past their expires_at TTL.

        Returns the number of documents deleted.
        """
        try:
            now = datetime.now(UTC)
            expired = await self._store.query(
                _COLLECTION,
                filters=[("expires_at", "<", now.isoformat())],
                limit=batch_size,
            )
            deleted = 0
            for doc in expired:
                task_id = doc.get("task_id", "")
                if task_id:
                    await self._store.delete(_COLLECTION, task_id)
                    deleted += 1
            if deleted:
                logger.info("Cleaned up %d expired task documents", deleted)
            return deleted
        except Exception as exc:
            logger.warning("Failed to cleanup expired tasks: %s", exc)
            return 0

    async def cleanup_processed_messages(self, retention_days: int = 7, batch_size: int = 200) -> int:
        """Delete processed message dedup records older than retention_days.

        Dedup markers come in two shapes: ``done`` markers carry
        ``processed_at``, while ``in_flight`` markers written by a handler that
        crashed before committing only carry ``acquired_at``. Filtering on
        ``processed_at`` alone leaks the in-flight ones forever, so both fields
        are swept.

        Returns the number of documents deleted.
        """
        try:
            cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
            stale: dict[str, dict[str, Any]] = {}
            for timestamp_field in ("processed_at", "acquired_at"):
                rows = await self._store.query(
                    "processed_messages",
                    filters=[(timestamp_field, "<", cutoff)],
                    limit=batch_size,
                )
                for row in rows:
                    key = row.get("key", "")
                    if key:
                        stale[key] = row

            deleted = 0
            for key in list(stale)[:batch_size]:
                await self._store.delete("processed_messages", key)
                deleted += 1
            if deleted:
                logger.info("Cleaned up %d old processed messages", deleted)
            return deleted
        except Exception as exc:
            logger.warning("Failed to cleanup processed messages: %s", exc)
            return 0

    async def get_task_by_id_prefix(self, task_id_prefix: str) -> dict[str, Any] | None:
        """Find a task whose ID starts with the given prefix.

        Uses a range query (task_id >= prefix AND task_id < prefix + \\uffff),
        ordered by task_id ascending, limited to 1 result.
        """
        try:
            upper_bound = task_id_prefix + "\uffff"
            results = await self._store.query(
                _COLLECTION,
                filters=[
                    ("task_id", ">=", task_id_prefix),
                    ("task_id", "<", upper_bound),
                ],
                order_by="task_id",
                limit=1,
            )
            return results[0] if results else None
        except Exception as exc:
            logger.warning("Failed to get task by prefix %s: %s", task_id_prefix, exc)
            return None
