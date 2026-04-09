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
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request

# Register schemes on import
import henchmen.schemes.bugfix_standard  # noqa: F401
import henchmen.schemes.feature_standard  # noqa: F401
import henchmen.schemes.goal_decomposition  # noqa: F401
from henchmen.config.settings import get_settings
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


async def _check_message_dedup(message_id: str) -> bool:
    """Check if a Pub/Sub message was already processed.

    Uses DocumentStore get-then-set: if the document already exists, it is a
    duplicate.  The check-then-set is not atomic, but duplicates are harmless
    (idempotent task intake) — Pub/Sub at-least-once delivery means we accept
    rare double-processing rather than blocking on distributed locks.

    Returns True if the message is a duplicate (already processed).
    """
    if not message_id:
        return False
    try:
        agent = get_agent()
        store = agent.tracker._store
        existing = await store.get("processed_messages", message_id)
        if existing is not None:
            return True  # Already processed
        await store.set(
            "processed_messages",
            message_id,
            {"processed_at": datetime.now(UTC), "handler": "task-intake"},
        )
        return False  # New message — document created successfully
    except Exception:
        # All other exceptions propagate — Pub/Sub will retry delivery
        raise


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
    try:
        envelope = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    # Layer 1: Pub/Sub message-level dedup via DocumentStore
    message_id = envelope.get("message", {}).get("messageId", "")
    if await _check_message_dedup(message_id):
        logger.info("Duplicate Pub/Sub message %s, skipping", message_id)
        return {"status": "duplicate", "message_id": message_id}

    message = envelope.get("message", {})
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
            print(f"[MASTERMIND] Resumed task {resume_task_id} completed: {result.get('status')}", flush=True)
            return {"status": "completed", "task_id": resume_task_id}
        except Exception as exc:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            print(f"[MASTERMIND] Resume FAILED for {resume_task_id}: {exc}", flush=True)
            print(f"[MASTERMIND] Traceback: {tb[:1000]}", flush=True)
            try:
                await agent.tracker.mark_escalated(resume_task_id, reason=f"Resume failed: {exc}")
            except Exception:
                pass
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
        return {"status": "completed", "task_id": task.id}
    except Exception as exc:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        print(f"[MASTERMIND] Task FAILED for {task.id}: {exc}", flush=True)
        print(f"[MASTERMIND] Traceback: {tb[:1000]}", flush=True)
        try:
            await agent.tracker.mark_escalated(task.id, reason=f"Unhandled error: {exc}")
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Task processing failed: {exc}") from exc


