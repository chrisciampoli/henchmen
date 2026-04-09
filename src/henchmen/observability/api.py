"""Metrics API — FastAPI router for task execution metrics."""

import logging
from typing import Any

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)


def create_metrics_router(tracker: Any) -> APIRouter:
    """Create a metrics API router bound to the given TaskTracker."""
    router = APIRouter(prefix="/metrics", tags=["metrics"])

    @router.get("/summary")
    async def get_summary(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
        """Aggregated task execution metrics for the given period."""
        tasks = await tracker.get_recent_tasks(days)

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

        by_scheme: dict[str, dict[str, Any]] = {}
        for t in tasks:
            sid = t.get("scheme_id", "unknown")
            if sid not in by_scheme:
                by_scheme[sid] = {"count": 0, "ci_passed": 0, "ci_decided": 0, "total_cost": 0.0}
            by_scheme[sid]["count"] += 1
            by_scheme[sid]["total_cost"] += t.get("estimated_cost_usd", 0)
            if t.get("ci_passed") is not None:
                by_scheme[sid]["ci_decided"] += 1
                if t.get("ci_passed") is True:
                    by_scheme[sid]["ci_passed"] += 1

        by_scheme_out = {}
        for sid, s in by_scheme.items():
            by_scheme_out[sid] = {
                "count": s["count"],
                "ci_pass_rate": s["ci_passed"] / s["ci_decided"] if s["ci_decided"] > 0 else 0.0,
                "avg_cost_usd": s["total_cost"] / s["count"] if s["count"] > 0 else 0.0,
            }

        return {
            "period_days": days,
            "tasks_total": count,
            "tasks_ci_passed": ci_passed,
            "tasks_ci_failed": ci_failed,
            "tasks_ci_pending": ci_pending,
            "ci_pass_rate": ci_passed / ci_decided if ci_decided > 0 else 0.0,
            "total_cost_usd": round(total_cost, 3),
            "avg_cost_per_task_usd": round(total_cost / count, 3) if count > 0 else 0.0,
            "avg_wall_clock_seconds": round(total_wall / count, 1) if count > 0 else 0.0,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "avg_confidence_score": round(total_conf / count, 2) if count > 0 else 0.0,
            "by_scheme": by_scheme_out,
        }

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

    return router
