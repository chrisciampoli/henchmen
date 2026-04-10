"""Structured logging for GCP Cloud Logging and Cloud Monitoring integration.

Emits JSON-structured log entries compatible with Cloud Logging's structured
payload format.  When running on Cloud Run, these are automatically ingested
and can drive log-based metrics and alerting policies in Cloud Monitoring.

.. note::
   This module intentionally uses ``print(..., file=sys.stdout, flush=True)``
   rather than the ``logging`` module. Cloud Logging parses JSON directly
   from stdout lines emitted by Cloud Run containers; routing through
   ``logging`` would either wrap the JSON in Python's default formatter
   (breaking the structured payload) or require disabling the root
   logger's handlers, which has its own side-effects for third-party
   libraries. Keeping a single raw ``print`` here is the minimum-impact
   way to preserve the structured payload contract. Do not replace this
   with ``logger.info(...)`` — it will break metrics ingestion.

Usage:
    from henchmen.observability.structured_logging import emit_metric

    emit_metric("task.completed", {"task_id": "abc", "cost_usd": 0.35, "model": "claude-sonnet-4@20250514"})
"""

import json
import logging
import sys
from typing import Any

logger = logging.getLogger("henchmen.metrics")

# Severity levels recognised by Cloud Logging structured payloads
_SEVERITY_MAP = {
    "debug": "DEBUG",
    "info": "INFO",
    "warning": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
}


def emit_metric(
    metric_name: str,
    labels: dict[str, Any] | None = None,
    value: float = 1.0,
    severity: str = "info",
) -> None:
    """Emit a structured log entry that doubles as a Cloud Monitoring metric source.

    Cloud Logging log-based metrics can be configured to extract numeric values
    from ``jsonPayload.metric_value`` and use ``jsonPayload.metric_labels`` for
    grouping.  This function writes a JSON line to stdout that Cloud Run's
    logging agent picks up automatically.

    Args:
        metric_name: Dot-separated metric name (e.g. ``task.completed``,
            ``cost.exceeded``, ``operative.timed_out``).
        labels: Key-value pairs for metric dimensions.
        value: Numeric metric value (default 1.0 for counters).
        severity: Log severity — one of debug, info, warning, error, critical.
    """
    # Cloud Logging metric labels must be strings — cast every value to str so
    # int/float/bool labels (e.g. recovered counts, ceiling amounts) do not get
    # rejected by the log-based metric extractor or break downstream parsers.
    str_labels: dict[str, str] = {k: str(v) for k, v in (labels or {}).items()}

    entry: dict[str, Any] = {
        "severity": _SEVERITY_MAP.get(severity, "INFO"),
        "message": f"metric:{metric_name}",
        "metric_name": metric_name,
        "metric_value": value,
        "metric_labels": str_labels,
    }

    # Write as a single JSON line — Cloud Run's logging agent parses this
    # as a structured log entry (jsonPayload).
    try:
        print(json.dumps(entry, default=str), file=sys.stdout, flush=True)
    except Exception:
        # Never let metric emission break the task pipeline
        logger.debug("Failed to emit metric %s", metric_name)


def emit_task_completed(
    task_id: str,
    scheme_id: str,
    final_status: str,
    cost_usd: float,
    wall_clock_seconds: float,
    model_name: str = "",
) -> None:
    """Emit a structured metric for task completion."""
    emit_metric(
        "task.completed",
        labels={
            "task_id": task_id,
            "scheme_id": scheme_id,
            "final_status": final_status,
            "model_name": model_name,
        },
        value=cost_usd,
    )
    emit_metric(
        "task.duration_seconds",
        labels={"task_id": task_id, "scheme_id": scheme_id},
        value=wall_clock_seconds,
    )


def emit_cost_exceeded(task_id: str, estimated_cost: float, ceiling: float) -> None:
    """Emit a structured metric when a task exceeds its cost ceiling."""
    emit_metric(
        "cost.exceeded",
        labels={"task_id": task_id, "ceiling_usd": ceiling},
        value=estimated_cost,
        severity="warning",
    )


def emit_operative_status(
    task_id: str,
    node_id: str,
    status: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Emit a structured metric for operative status changes."""
    emit_metric(
        "operative.status",
        labels={
            "task_id": task_id,
            "node_id": node_id,
            "status": status,
        },
    )
    if input_tokens or output_tokens:
        emit_metric(
            "operative.tokens",
            labels={"task_id": task_id, "node_id": node_id},
            value=float(input_tokens + output_tokens),
        )


def emit_watchdog_event(stalled_count: int, recovered: int, escalated: int) -> None:
    """Emit a structured metric for watchdog sweep results."""
    emit_metric(
        "watchdog.sweep",
        labels={
            "stalled_found": stalled_count,
            "recovered": recovered,
            "escalated": escalated,
        },
        value=float(stalled_count),
        severity="warning" if stalled_count > 0 else "info",
    )