async def _process_task(agent: MastermindAgent, task: HenchmenTask) -> None:
    """Process a task in the background."""
    import sys

    try:
        print(f"[MASTERMIND] Starting task processing: {task.id} ({task.title})", flush=True)
        result = await agent.handle_task(task)
        print(f"[MASTERMIND] Task {task.id} completed with status: {result.get('status')}", flush=True)
        print(f"[MASTERMIND] Result: {json.dumps(result, default=str)[:500]}", flush=True)

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
            print(f"[MASTERMIND] Sending Slack notification for task {task.id}", flush=True)
            await _notify_slack(task, result)

    except Exception as exc:
        print(f"[MASTERMIND] ERROR processing task {task.id}: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc()


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

    print(f"[MASTERMIND] Slack notification: status={status}, scheme={scheme_id}", flush=True)

    # Extract channel and thread_ts from source_id (format: "channel/thread_ts")
    parts = task.source_id.split("/")
    if len(parts) >= 2:
        channel = parts[0]
        thread_ts = parts[1]
        print(f"[MASTERMIND] Posting to Slack channel={channel} thread={thread_ts}", flush=True)
        try:
            client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
            print("[MASTERMIND] Slack notification sent successfully", flush=True)
        except Exception as exc:
            print(f"[MASTERMIND] Failed to notify Slack: {exc}", flush=True)
    else:
        print(f"[MASTERMIND] Cannot parse source_id for Slack: {task.source_id}", flush=True)


@app.post("/pubsub/operative-complete")
async def operative_complete_handler(request: Request) -> dict[str, Any]:
    """Handle operative completion reports from Pub/Sub.

    Feeds the real OperativeReport (with tokens, cost, files_changed) to the
    LairManager so wait_for_completion() returns accurate telemetry.
    """
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
            print(f"[CI-LOOP] FAILED for {task_id_prefix}: {ci_exc}", flush=True)
            print(f"[CI-LOOP] Traceback: {tb_str[:1000]}", flush=True)
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
        print(f"[CI-LOOP] Result: {result}", flush=True)
    except Exception as exc:
        print(f"[CI-LOOP] Error: {exc}", flush=True)


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
    """
    agent = get_agent()
    stalled = await agent.tracker.get_stalled_tasks(heartbeat_threshold_minutes=10)

    recovered = 0
    escalated = 0
    for task_data in stalled:
        task_id = task_data.get("task_id", "")
        attempts = task_data.get("recovery_attempts", 0)

        if attempts >= 3:
            await agent.tracker.mark_escalated(task_id, reason="Stalled after 3 recovery attempts")
            escalated += 1
            print(f"[WATCHDOG] Escalated stalled task {task_id} (attempts={attempts})", flush=True)
        else:
            await agent.tracker.mark_stalled(task_id)
            await agent.tracker.increment_recovery_attempts(task_id)
            # Re-publish for resume via MessageBroker
            try:
                broker = agent._get_broker()
                data = json.dumps({"resume_task_id": task_id}).encode("utf-8")
                await broker.publish(agent.settings.pubsub_topic_task_intake, data, task_id=task_id)
                recovered += 1
                print(f"[WATCHDOG] Re-published stalled task {task_id} for recovery", flush=True)
            except Exception as exc:
                print(f"[WATCHDOG] Failed to re-publish task {task_id}: {exc}", flush=True)

    from henchmen.observability.structured_logging import emit_watchdog_event

    emit_watchdog_event(stalled_count=len(stalled), recovered=recovered, escalated=escalated)

    return {"stalled_found": len(stalled), "recovered": recovered, "escalated": escalated}


@app.post("/api/v1/check-dlq")
async def check_dlq_handler() -> dict[str, Any]:
    """Check dead letter queue for lost messages.

    Called every 15 minutes by Cloud Scheduler.  Pulls up to 10 messages
    from the DLQ subscription, logs them, and acknowledges so they don't
    pile up.  Returns the count of dead-lettered messages found.

    TODO: Add pull() to MessageBroker for DLQ so this can use the provider interface.
    Pub/Sub pull (subscriber) semantics differ from the push-based publish interface,
    so this remains a direct SDK call for now.
    """
    from google.cloud import pubsub_v1  # type: ignore[attr-defined]

    agent = get_agent()
    project = agent.settings.gcp_project_id
    env = agent.settings.environment.value
    sub_path = f"projects/{project}/subscriptions/henchmen-{env}-dead-letter-sub"

    try:
        subscriber = pubsub_v1.SubscriberClient()
        response = subscriber.pull(request={"subscription": sub_path, "max_messages": 10})
        count = len(response.received_messages)

        if count > 0:
            print(f"[DLQ] Found {count} dead-lettered messages", flush=True)
            for msg in response.received_messages:
                print(f"[DLQ] Message: {msg.message.data.decode()[:500]}", flush=True)
            # Ack them so they don't pile up
            ack_ids = [m.ack_id for m in response.received_messages]
            subscriber.acknowledge(request={"subscription": sub_path, "ack_ids": ack_ids})

        return {"dead_letter_count": count}
    except Exception as exc:
        print(f"[DLQ] Check failed: {exc}", flush=True)
        return {"dead_letter_count": -1, "error": str(exc)}


@app.post("/api/v1/cleanup")
async def cleanup_handler() -> dict[str, Any]:
    """Cleanup stale tasks (called by Cloud Scheduler)."""
    logger.info("Running stale task cleanup")
    return {"status": "ok", "cleaned": 0}
