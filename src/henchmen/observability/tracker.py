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

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

# Model pricing configuration: (input_price_per_1M, output_price_per_1M) in USD.
# Maintained here as a lookup dict rather than in Settings because these change
# infrequently and adding per-model fields to Settings would be over-engineering.
_PRICE_MAP: dict[str, tuple[float, float]] = {
    "claude-sonnet-4@20250514": (3.0, 15.0),
    "claude-haiku-4-5@20251001": (0.80, 4.0),
    "gemini-3.1-pro": (2.0, 12.0),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.15, 0.60),
}


def estimate_cost(model_name: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
    """Estimate USD cost for a model call. Returns 0.0 for unknown models.

    Cached input tokens are billed at 25% of the standard input rate
    (75% discount via Gemini context caching).
    """
    prices = _PRICE_MAP.get(model_name)
    if not prices:
        if input_tokens > 0 or output_tokens > 0:
            logger.warning("Unknown model for cost estimation: %s", model_name)
        return 0.0
    input_price, output_price = prices
    # Cached tokens are billed at 25% of standard input price
    non_cached = max(0, input_tokens - cached_input_tokens)
    cached_cost = cached_input_tokens * input_price * 0.25 / 1_000_000
    standard_cost = non_cached * input_price / 1_000_000
    output_cost = output_tokens * output_price / 1_000_000
    return standard_cost + cached_cost + output_cost


# ---------------------------------------------------------------------------
# TaskTracker
# ---------------------------------------------------------------------------

_COLLECTION = "task_executions"
_RETENTION_DAYS = 30


class TaskTracker:
    """Persists task execution telemetry to a DocumentStore.

    All methods silently catch exceptions — observability must never block task execution.
    Accepts a DocumentStore via dependency injection; falls back to creating a
    Firestore-backed store when none is provided (legacy compatibility).
    """

    def __init__(self, settings: "Settings", document_store: DocumentStore | None = None) -> None:
        # Per-document asyncio locks serialize the non-counter portion of
        # ``record_node_result`` (node_metrics dict, files_changed list).
        # Counter-style increments go through ``DocumentStore.increment``
        # and are atomic across replicas, so the lock is only needed for
        # the structured-merge branch.
        self._doc_locks: dict[str, asyncio.Lock] = {}

        if document_store is not None:
            self._store = document_store
            # Legacy compatibility attributes — some code paths still reference _db/_collection
            # directly (e.g. server.py dedup check). Those callers must be updated; here we
            # set them to None so AttributeError surfaces clearly instead of silently misbehaving.
            self._db = None
            self._collection = None
        else:
            # Fallback: build a Firestore-backed DocumentStore for backward compatibility.
            # This path is used when TaskTracker is constructed without explicit providers.
            try:
                from google.cloud import firestore

                db = firestore.Client(
                    project=settings.gcp_project_id,
                    database=settings.firestore_database,
                )
                self._db = db
                self._collection = db.collection(_COLLECTION)
                # Wrap the raw Firestore client in a thin adapter so _store works too.
                self._store = _FirestoreLegacyAdapter(db)
            except Exception as exc:
                logger.warning("Failed to initialize Firestore client: %s", exc)
                self._db = None
                self._collection = None
                self._store = _NullDocumentStore()

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
                "created_at": now,
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
                "expires_at": now + timedelta(days=_RETENTION_DAYS),
                "ci_fix_attempts": 0,
                "ci_fix_in_progress": False,
                "execution_state": "running",
                "current_node_id": None,
                "last_heartbeat": now,
                "recovery_attempts": 0,
                "escalation_reason": None,
            }
            await self._store.set(_COLLECTION, task.id, doc)
            logger.info("Started tracking task %s", task.id)
        except Exception as exc:
            logger.warning("Failed to start tracking task %s: %s", task.id, exc)

    async def record_node_result(self, task_id: str, node_id: str, report: OperativeReport) -> None:
        """Record metrics from an agentic node's OperativeReport.

        Counter fields (tokens, model/tool calls, cost, wall clock) are
        incremented through ``DocumentStore.increment`` so concurrent
        replicas can't clobber each other's additions. Structured
        fields (node_metrics dict, files_changed list) still use a
        merged update guarded by a per-doc asyncio lock.
        """
        try:
            model_name = getattr(report, "model_name", "") or ""
            cost = estimate_cost(
                model_name,
                report.total_input_tokens,
                report.total_output_tokens,
            )
            node_data = {
                "input_tokens": report.total_input_tokens,
                "output_tokens": report.total_output_tokens,
                "model_calls": report.model_calls,
                "tool_calls": report.tool_calls_count,
                "wall_clock_seconds": report.wall_clock_seconds,
                "cost_usd": cost,
                "model_name": model_name,
                "status": report.status.value,
                "confidence_score": report.confidence_score,
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
        """Mark task as completed with final status."""
        try:
            await self._store.update(
                _COLLECTION,
                task_id,
                {
                    "completed_at": datetime.now(UTC),
                    "final_status": final_status,
                    "pr_url": pr_url,
                    "pr_number": pr_number,
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
                    "last_heartbeat": datetime.now(UTC),
                },
            )
        except Exception as exc:
            logger.warning("Failed to checkpoint task %s: %s", task_id, exc)

    async def mark_stalled(self, task_id: str) -> None:
        """Mark a task as stalled (heartbeat expired)."""
        try:
            await self._store.update(_COLLECTION, task_id, {"execution_state": "stalled"})
            logger.info("Marked task %s as stalled", task_id)
        except Exception as exc:
            logger.warning("Failed to mark task %s as stalled: %s", task_id, exc)

    async def mark_escalated(self, task_id: str, reason: str = "") -> None:
        """Mark a task as escalated (unrecoverable)."""
        try:
            await self._store.update(
                _COLLECTION,
                task_id,
                {
                    "execution_state": "escalated",
                    "final_status": "escalated",
                    "escalation_reason": reason,
                    "completed_at": datetime.now(UTC),
                },
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
        """Find tasks with execution_state='running' whose heartbeat has expired."""
        try:
            cutoff = datetime.now(UTC) - timedelta(minutes=heartbeat_threshold_minutes)
            return await self._store.query(
                _COLLECTION,
                filters=[
                    ("execution_state", "==", "running"),
                    ("last_heartbeat", "<", cutoff),
                ],
            )
        except Exception as exc:
            logger.warning("Failed to query stalled tasks: %s", exc)
            return []

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
                filters=[("created_at", ">=", cutoff)],
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
            pr_created = sum(1 for t in tasks if t.get("final_status") == "pr_created")
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
                "success_rate": pr_created / total if total else 0.0,
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


# ---------------------------------------------------------------------------
# Legacy compatibility adapters (used when no DocumentStore is injected)
# ---------------------------------------------------------------------------


class _FirestoreLegacyAdapter:
    """Thin async wrapper around a synchronous Firestore Client for the legacy fallback path."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            doc = self._db.collection(collection).document(document_id).get()
            if doc.exists:
                return doc.to_dict() or {}
            return None

        return await asyncio.to_thread(_read)

    async def set(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        await asyncio.to_thread(self._db.collection(collection).document(document_id).set, data)

    async def update(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        await asyncio.to_thread(self._db.collection(collection).document(document_id).update, data)

    async def delete(self, collection: str, document_id: str) -> None:
        await asyncio.to_thread(self._db.collection(collection).document(document_id).delete)

    async def query(
        self,
        collection: str,
        filters: list[tuple[str, str, Any]] | None = None,
        order_by: str | None = None,
        order_direction: str = "ASCENDING",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        from google.cloud import firestore

        def _query() -> list[dict[str, Any]]:
            ref: Any = self._db.collection(collection)
            if filters:
                for field, op, value in filters:
                    ref = ref.where(field, op, value)
            if order_by:
                direction = firestore.Query.DESCENDING if order_direction == "DESCENDING" else firestore.Query.ASCENDING
                ref = ref.order_by(order_by, direction=direction)
            if limit:
                ref = ref.limit(limit)
            return [doc.to_dict() for doc in ref.stream()]

        return await asyncio.to_thread(_query)

    async def increment(self, collection: str, document_id: str, field_deltas: dict[str, int | float]) -> None:
        from google.cloud import firestore

        if not field_deltas:
            return
        payload = {field: firestore.Increment(delta) for field, delta in field_deltas.items()}
        await asyncio.to_thread(self._db.collection(collection).document(document_id).update, payload)

    async def update_if(
        self,
        collection: str,
        document_id: str,
        expected_field: str,
        expected_value: Any,
        new_values: dict[str, Any],
    ) -> bool:
        from google.cloud import firestore

        doc_ref = self._db.collection(collection).document(document_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _txn(txn: Any) -> bool:
            snapshot = doc_ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            current = snapshot.to_dict() or {}
            if current.get(expected_field) != expected_value:
                return False
            txn.update(doc_ref, new_values)
            return True

        result: bool = await asyncio.to_thread(_txn, transaction)
        return result


class _NullDocumentStore:
    """No-op DocumentStore used when Firestore initialization fails."""

    async def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        return None

    async def set(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        pass

    async def update(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        pass

    async def delete(self, collection: str, document_id: str) -> None:
        pass

    async def query(
        self,
        collection: str,
        filters: list[tuple[str, str, Any]] | None = None,
        order_by: str | None = None,
        order_direction: str = "ASCENDING",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return []

    async def increment(self, collection: str, document_id: str, field_deltas: dict[str, int | float]) -> None:
        pass

    async def update_if(
        self,
        collection: str,
        document_id: str,
        expected_field: str,
        expected_value: Any,
        new_values: dict[str, Any],
    ) -> bool:
        return False
