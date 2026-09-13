"""Vertex AI Experiments tracking for model performance comparison.

Logs each task completion as an experiment run with parameters (model, scheme,
max_steps) and metrics (cost, duration, CI pass, evaluation scores).

Feature-flagged via ``vertex_ai_experiments_enabled`` in settings, and only
active when the deployment actually runs on GCP.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from henchmen.observability.structured_logging import primary_model_name
from henchmen.observability.tracker import SUCCESS_STATUSES

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)


def _log_run_blocking(
    project_id: str,
    region: str,
    experiment_name: str,
    run_name: str,
    params: dict[str, Any],
    metrics: dict[str, Any],
) -> None:
    """Blocking Vertex AI Experiments write — always call via ``asyncio.to_thread``.

    Uses the module-level ``aiplatform`` API: ``vertexai.experiment`` does not
    exist and ``aiplatform.Experiment`` has no ``start_run``, so the previous
    shape could never have executed end to end.
    """
    from google.cloud import aiplatform

    aiplatform.init(project=project_id, location=region, experiment=experiment_name)
    with aiplatform.start_run(run_name):
        aiplatform.log_params(params)
        aiplatform.log_metrics(metrics)


async def log_experiment_run(
    task_id: str,
    scheme_id: str,
    model_name: str,
    final_status: str,
    cost_usd: float,
    wall_clock_seconds: float,
    ci_passed: bool | None,
    evaluation_scores: dict[str, float] | None,
    total_input_tokens: int = 0,
    total_output_tokens: int = 0,
    max_steps: int = 0,
    project_id: str = "",
    region: str = "us-central1",
    experiment_name: str = "henchmen-operatives",
) -> None:
    """Log a task completion as a Vertex AI Experiment run.

    Each run records:
    - Parameters: model_name, scheme_id, max_steps
    - Metrics: cost_usd, wall_clock_seconds, ci_passed, quality scores
    """
    params: dict[str, Any] = {
        "model_name": model_name,
        "scheme_id": scheme_id,
        "max_steps": max_steps,
    }

    metrics: dict[str, float] = {
        "cost_usd": cost_usd,
        "wall_clock_seconds": wall_clock_seconds,
        "total_input_tokens": float(total_input_tokens),
        "total_output_tokens": float(total_output_tokens),
        "success": 1.0 if str(final_status or "").lower() in SUCCESS_STATUSES else 0.0,
    }

    if ci_passed is not None:
        metrics["ci_passed"] = 1.0 if ci_passed else 0.0

    if evaluation_scores:
        for key, value in evaluation_scores.items():
            metrics[f"eval_{key}"] = value

    run_name = f"task-{task_id[:12]}"
    try:
        await asyncio.to_thread(
            _log_run_blocking,
            project_id,
            region,
            experiment_name,
            run_name,
            params,
            metrics,
        )
        logger.info(
            "Logged experiment run %s: cost=$%.3f status=%s",
            run_name,
            cost_usd,
            final_status,
        )
    except ImportError:
        # Experiments were explicitly enabled, so a missing extra is a
        # misconfiguration the operator needs to see, not a debug detail.
        logger.warning(
            "google-cloud-aiplatform is not installed; "
            'HENCHMEN_VERTEX_AI_EXPERIMENTS_ENABLED has no effect. Install: pip install -e ".[gcp]"'
        )
    except Exception as exc:
        logger.warning("Failed to log experiment run for task %s: %s", task_id, exc)


async def maybe_log_experiment(
    settings: "Settings",
    task_data: dict[str, Any],
) -> None:
    """Log experiment if enabled in settings. Convenience wrapper for the mastermind."""
    if not getattr(settings, "vertex_ai_experiments_enabled", False):
        return

    if settings.provider != "gcp" or not settings.gcp_project_id:
        logger.debug("Vertex AI Experiments enabled but the deployment is not on GCP; skipping.")
        return

    task_id = task_data.get("task_id", "")
    if not task_id:
        return

    await log_experiment_run(
        task_id=task_id,
        scheme_id=task_data.get("scheme_id", ""),
        model_name=primary_model_name(task_data),
        final_status=task_data.get("final_status", ""),
        cost_usd=task_data.get("estimated_cost_usd", 0.0),
        wall_clock_seconds=task_data.get("wall_clock_seconds", 0.0),
        ci_passed=task_data.get("ci_passed"),
        evaluation_scores=task_data.get("evaluation_scores"),
        total_input_tokens=task_data.get("total_input_tokens", 0),
        total_output_tokens=task_data.get("total_output_tokens", 0),
        project_id=settings.gcp_project_id,
        region=settings.gcp_region,
        experiment_name=settings.vertex_ai_experiment_name,
    )
