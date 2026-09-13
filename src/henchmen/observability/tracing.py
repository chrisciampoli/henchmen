"""Distributed tracing using OpenTelemetry + Cloud Trace.

Only FastAPI auto-instrumentation is wired: each service (Dispatch, Mastermind,
Operative, Forge) calls ``init_tracing()`` at startup and ``instrument_fastapi``
on its app. There is no Pub/Sub trace propagation — the helpers that claimed to
provide it were never called by any publisher or handler, so they were removed
rather than left as a promise the code does not keep.

Tracing exports to Cloud Trace and is therefore a GCP-only feature: on
``HENCHMEN_PROVIDER=local``/``aws`` ``init_tracing`` logs once and no-ops
instead of constructing a ``CloudTraceSpanExporter`` that fails
``google.auth.default()``.

Usage:
    from henchmen.observability.tracing import init_tracing, instrument_fastapi

    # In FastAPI lifespan:
    init_tracing("mastermind", project_id=settings.gcp_project_id)
    instrument_fastapi(app)
"""

import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

_tracer_provider: Any = None


def _tracing_provider_is_gcp() -> bool:
    """True when the deployment runs on GCP, where Cloud Trace is reachable."""
    try:
        from henchmen.config.settings import get_settings

        return get_settings().provider == "gcp"
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Tracing could not load Settings (%s); assuming tracing is disabled", exc)
        return False


def init_tracing(service_name: str, project_id: str = "") -> None:
    """Initialize OpenTelemetry tracing with the Cloud Trace exporter.

    Safe to call multiple times — only initializes once. No-ops with an info
    log when OpenTelemetry is not installed or the deployment is not on GCP.
    """
    global _tracer_provider
    if _tracer_provider is not None:
        return

    if not _tracing_provider_is_gcp():
        logger.info("Tracing disabled for %s: Cloud Trace export requires HENCHMEN_PROVIDER=gcp", service_name)
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": f"henchmen-{service_name}",
                "service.namespace": "henchmen",
                "cloud.provider": "gcp",
                "cloud.platform": "gcp_cloud_run",
            }
        )

        # Build the exporter BEFORE publishing the provider globally: assigning
        # _tracer_provider first meant a failing exporter left a half-configured
        # provider in place that the early return above could never retry.
        exporter = CloudTraceSpanExporter(project_id=project_id) if project_id else CloudTraceSpanExporter()  # type: ignore[no-untyped-call]
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer_provider = provider

        logger.info("OpenTelemetry tracing initialized for %s", service_name)
    except ImportError:
        logger.info("OpenTelemetry not available, tracing disabled")
    except Exception as exc:
        logger.warning("Failed to initialize tracing: %s", exc)


def instrument_fastapi(app: Any) -> None:
    """Add OpenTelemetry instrumentation to a FastAPI app."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        logger.info("FastAPI instrumented with OpenTelemetry")
    except ImportError:
        logger.debug("FastAPI OpenTelemetry instrumentation not available")
    except Exception as exc:
        logger.warning("Failed to instrument FastAPI: %s", exc)


def shutdown_tracing() -> None:
    """Flush and shut down the tracer provider."""
    global _tracer_provider
    if _tracer_provider is not None:
        with contextlib.suppress(Exception):
            _tracer_provider.shutdown()
        _tracer_provider = None
