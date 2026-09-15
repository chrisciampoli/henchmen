"""Metrics API -- FastAPI router for task execution metrics.

Exposes two surfaces:

1. ``/metrics/summary``, ``/metrics/tasks`` and ``/metrics/tasks/{id}`` -- JSON
   endpoints used by the built-in dashboard and self-hosters polling from
   scripts. ``ci_pass_rate`` is returned as ``null`` (``None``) rather than
   ``0.0`` when there is no decided data, so alerting rules like
   ``ci_pass_rate < 0.5`` do not page on empty windows.
2. ``/metrics/prometheus`` -- OpenMetrics text format for Prometheus scrapers.
   Requires the ``observability`` extras (``prometheus-client``). Returns 503
   with a helpful message when the dependency is missing so operators discover
   the gap immediately rather than silently getting no data.

Security
--------
Task execution documents hold the original task payload (Slack thread
messages, Jira fields), interrupted-operative reports (git diffs) and per-node
lint/test output. None of that leaves this router: every task response is
projected onto :data:`_PUBLIC_TASK_FIELDS`, which is ids, statuses, timestamps
and numeric telemetry only.

Access is gated on ``HENCHMEN_METRICS_AUTH_TOKEN``. When the token is set every
request must carry ``Authorization: Bearer <token>``. When it is empty the
endpoints stay open in DEV (with a startup warning) and fail closed with 401 in
STAGING/PROD.
"""

import logging
import secrets
from collections.abc import Callable, Coroutine
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

from henchmen.observability.tracker import SUCCESS_STATUSES

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

# Fields safe to hand to any caller that can reach the metrics port. Everything
# omitted here is either free-form task content or operative output.
_PUBLIC_TASK_FIELDS: tuple[str, ...] = (
    "task_id",
    "scheme_id",
    "source",
    "created_at",
    "completed_at",
    "last_heartbeat",
    "final_status",
    "execution_state",
    "current_node_id",
    "escalation_node",
    "pr_url",
    "pr_number",
    "ci_passed",
    "ci_fix_attempts",
    "ci_fix_in_progress",
    "recovery_attempts",
    "nodes_executed",
    "node_metrics",
    "total_input_tokens",
    "total_output_tokens",
    "total_model_calls",
    "total_tool_calls",
    "estimated_cost_usd",
    "wall_clock_seconds",
    "rag_chunks_retrieved",
    "confidence_score",
    "evaluation_scores",
)


def _public_task_view(task: dict[str, Any]) -> dict[str, Any]:
    """Project a task execution document onto the non-sensitive fields."""
    view = {field: task[field] for field in _PUBLIC_TASK_FIELDS if field in task}
    files_changed = task.get("files_changed")
    if isinstance(files_changed, list):
        view["files_changed_count"] = len(files_changed)
    return view


