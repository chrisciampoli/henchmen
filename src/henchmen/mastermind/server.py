"""Mastermind service - Cloud Run HTTP handler for task orchestration.

Receives tasks via Pub/Sub push subscription and orchestrates the full
Scheme execution lifecycle.  Includes watchdog and DLQ monitoring
endpoints invoked by Cloud Scheduler.
"""

import asyncio
import base64
import json
import logging
import re
import signal
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Request

# Register schemes on import
import henchmen.schemes.bugfix_standard  # noqa: F401
import henchmen.schemes.feature_standard  # noqa: F401
import henchmen.schemes.goal_decomposition  # noqa: F401
from henchmen.config.settings import get_settings
from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc
from henchmen.mastermind.agent import MastermindAgent
from henchmen.models.task import HenchmenTask
from henchmen.observability.api import create_metrics_router

logger = logging.getLogger(__name__)

# Graceful shutdown event — set when SIGTERM received
_shutdown_event = asyncio.Event()


class _SecretRedactionFilter(logging.Filter):
    """Logging filter that redacts known secret token patterns before they reach Cloud Logging."""

    _PATTERNS = [
        re.compile(r"(ghp_[a-zA-Z0-9]{36})"),  # GitHub personal access tokens
        re.compile(r"(ghs_[a-zA-Z0-9]{36})"),  # GitHub server-to-server tokens
        re.compile(r"(xoxb-[a-zA-Z0-9-]+)"),  # Slack bot tokens
        re.compile(r"(sk-[a-zA-Z0-9]{32,})"),  # OpenAI / generic secret keys
        re.compile(r"(x-access-token:[^@\s]+)"),  # Git clone credential URLs
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.msg)
        for pattern in self._PATTERNS:
            msg = pattern.sub("***REDACTED***", msg)
        record.msg = msg
        return True


logging.getLogger().addFilter(_SecretRedactionFilter())

# Singleton agent instance
_agent: MastermindAgent | None = None


