"""Distributed tracing using OpenTelemetry + Cloud Trace.

Provides cross-service trace propagation through Pub/Sub message attributes
using W3C Trace Context format. Each service (Dispatch, Mastermind, Operative,
Forge) calls ``init_tracing()`` at startup to configure the exporter.

Usage:
    from henchmen.observability.tracing import init_tracing, get_tracer

    # In FastAPI lifespan:
    init_tracing("mastermind", project_id="${GCP_PROJECT_ID}")

    # In code:
    tracer = get_tracer()
    with tracer.start_as_current_span("handle_task") as span:
        span.set_attribute("task.id", task_id)
        ...

    # Pub/Sub propagation:
    attrs = inject_trace_context()  # Add to publish attributes
    extract_trace_context(attrs)    # Restore on receive
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_tracer_provider: Any = None


def init_tracing(service_name: str, project_id: str = "") -> None:
    """Initialize OpenTelemetry tracing with Cloud Trace exporter.

    Safe to call multiple times — only initializes once.
    No-ops gracefully if dependencies are unavailable.
    """
    global _tracer_provider
    if _tracer_provider is not None:
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

        _tracer_provider = TracerProvider(resource=resource)
        exporter = CloudTraceSpanExporter(project_id=project_id) if project_id else CloudTraceSpanExporter()  # type: ignore[no-untyped-call]
        _tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(_tracer_provider)

        logger.info("OpenTelemetry tracing initialized for %s", service_name)
    except ImportError:
        logger.info("OpenTelemetry not available, tracing disabled")
    except Exception as exc:
        logger.warning("Failed to initialize tracing: %s", exc)


def get_tracer(name: str = "henchmen") -> Any:
    """Get an OpenTelemetry tracer instance.

    Returns a no-op tracer if tracing is not initialized.
    """
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except ImportError:
        return _NoOpTracer()


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


def inject_trace_context() -> dict[str, str]:
    """Inject current trace context into a dict for Pub/Sub message attributes.

    Returns a dict with W3C traceparent/tracestate headers.
    """
    try:
        from opentelemetry.trace.propagation import get_current_span

        span = get_current_span()
        if not span.get_span_context().is_valid:
            return {}

        from opentelemetry.propagate import inject

        carrier: dict[str, str] = {}
        inject(carrier)
        return carrier
    except ImportError:
        return {}
    except Exception:
        return {}


def extract_trace_context(attributes: dict[str, str]) -> None:
    """Extract trace context from Pub/Sub message attributes and attach to current context.

    Call this at the start of a Pub/Sub message handler to link the
    downstream span to the upstream trace.
    """
    try:
        from opentelemetry import context
        from opentelemetry.propagate import extract

        ctx = extract(attributes)
        context.attach(ctx)
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("Failed to extract trace context: %s", exc)


def shutdown_tracing() -> None:
    """Flush and shut down the tracer provider."""
    global _tracer_provider
    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
        except Exception:
            pass
        _tracer_provider = None


class _NoOpTracer:
    """Fallback tracer when OpenTelemetry is not available."""

    def start_as_current_span(self, name: str, **kwargs: Any) -> "_NoOpSpan":
        return _NoOpSpan()

    def start_span(self, name: str, **kwargs: Any) -> "_NoOpSpan":
        return _NoOpSpan()


class _NoOpSpan:
    """No-op span that can be used as a context manager."""

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass

    def end(self) -> None:
        pass