def _compute_summary(tasks: list[dict[str, Any]], days: int) -> dict[str, Any]:
    """Compute the aggregated summary payload from a list of task records."""
    ci_passed = sum(1 for t in tasks if t.get("ci_passed") is True)
    ci_failed = sum(1 for t in tasks if t.get("ci_passed") is False)
    ci_pending = sum(1 for t in tasks if t.get("ci_passed") is None)
    ci_decided = ci_passed + ci_failed

    total_cost = sum(t.get("estimated_cost_usd", 0) for t in tasks)
    total_wall = sum(t.get("wall_clock_seconds", 0) for t in tasks)
    total_in = sum(t.get("total_input_tokens", 0) for t in tasks)
    total_out = sum(t.get("total_output_tokens", 0) for t in tasks)
    total_conf = sum(t.get("confidence_score", 0) for t in tasks)
    count = len(tasks)

    # ``pr_created`` is the happy path of the bugfix/feature schemes; counting
    # only ``completed`` reported every successful task as not-completed.
    tasks_completed = sum(1 for t in tasks if str(t.get("final_status") or "").lower() in SUCCESS_STATUSES)
    tasks_escalated = sum(1 for t in tasks if str(t.get("final_status") or "").lower() == "escalated")

    by_scheme: dict[str, dict[str, Any]] = {}
    for t in tasks:
        sid = t.get("scheme_id", "unknown")
        if sid not in by_scheme:
            by_scheme[sid] = {
                "count": 0,
                "ci_passed": 0,
                "ci_decided": 0,
                "total_cost": 0.0,
            }
        by_scheme[sid]["count"] += 1
        by_scheme[sid]["total_cost"] += t.get("estimated_cost_usd", 0)
        if t.get("ci_passed") is not None:
            by_scheme[sid]["ci_decided"] += 1
            if t.get("ci_passed") is True:
                by_scheme[sid]["ci_passed"] += 1

    by_scheme_out: dict[str, dict[str, Any]] = {}
    for sid, s in by_scheme.items():
        by_scheme_out[sid] = {
            "count": s["count"],
            # Return null rather than 0.0 when there is no decided CI data, so
            # alert rules that fire on low pass rates do not trip on empty
            # windows.
            "ci_pass_rate": (s["ci_passed"] / s["ci_decided"] if s["ci_decided"] > 0 else None),
            "avg_cost_usd": s["total_cost"] / s["count"] if s["count"] > 0 else 0.0,
        }

    return {
        "period_days": days,
        "tasks_total": count,
        "tasks_completed": tasks_completed,
        "tasks_escalated": tasks_escalated,
        "tasks_ci_passed": ci_passed,
        "tasks_ci_failed": ci_failed,
        "tasks_ci_pending": ci_pending,
        # ``None`` (JSON null) when no CI decisions have landed yet. This
        # prevents self-hosters whose dashboards page on ``ci_pass_rate < 0.5``
        # from being woken up by empty data.
        "ci_pass_rate": ci_passed / ci_decided if ci_decided > 0 else None,
        "total_cost_usd": round(total_cost, 3),
        "avg_cost_per_task_usd": round(total_cost / count, 3) if count > 0 else 0.0,
        "avg_wall_clock_seconds": round(total_wall / count, 1) if count > 0 else 0.0,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "avg_confidence_score": round(total_conf / count, 2) if count > 0 else 0.0,
        "by_scheme": by_scheme_out,
    }


_MetricsAuthDependency = Callable[..., Coroutine[Any, Any, None]]


def build_metrics_auth_dependency(settings: "Settings") -> _MetricsAuthDependency:
    """Return the FastAPI dependency guarding the /metrics router.

    Fail-closed: an unset token means "open" only in DEV on a repository
    checkout, never in STAGING, PROD or on a desktop install. The token itself
    is never logged.
    """
    from henchmen.config.paths import is_desktop_install

    return _build_metrics_auth(
        (settings.metrics_auth_token or "").strip(), settings.environment.value, is_desktop_install()
    )


def _build_metrics_auth(token: str, environment: str, desktop: bool) -> _MetricsAuthDependency:
    from henchmen.config.settings import Environment

    if not token:
        if environment in (Environment.STAGING.value, Environment.PROD.value) or desktop:

            async def _deny(authorization: str = Header(default="")) -> None:
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "HENCHMEN_METRICS_AUTH_TOKEN is not configured; "
                        f"the /metrics endpoints are disabled in {environment}."
                    ),
                )

            logger.error(
                "[metrics] HENCHMEN_METRICS_AUTH_TOKEN is empty in %s — /metrics endpoints will return 401",
                environment,
            )
            return _deny

        logger.warning(
            "[metrics] HENCHMEN_METRICS_AUTH_TOKEN is empty — /metrics endpoints are unauthenticated in %s",
            environment,
        )

        async def _allow(authorization: str = Header(default="")) -> None:
            return None

        return _allow

    expected = f"Bearer {token}"

    async def _require_bearer(authorization: str = Header(default="")) -> None:
        if not secrets.compare_digest(authorization.strip(), expected):
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing bearer token for /metrics.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return _require_bearer


# One dependency per (token, environment): the open/deny warning is logged once,
# not on every request.
_cached_metrics_auth = lru_cache(maxsize=8)(_build_metrics_auth)


async def require_metrics_auth(authorization: str = Header(default="")) -> None:
    """Per-request metrics auth for routes defined outside :func:`create_metrics_router`.

    Reads ``Settings`` when the request arrives, so it can be attached at import
    time (``@app.get(..., dependencies=[Depends(require_metrics_auth)])``) and
    applies exactly the bearer-token rules of the ``/metrics`` router.
    """
    from henchmen.config.paths import is_desktop_install
    from henchmen.config.settings import get_settings

    settings = get_settings()
    check = _cached_metrics_auth(
        (settings.metrics_auth_token or "").strip(), settings.environment.value, is_desktop_install()
    )
    await check(authorization)


