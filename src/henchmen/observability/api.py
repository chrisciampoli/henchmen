"""Metrics API -- FastAPI router for task execution metrics.

Exposes two surfaces:

1. ``/metrics/summary`` and ``/metrics/tasks`` -- JSON endpoints used by the
   built-in dashboard and self-hosters polling from scripts. ``ci_pass_rate``
   is returned as ``null`` (``None``) rather than ``0.0`` when there is no
   decided data, so alerting rules like ``ci_pass_rate < 0.5`` do not page on
   empty windows.
2. ``/metrics/prometheus`` -- OpenMetrics text format for Prometheus scrapers.
   Requires the ``observability`` extras (``prometheus-client``). Returns 503
   with a helpful message when the dependency is missing so operators discover
   the gap immediately rather than silently getting no data.
"""

import logging
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)


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

    tasks_completed = sum(
        1 for t in tasks if t.get("final_status") in ("completed", "COMPLETED")
    )
    tasks_escalated = sum(
        1 for t in tasks if t.get("final_status") in ("escalated", "ESCALATED")
    )

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
            "ci_pass_rate": (
                s["ci_passed"] / s["ci_decided"] if s["ci_decided"] > 0 else None
            ),
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


def create_metrics_router(tracker: Any) -> APIRouter:
    """Create a metrics API router bound to the given TaskTracker."""
    router = APIRouter(prefix="/metrics", tags=["metrics"])

    @router.get("/summary")
    async def get_summary(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
        """Aggregated task execution metrics for the given period."""
        tasks = await tracker.get_recent_tasks(days)
        return _compute_summary(tasks, days)

    @router.get("/tasks")
    async def get_tasks(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
        """List recent task execution records."""
        tasks = await tracker.get_recent_tasks(days)
        return {"period_days": days, "tasks": tasks}

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        """Retrieve a single task execution record by ID."""
        task_data = await tracker.get_task(task_id)
        if task_data is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        return dict(task_data)

    @router.get("/prometheus")
    async def get_prometheus(
        days: int = Query(default=7, ge=1, le=90),
    ) -> PlainTextResponse:
        """Expose a minimal OpenMetrics surface for Prometheus scrapers.

        Falls back to a 503 if ``prometheus-client`` is not installed so
        operators learn about the missing extras instead of silently getting
        no metrics.
        """
        try:
            from prometheus_client import (
                CONTENT_TYPE_LATEST,
                CollectorRegistry,
                Counter,
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

        # Use a fresh registry per request so the gauge/counter values reflect
        # the current window without leaking state across scrapes. Prometheus
        # counters here are effectively snapshots: Henchmen's persistent store
        # already owns the source of truth, so we expose derived totals.
        registry = CollectorRegistry()

        tasks_completed_total = Counter(
            "henchmen_tasks_completed_total",
            "Total tasks that reached a completed terminal state in the window.",
            registry=registry,
        )
        tasks_escalated_total = Counter(
            "henchmen_tasks_escalated_total",
            "Total tasks that escalated in the window.",
            registry=registry,
        )
        ci_pass_rate_gauge = Gauge(
            "henchmen_ci_pass_rate",
            "Fraction of decided CI runs that passed. Unset when no data.",
            registry=registry,
        )
        cost_usd_gauge = Gauge(
            "henchmen_cost_usd_total",
            "Total estimated LLM spend (USD) over the window.",
            registry=registry,
        )

        tasks_completed_total.inc(summary["tasks_completed"])
        tasks_escalated_total.inc(summary["tasks_escalated"])
        cost_usd_gauge.set(summary["total_cost_usd"])

        # When there is no decided CI data the gauge is intentionally left
        # unset rather than being pinned to 0. Prometheus will render this as
        # "no sample", which is the right signal for "we do not know yet".
        ci_pass_rate = summary["ci_pass_rate"]
        if ci_pass_rate is not None:
            ci_pass_rate_gauge.set(ci_pass_rate)

        return PlainTextResponse(
            content=generate_latest(registry).decode("utf-8"),
            media_type=CONTENT_TYPE_LATEST,
        )

    return router