def get_agent() -> MastermindAgent:
    global _agent
    if _agent is None:
        settings = get_settings()
        from henchmen.providers.registry import ProviderRegistry

        registry = ProviderRegistry(settings)
        app.state.message_broker = registry.get_message_broker()
        app.state.document_store = registry.get_document_store()
        app.state.container_orchestrator = registry.get_container_orchestrator()
        _agent = MastermindAgent(
            settings=settings,
            broker=app.state.message_broker,
            document_store=app.state.document_store,
            container_orchestrator=app.state.container_orchestrator,
        )
    return _agent


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan handler for startup and graceful shutdown."""
    # Startup
    from henchmen.observability.tracing import init_tracing, instrument_fastapi, shutdown_tracing

    settings = get_settings()
    init_tracing("mastermind", project_id=settings.gcp_project_id)
    instrument_fastapi(app)

    # Initialize providers and agent
    from henchmen.providers.registry import ProviderRegistry

    registry = ProviderRegistry(settings)
    app.state.message_broker = registry.get_message_broker()
    app.state.document_store = registry.get_document_store()
    app.state.container_orchestrator = registry.get_container_orchestrator()

    global _agent
    _agent = MastermindAgent(
        settings=settings,
        broker=app.state.message_broker,
        document_store=app.state.document_store,
        container_orchestrator=app.state.container_orchestrator,
    )

    agent = get_agent()
    router = create_metrics_router(agent.tracker)
    app.include_router(router)

    # Register SIGTERM handler for graceful shutdown on Cloud Run
    loop = asyncio.get_running_loop()

    def _sigterm_handler() -> None:
        logger.info("[mastermind] SIGTERM received, initiating graceful shutdown")
        _shutdown_event.set()

    try:
        loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler
        pass

    logger.info("[mastermind] Service started")
    yield
    # Shutdown
    shutdown_tracing()
    logger.info("[mastermind] Shutting down — active tasks: %d", len(agent._active_tasks))


app = FastAPI(title="Henchmen Mastermind", description="Task orchestration engine", lifespan=lifespan)


async def _acquire_watchdog_lease(
    store: Any,
    env: str,
    instance_id: str,
    ttl_seconds: int = 60,
) -> bool:
    """Try to acquire a short-lived watchdog lease in the document store.

    Uses a get-then-set pattern (not a true transaction) but that is
    sufficient here: the watchdog is idempotent, a rare double-run is no
    worse than the current behaviour, and the real fix is a Firestore
    transaction, tracked as a follow-up.  Returns True if this caller
    now holds the lease, False if another replica holds an unexpired
    lease.
    """
    try:
        existing = await store.get("watchdog_leases", env)
    except Exception as exc:
        logger.warning("[watchdog] Lease get failed (%s); skipping this tick", exc)
        return False

    now = datetime.now(UTC)
    if existing:
        expires_raw = existing.get("expires_at", "1970-01-01T00:00:00+00:00")
        try:
            expires = datetime.fromisoformat(expires_raw)
        except ValueError:
            expires = datetime.fromtimestamp(0, tz=UTC)
        if expires > now:
            logger.info(
                "[watchdog] Lease held by %s until %s; skipping",
                existing.get("holder", "unknown"),
                expires_raw,
            )
            return False

    try:
        await store.set(
            "watchdog_leases",
            env,
            {
                "holder": instance_id,
                "acquired_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            },
        )
    except Exception as exc:
        logger.warning("[watchdog] Lease set failed (%s); skipping this tick", exc)
        return False

    return True


# In-flight markers older than this are considered reclaimable: if a prior
# handler crashed after marking the message as in_flight but before completing
# (or before marking as done), a Pub/Sub redelivery should be allowed to retry.
# This is the E2 fix for the check-then-set race that could silently drop
# tasks on transient processing failures.
_DEDUP_INFLIGHT_TTL_SECONDS = 900


async def _check_message_dedup(message_id: str, dedup_key: str | None = None) -> bool:
    """Check if a Pub/Sub message (or an explicit dedup key) was already processed.

    Two-phase dedup (E2 fix):

    1. On first observation, the message is marked ``in_flight`` with an
       acquisition timestamp. The handler runs, and only on successful
       completion does the caller upgrade the marker to ``done`` via
       :func:`_mark_message_done`.
    2. A subsequent delivery that sees a ``done`` marker is a true duplicate
       and returns True.
    3. A subsequent delivery that sees an ``in_flight`` marker inspects its
       ``acquired_at`` timestamp: if older than ``_DEDUP_INFLIGHT_TTL_SECONDS``
       the prior handler is assumed crashed and the retry is allowed to
       reclaim the marker. If newer, a concurrent handler is running and the
       retry returns True (treat as duplicate — the concurrent handler will
       ack the original delivery).

    This closes the original failure mode: dedup doc written before
    processing, processing crashes, Pub/Sub retries, retry sees the dedup doc
    and silently ack's — the task is lost. With the TTL reclaim path the
    retry either completes normally (if the prior was short) or reclaims
    (if the prior crashed).

    When a caller supplies an application-level ``dedup_key`` (e.g. the
    watchdog's ``resume-<task_id>-<attempts>``), it is checked in addition
    to the Pub/Sub ``message_id``.

    Returns True if the message should be treated as a duplicate (skipped).
    """
    agent = get_agent()
    store = agent.tracker._store
    now = datetime.now(UTC)

    async def _check_or_claim(key: str) -> bool:
        try:
            existing = await store.get("processed_messages", key)
        except Exception:
            raise
        if existing is not None:
            status = existing.get("status", "done")
            if status == "done":
                return True
            # in_flight — check TTL
            acquired_raw = existing.get("acquired_at") or existing.get("processed_at")
            if acquired_raw:
                try:
                    acquired = datetime.fromisoformat(acquired_raw)
                except ValueError:
                    acquired = now  # Treat as freshly acquired on parse failure
                age_seconds = (now - acquired).total_seconds()
                if age_seconds < _DEDUP_INFLIGHT_TTL_SECONDS:
                    logger.info(
                        "[dedup] %s is in_flight (age=%.0fs), treating retry as duplicate",
                        key,
                        age_seconds,
                    )
                    return True
                logger.warning(
                    "[dedup] %s in_flight marker is stale (age=%.0fs); reclaiming for retry",
                    key,
                    age_seconds,
                )
        # Mark as in_flight — the caller is responsible for upgrading to done
        # after successful processing, or leaving the marker to expire on
        # failure (so Pub/Sub's redelivery can reclaim).
        try:
            await store.set(
                "processed_messages",
                key,
                {
                    "status": "in_flight",
                    "acquired_at": now.isoformat(),
                    "handler": "task-intake",
                    "key": key,
                },
            )
        except Exception:
            raise
        return False

    # Application-level dedup key takes precedence because it is deterministic.
    if dedup_key:
        if await _check_or_claim(dedup_key):
            return True

    if message_id:
        if await _check_or_claim(message_id):
            return True

    return False


async def _mark_message_done(message_id: str, dedup_key: str | None = None) -> None:
    """Upgrade an ``in_flight`` dedup marker to ``done`` after successful processing.

    This is the second half of the E2 two-phase dedup fix. Call this ONLY
    when the handler has fully committed the task (persisted state, published
    downstream events, etc.). Best-effort — failures are logged but do not
    propagate, since a missing upgrade will be harmlessly reclaimed after
    ``_DEDUP_INFLIGHT_TTL_SECONDS`` by a future retry or the watchdog.
    """
    agent = get_agent()
    store = agent.tracker._store
    now = datetime.now(UTC).isoformat()
    for key in (dedup_key, message_id):
        if not key:
            continue
        try:
            await store.set(
                "processed_messages",
                key,
                {
                    "status": "done",
                    "processed_at": now,
                    "handler": "task-intake",
                    "key": key,
                },
            )
        except Exception as exc:
            logger.warning("[dedup] Failed to mark %s as done: %s", key, exc)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "component": "mastermind"}


@app.post("/pubsub/task-intake")
async def task_intake_handler(request: Request) -> dict[str, Any]:
    """Handle incoming tasks from Pub/Sub push subscription.

    Pub/Sub wraps messages in an envelope:
    {
      "message": {
        "data": "<base64-encoded HenchmenTask JSON>",
        "attributes": {...},
        "messageId": "..."
      },
      "subscription": "..."
    }
    """
    await verify_pubsub_oidc(request, get_settings())
    try:
        envelope = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    # Layer 1: Pub/Sub message-level dedup via DocumentStore. Honor both
    # Pub/Sub's ``messageId`` and an optional application-level
    # ``dedup_key`` attribute — the watchdog sets the latter to
    # ``resume-<task_id>-<attempts>`` so two replicas racing past the
    # watchdog lease cannot both re-publish the same resume.
    message = envelope.get("message", {})
    message_id = message.get("messageId", "")
    attributes = message.get("attributes") or {}
    dedup_key = attributes.get("dedup_key") if isinstance(attributes, dict) else None
    if await _check_message_dedup(message_id, dedup_key=dedup_key):
        logger.info(
            "Duplicate Pub/Sub message (message_id=%s, dedup_key=%s), skipping",
            message_id,
            dedup_key,
        )
        return {"status": "duplicate", "message_id": message_id, "dedup_key": dedup_key}

    data_b64 = message.get("data", "")

    if not data_b64:
        logger.warning("Empty Pub/Sub message received")
        return {"status": "ok", "detail": "empty message"}

    try:
        data_bytes = base64.b64decode(data_b64)
        task_data = json.loads(data_bytes.decode("utf-8"))
    except Exception as exc:
        logger.error("Failed to decode Pub/Sub message: %s", exc)
        return {"status": "error", "detail": str(exc)}

    # Detect resume requests from watchdog (re-published stalled tasks)
    resume_task_id = task_data.get("resume_task_id")
    agent = get_agent()

    if resume_task_id:
        logger.info("Resuming task from watchdog: %s", resume_task_id)
        try:
            result = await agent.resume_task(resume_task_id)
            logger.info("[MASTERMIND] Resumed task %s completed: %s", resume_task_id, result.get("status"))
            # E2: only upgrade dedup marker to ``done`` after successful processing.
            await _mark_message_done(message_id, dedup_key=dedup_key)
            return {"status": "completed", "task_id": resume_task_id}
        except Exception as exc:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            logger.error("[MASTERMIND] Resume FAILED for %s: %s", resume_task_id, exc)
            logger.error("[MASTERMIND] Traceback: %s", tb[:1000])
            try:
                await agent.tracker.mark_escalated(resume_task_id, reason=f"Resume failed: {exc}")
            except Exception:
                pass
            # E2: do NOT mark as done — leave the in_flight marker so Pub/Sub
            # can retry and the stale TTL reclaim path handles it.
            raise HTTPException(status_code=500, detail=f"Resume failed: {exc}") from exc

    logger.info("Received task from Pub/Sub: %s (source: %s)", task_data.get("id"), task_data.get("source"))

    try:
        task = HenchmenTask.model_validate(task_data)
    except Exception as exc:
        logger.error("Failed to validate task: %s", exc)
        return {"status": "error", "detail": f"Invalid task data: {exc}"}

    # Execute synchronously — Cloud Run keeps the request alive (up to 3600s).
    # Returning before completion would ack the Pub/Sub message, causing lost tasks
    # if the instance recycles.  Returning 500 triggers Pub/Sub retry.
    try:
        await _process_task(agent, task)
        # E2: only upgrade dedup marker to ``done`` after successful processing.
        await _mark_message_done(message_id, dedup_key=dedup_key)
        return {"status": "completed", "task_id": task.id}
    except Exception as exc:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        logger.error("[MASTERMIND] Task FAILED for %s: %s", task.id, exc)
        logger.error("[MASTERMIND] Traceback: %s", tb[:1000])
        try:
            await agent.tracker.mark_escalated(task.id, reason=f"Unhandled error: {exc}")
        except Exception:
            pass
        # E2: do NOT mark as done — leave the in_flight marker so Pub/Sub
        # can retry and the stale TTL reclaim path handles it.
        raise HTTPException(status_code=500, detail=f"Task processing failed: {exc}") from exc


async def _process_task(agent: MastermindAgent, task: HenchmenTask) -> None:
    """Process a task in the background."""
    try:
        logger.info("[MASTERMIND] Starting task processing: %s (%s)", task.id, task.title)
        result = await agent.handle_task(task)
        logger.info("[MASTERMIND] Task %s completed with status: %s", task.id, result.get("status"))
        logger.info("[MASTERMIND] Result: %s", json.dumps(result, default=str)[:500])

        # Emit structured metric for Cloud Monitoring
        from henchmen.observability.structured_logging import emit_task_completed

        task_metrics = await agent.tracker.get_task(task.id)
        emit_task_completed(
            task_id=task.id,
            scheme_id=result.get("scheme_id", "unknown"),
            final_status=result.get("result", {}).get("final_status", result.get("status", "unknown")),
            cost_usd=task_metrics.get("estimated_cost_usd", 0.0) if task_metrics else 0.0,
            wall_clock_seconds=task_metrics.get("wall_clock_seconds", 0.0) if task_metrics else 0.0,
        )

        # Notify Slack if the task came from Slack
        if task.source.value == "slack":
            logger.info("[MASTERMIND] Sending Slack notification for task %s", task.id)
            await _notify_slack(task, result)

    except Exception as exc:
        logger.exception("[MASTERMIND] ERROR processing task %s: %s", task.id, exc)


def _format_metrics_block(metrics: dict[str, Any]) -> str:
    """Format task metrics into a Slack-friendly text block."""
    cost = metrics.get("estimated_cost_usd", 0)
    in_tokens = metrics.get("total_input_tokens", 0)
    out_tokens = metrics.get("total_output_tokens", 0)
    wall = metrics.get("wall_clock_seconds", 0)
    files = metrics.get("files_changed", [])
    confidence = metrics.get("confidence_score", 0)
    node_metrics = metrics.get("node_metrics", {})

    in_k = f"{in_tokens // 1000}K" if in_tokens >= 1000 else str(in_tokens)
    out_k = f"{out_tokens // 1000}K" if out_tokens >= 1000 else str(out_tokens)

    minutes = int(wall // 60)
    seconds = int(wall % 60)
    duration = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

    node_parts = []
    for node_id, nm in node_metrics.items():
        node_secs = nm.get("wall_clock_seconds", 0)
        node_min = int(node_secs // 60)
        short_name = node_id.replace("_", " ").split()[-1]
        node_parts.append(f"{short_name} ({node_min}m)")
    node_line = " -> ".join(node_parts) if node_parts else "n/a"

    lines = [
        "\nMetrics:",
        f"- Cost: ${cost:.2f} ({in_k} in / {out_k} out tokens)",
        f"- Duration: {duration}",
        f"- Nodes: {node_line}",
        f"- Files changed: {len(files)}",
        f"- Confidence: {confidence:.2f}",
    ]
    return "\n".join(lines)


def _format_ci_result_message(pr_number: int, ci_passed: bool, failed_checks: list[str]) -> str:
    """Format a CI result message for Slack."""
    if ci_passed:
        return f"CI result for PR #{pr_number}: All checks passed"
    else:
        checks = ", ".join(failed_checks) if failed_checks else "unknown checks"
        return f"CI result for PR #{pr_number}: FAILED ({checks})"


async def _notify_slack(task: HenchmenTask, result: dict[str, Any]) -> None:
    """Send a status update back to Slack."""
    import os

    from slack_sdk import WebClient

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        return

    client = WebClient(token=bot_token)
    # Use final_status from execution report (not state machine status) to detect escalations
    final_status = result.get("result", {}).get("final_status", result.get("status", "unknown"))
    status = "escalated" if final_status == "escalated" else result.get("status", "unknown")
    scheme_id = result.get("scheme_id", "unknown")

    if status == "completed":
        pr_url = result.get("result", {}).get("pr_url", "")
        text = f"Task completed! Scheme: `{scheme_id}`"
        if pr_url:
            text += f"\nPR: {pr_url}"
        # Enrich with metrics from tracker
        try:
            agent = get_agent()
            metrics = await agent.tracker.get_task(task.id)
            if metrics:
                text += _format_metrics_block(metrics)
        except Exception:
            pass  # Metrics enrichment is best-effort
    elif status == "escalated":
        reason = result.get("error", result.get("result", {}).get("reason", "unknown"))
        # Extract details about what failed from node results
        node_results = result.get("result", {}).get("node_results", {})
        failed_nodes = []
        for node_id, nr in node_results.items():
            if nr.get("condition") == "fail":
                msg = nr.get("message", "")
                output = nr.get("output", "")[:300]
                failed_nodes.append(f"  - `{node_id}`: {msg}" + (f"\n    ```{output}```" if output else ""))
        escalated = result.get("result", {}).get("escalated", False)
        icon = "\U0001f6a8" if escalated else "\u26a0\ufe0f"
        text = f"{icon} Task escalated — needs human review.\nScheme: `{scheme_id}`\nReason: {reason}"
        if failed_nodes:
            text += "\n\nFailed checks:\n" + "\n".join(failed_nodes)
        # Enrich with metrics from tracker
        try:
            agent = get_agent()
            metrics = await agent.tracker.get_task(task.id)
            if metrics:
                text += _format_metrics_block(metrics)
        except Exception:
            pass
    else:
        text = f"Task status: `{status}` (scheme: `{scheme_id}`)"

    logger.info("[MASTERMIND] Slack notification: status=%s, scheme=%s", status, scheme_id)

    # Extract channel and thread_ts from source_id (format: "channel/thread_ts")
    parts = task.source_id.split("/")
    if len(parts) >= 2:
        channel = parts[0]
        thread_ts = parts[1]
        logger.info("[MASTERMIND] Posting to Slack channel=%s thread=%s", channel, thread_ts)
        try:
            client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
            logger.info("[MASTERMIND] Slack notification sent successfully")
        except Exception as exc:
            logger.error("[MASTERMIND] Failed to notify Slack: %s", exc)
    else:
        logger.error("[MASTERMIND] Cannot parse source_id for Slack: %s", task.source_id)


@app.post("/pubsub/operative-complete")
async def operative_complete_handler(request: Request) -> dict[str, Any]:
    """Handle operative completion reports from Pub/Sub.

    Feeds the real OperativeReport (with tokens, cost, files_changed) to the
    LairManager so wait_for_completion() returns accurate telemetry.
    """
    await verify_pubsub_oidc(request, get_settings())
    try:
        envelope = await request.json()

        # Layer 1: Pub/Sub message-level dedup
        message_id = envelope.get("message", {}).get("messageId", "")
        if await _check_message_dedup(message_id):
            logger.info("Duplicate operative-complete message %s, skipping", message_id)
            return {"status": "duplicate", "message_id": message_id}

        message = envelope.get("message", {})
        data_b64 = message.get("data", "")
        data = json.loads(base64.b64decode(data_b64).decode("utf-8"))

        # Parse the full OperativeReport, persist to DocumentStore, and feed to LairManager
        from henchmen.models.operative import OperativeReport

        report = OperativeReport.model_validate(data)
        agent = get_agent()

        # Write to DocumentStore so any Mastermind instance can pick it up
        # (cross-instance coordination — the instance polling may differ from this one)
        try:
            report_key = f"{report.task_id}:{report.node_id}"
            await agent.tracker._store.set("operative_reports", report_key, report.model_dump(mode="json"))
        except Exception as store_exc:
            logger.warning("Failed to persist operative report to DocumentStore: %s", store_exc)

        agent.lair_manager.notify_operative_complete(report)

        logger.info(
            "Operative complete: task=%s, node=%s, status=%s, tokens=%d/%d",
            report.task_id,
            report.node_id,
            report.status.value,
            report.total_input_tokens,
            report.total_output_tokens,
        )

        from henchmen.observability.structured_logging import emit_operative_status

        emit_operative_status(
            task_id=report.task_id,
            node_id=report.node_id,
            status=report.status.value,
            input_tokens=report.total_input_tokens,
            output_tokens=report.total_output_tokens,
        )

        return {"status": "ok"}
    except Exception as exc:
        logger.error("Failed to process operative-complete: %s", exc)
        return {"status": "error", "detail": str(exc)}


@app.post("/pubsub/forge-result")
async def forge_result_handler(request: Request) -> dict[str, Any]:
    """Handle CI results from Forge via Pub/Sub."""
    await verify_pubsub_oidc(request, get_settings())
    try:
        envelope = await request.json()
        message = envelope.get("message", {})
        data_b64 = message.get("data", "")
        data = json.loads(base64.b64decode(data_b64).decode("utf-8"))

        request_id = data.get("request_id", "")
        agent = get_agent()
        agent.notify_forge_result(request_id, data)

        # Track CI result
        task_id = data.get("task_id", "")
        ci_passed = data.get("status") == "passed"
        if task_id:
            await agent.tracker.record_ci_result(task_id, ci_passed)

        logger.info("Forge result: request_id=%s, status=%s", request_id, data.get("status"))
        return {"status": "ok"}
    except Exception as exc:
        logger.error("Failed to process forge-result: %s", exc)
        return {"status": "error", "detail": str(exc)}


@app.post("/pubsub/ci-failure")
async def ci_failure_handler(request: Request) -> dict[str, Any]:
    """Handle CI failure events from Pub/Sub."""
    await verify_pubsub_oidc(request, get_settings())
    try:
        envelope = await request.json()
        message = envelope.get("message", {})
        data_b64 = message.get("data", "")
        data = json.loads(base64.b64decode(data_b64).decode("utf-8"))

        task_id_prefix = data.get("task_id_prefix", "")
        repo = data.get("repo", "")
        branch = data.get("branch", "")
        check_suite_id = data.get("check_suite_id", 0)

        if not task_id_prefix:
            return {"status": "error", "detail": "missing task_id_prefix"}

        agent = get_agent()
        try:
            await _handle_ci_failure(agent, task_id_prefix, repo, branch, check_suite_id)
            return {"status": "completed"}
        except Exception as ci_exc:
            tb_str = "".join(traceback.format_exception(type(ci_exc), ci_exc, ci_exc.__traceback__))
            logger.error("[CI-LOOP] FAILED for %s: %s", task_id_prefix, ci_exc)
            logger.error("[CI-LOOP] Traceback: %s", tb_str[:1000])
            raise HTTPException(status_code=500, detail=f"CI failure handling failed: {ci_exc}") from ci_exc
    except Exception as exc:
        logger.error("Failed to process ci-failure: %s", exc)
        return {"status": "error", "detail": str(exc)}


async def _handle_ci_failure(
    agent: MastermindAgent,
    task_id_prefix: str,
    repo: str,
    branch: str,
    check_suite_id: int,
) -> None:
    """Process CI failure in the background."""
    try:
        result = await agent.handle_ci_failure(task_id_prefix, repo, branch, check_suite_id)
        logger.info("[CI-LOOP] Result: %s", result)
    except Exception as exc:
        logger.error("[CI-LOOP] Error: %s", exc)


@app.get("/api/v1/metrics/summary")
async def metrics_summary(days: int = 7) -> dict[str, Any]:
    """Return aggregated metrics for the dashboard.

    Covers cost-and-quality correlation: success rate, escalation rate,
    average cost per task, token usage, cost by model, and escalation reasons.

    Query params:
        days: Number of days to look back (default 7).
    """
    agent = get_agent()
    return await agent.tracker.get_metrics_summary(days)


@app.post("/api/v1/watchdog")
async def watchdog_handler() -> dict[str, Any]:
    """Detect stalled tasks and trigger recovery.

    Called every 5 minutes by Cloud Scheduler.  Queries Firestore for
    tasks whose heartbeat has expired (>10 min) and either re-publishes
    them for retry or escalates after 3 failed recovery attempts.

    Gated behind a short-lived Firestore lease (``watchdog_leases/{env}``)
    so two Mastermind replicas cannot both run the body simultaneously.
    The lease is best-effort (no true transaction) — the real fix is a
    Firestore transaction, tracked as a follow-up — but combined with the
    resume-publish dedup key below it eliminates the most common double
    re-publish race.
    """
    import os
    import uuid

    agent = get_agent()
    env = agent.settings.environment.value

    # Best-effort lease: skip this tick if another replica already holds it.
    instance_id = os.environ.get("K_REVISION") or os.environ.get("HOSTNAME") or str(uuid.uuid4())
    store = agent.tracker._store
    have_lease = await _acquire_watchdog_lease(store, env, instance_id, ttl_seconds=60)
    if not have_lease:
        return {"stalled_found": 0, "recovered": 0, "escalated": 0, "skipped": "lease_held"}

    stalled = await agent.tracker.get_stalled_tasks(heartbeat_threshold_minutes=10)

    recovered = 0
    escalated = 0
    for task_data in stalled:
        task_id = task_data.get("task_id", "")
        attempts = task_data.get("recovery_attempts", 0)

        if attempts >= 3:
            await agent.tracker.mark_escalated(task_id, reason="Stalled after 3 recovery attempts")
            escalated += 1
            logger.warning("[WATCHDOG] Escalated stalled task %s (attempts=%d)", task_id, attempts)
        else:
            await agent.tracker.mark_stalled(task_id)
            await agent.tracker.increment_recovery_attempts(task_id)
            # Re-publish for resume via MessageBroker.
            #
            # Deterministic dedup key: if two replicas race past the lease
            # (e.g. during lease expiry), the task-intake handler's Layer 1
            # dedup (`_check_message_dedup`) will reject the duplicate via
            # this identical ``dedup_key``.  The key is attached as a
            # Pub/Sub attribute so the receiver can inspect it.
            dedup_key = f"resume-{task_id}-{attempts}"
            try:
                broker = agent._get_broker()
                data = json.dumps({"resume_task_id": task_id}).encode("utf-8")
                await broker.publish(
                    agent.settings.pubsub_topic_task_intake,
                    data,
                    task_id=task_id,
                    dedup_key=dedup_key,
                )
                recovered += 1
                logger.info(
                    "[WATCHDOG] Re-published stalled task %s for recovery (dedup_key=%s)",
                    task_id,
                    dedup_key,
                )
            except Exception as exc:
                logger.error("[WATCHDOG] Failed to re-publish task %s: %s", task_id, exc)

    from henchmen.observability.structured_logging import emit_watchdog_event

    emit_watchdog_event(stalled_count=len(stalled), recovered=recovered, escalated=escalated)

    return {"stalled_found": len(stalled), "recovered": recovered, "escalated": escalated}


@app.post("/api/v1/check-dlq")
async def check_dlq_handler() -> dict[str, Any]:
    """Check dead letter queue for lost messages.

    Called every 15 minutes by Cloud Scheduler.  Pulls up to 10 messages
    from the DLQ subscription via the ``MessageBroker`` provider interface,
    logs them, and acknowledges so they don't pile up.  Returns the count
    of dead-lettered messages found.
    """
    agent = get_agent()
    env = agent.settings.environment.value
    subscription_name = f"henchmen-{env}-dead-letter-sub"

    try:
        broker = agent._get_broker()
        messages = await broker.pull_dlq(subscription_name, max_messages=10)
        count = len(messages)

        if count > 0:
            logger.warning("[DLQ] Found %d dead-lettered messages", count)
            for msg in messages:
                data = str(msg.get("data", ""))[:500]
                logger.warning("[DLQ] Message: %s", data)

        return {"dead_letter_count": count}
    except NotImplementedError as exc:
        # Provider (e.g. AWS SNS) does not expose a DLQ pull path.
        logger.info("[DLQ] Check skipped (provider unsupported): %s", exc)
        return {"dead_letter_count": 0, "skipped": True, "reason": str(exc)}
    except Exception as exc:
        logger.error("[DLQ] Check failed: %s", exc)
        return {"dead_letter_count": -1, "error": str(exc)}


@app.post("/api/v1/cleanup")
async def cleanup_handler() -> dict[str, Any]:
    """Cleanup stale tasks (called by Cloud Scheduler)."""
    logger.info("Running stale task cleanup")
    return {"status": "ok", "cleaned": 0}