def create_metrics_router(tracker: Any, settings: "Settings | None" = None) -> APIRouter:
    """Create a metrics API router bound to the given TaskTracker."""
    if settings is None:
        from henchmen.config.settings import get_settings

        settings = get_settings()

    router = APIRouter(
        prefix="/metrics",
        tags=["metrics"],
        dependencies=[Depends(build_metrics_auth_dependency(settings))],
    )

    @router.get("/summary")
    async def get_summary(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
        """Aggregated task execution metrics for the given period."""
        tasks = await tracker.get_recent_tasks(days)
        return _compute_summary(tasks, days)

    @router.get("/tasks")
    async def get_tasks(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
        """List recent task execution records (telemetry only, no task content)."""
        tasks = await tracker.get_recent_tasks(days)
        return {"period_days": days, "tasks": [_public_task_view(t) for t in tasks]}

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        """Retrieve a single task execution record (telemetry only) by ID."""
        task_data = await tracker.get_task(task_id)
        if task_data is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        return _public_task_view(dict(task_data))

    @router.get("/prometheus")
    async def get_prometheus(
        days: int = Query(default=7, ge=1, le=90),
    ) -> PlainTextResponse:
        """Expose a minimal OpenMetrics surface for Prometheus scrapers.

        Every series is a Gauge describing the trailing ``days`` window. They
        are deliberately *not* counters: the values are recomputed from a
        sliding window on each scrape, so they go down as old tasks age out and
        ``rate()``/``increase()`` over them would report phantom resets.

        Falls back to a 503 if ``prometheus-client`` is not installed so
        operators learn about the missing extras instead of silently getting
        no metrics.
        """
        try:
            from prometheus_client import (
                CONTENT_TYPE_LATEST,
                CollectorRegistry,
                Gauge,
                generate_latest,
            )
        except ImportError:
            return PlainTextResponse(
                content=(
                    "prometheus-client is not installed. "
                    "Install the observability extras to enable this endpoint: "
                    'pip install -e ".[observability]"'
                ),
                status_code=503,
            )

        tasks = await tracker.get_recent_tasks(days)
        summary = _compute_summary(tasks, days)

        # A fresh registry per request: the persistent store owns the source of
        # truth, this endpoint only exposes a derived snapshot of the window.
        registry = CollectorRegistry()
        window = str(days)

        tasks_completed_window = Gauge(
            "henchmen_tasks_completed_window",
            "Tasks that reached a successful terminal state within the window.",
            ["window_days"],
            registry=registry,
        )
        tasks_escalated_window = Gauge(
            "henchmen_tasks_escalated_window",
            "Tasks that escalated within the window.",
            ["window_days"],
            registry=registry,
        )
        cost_usd_window = Gauge(
            "henchmen_cost_usd_window",
            "Total estimated LLM spend (USD) over the window.",
            ["window_days"],
            registry=registry,
        )

        tasks_completed_window.labels(window_days=window).set(summary["tasks_completed"])
        tasks_escalated_window.labels(window_days=window).set(summary["tasks_escalated"])
        cost_usd_window.labels(window_days=window).set(summary["total_cost_usd"])

        # With no decided CI data the gauge is not registered at all. An
        # unlabelled Gauge exports its initial 0.0 even when ``set()`` is never
        # called, which would page every alert of the form
        # ``henchmen_ci_pass_rate < 0.5`` on an empty window.
        ci_pass_rate = summary["ci_pass_rate"]
        if ci_pass_rate is not None:
            ci_pass_rate_gauge = Gauge(
                "henchmen_ci_pass_rate",
                "Fraction of decided CI runs that passed. Absent when no data.",
                ["window_days"],
                registry=registry,
            )
            ci_pass_rate_gauge.labels(window_days=window).set(ci_pass_rate)

        return PlainTextResponse(
            content=generate_latest(registry).decode("utf-8"),
            media_type=CONTENT_TYPE_LATEST,
        )

    return router
