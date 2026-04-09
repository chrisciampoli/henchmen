"""Dispatch service - FastAPI Cloud Run HTTP handler for task intake routing."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import signal
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from henchmen.config.settings import Environment, get_settings
from henchmen.dispatch.handlers.cli import handle_cli_request
from henchmen.dispatch.handlers.github import handle_github_webhook
from henchmen.dispatch.handlers.jira import handle_jira_webhook
from henchmen.dispatch.handlers.slack import handle_slack_event
from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc
from henchmen.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiting middleware
# ---------------------------------------------------------------------------

# Maximum requests per window per client IP
_RATE_LIMIT = 60
_RATE_WINDOW_SECONDS = 60


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory sliding-window rate limiter per client IP.

    Applies to /webhooks/* and /api/v1/* paths only.
    """

    def __init__(self, app: FastAPI) -> None:
        super().__init__(app)
        self._requests: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if not (path.startswith("/webhooks/") or path.startswith("/api/v1/")):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window_start = now - _RATE_WINDOW_SECONDS

        # Prune old entries and check limit
        timestamps = self._requests[client_ip]
        self._requests[client_ip] = [t for t in timestamps if t > window_start]

        if len(self._requests[client_ip]) >= _RATE_LIMIT:
            logger.warning(
                "[rate-limit] %s exceeded %d req/%ds on %s",
                client_ip,
                _RATE_LIMIT,
                _RATE_WINDOW_SECONDS,
                path,
            )
            return Response(
                content=json.dumps({"detail": "Rate limit exceeded"}),
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(_RATE_WINDOW_SECONDS)},
            )

        self._requests[client_ip].append(now)
        return await call_next(request)


# ---------------------------------------------------------------------------
# Webhook signature verification helpers
# ---------------------------------------------------------------------------


def _verify_github_signature(body: bytes, signature_header: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature.

    GitHub sends the signature in the X-Hub-Signature-256 header as
    ``sha256=<hex-digest>``.
    """
    if not signature_header or not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _verify_slack_signature(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    """Verify Slack request signature.

    Slack signs requests using ``v0=HMAC-SHA256(signing_secret, 'v0:{ts}:{body}')``.
    Also rejects requests older than 5 minutes to prevent replay attacks.
    """
    if not timestamp or not signature or not secret:
        return False

    # Reject stale requests (replay protection)
    try:
        if abs(time.time() - float(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _verify_jira_signature(body: bytes, signature_header: str, secret: str) -> bool:
    """Verify Jira webhook HMAC-SHA256 signature.

    Atlassian sends the signature in the X-Atlassian-Webhook-Signature header as
    ``sha256=<hex-digest>``.
    """
    if not signature_header or not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _require_signing_secret(
    env: Environment,
    secret: str,
    *,
    integration: str,
) -> None:
    """Raise 401 if a signing secret is required but missing.

    Fail-closed policy: STAGING and PROD must have a signing secret configured.
    DEV tolerates missing secrets for local iteration but logs a warning.
    """
    if secret:
        return
    if env in (Environment.STAGING, Environment.PROD):
        logger.error(
            "[%s] Refusing request: signing secret is not configured in %s environment",
            integration,
            env.value,
        )
        raise HTTPException(
            status_code=401,
            detail=f"{integration} webhook signing secret is not configured",
        )
    logger.warning(
        "[%s] Signing secret is empty; accepting request in %s environment only",
        integration,
        env.value,
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan handler for graceful shutdown."""
    loop = asyncio.get_running_loop()

    def _sigterm_handler() -> None:
        logger.info("[dispatch] SIGTERM received, initiating graceful shutdown")

    try:
        loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)
    except (NotImplementedError, RuntimeError):
        pass  # Windows or non-main thread (e.g., test runner)

    from henchmen.observability.tracing import init_tracing, instrument_fastapi, shutdown_tracing

    settings = get_settings()
    init_tracing("dispatch", project_id=settings.gcp_project_id)
    instrument_fastapi(app)

    registry = ProviderRegistry(settings)
    app.state.message_broker = registry.get_message_broker()

    logger.info("[dispatch] Service started")
    yield
    shutdown_tracing()
    logger.info("[dispatch] Shutting down")


app = FastAPI(title="Henchmen Dispatch", description="Task intake router", lifespan=lifespan)
app.add_middleware(RateLimitMiddleware)  # type: ignore[arg-type]

_normalizer = TaskNormalizer()


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/api/v1/tasks")
async def create_task(request: Request) -> dict[str, Any]:
    """CLI handler - accepts JSON task creation requests."""
    settings = get_settings()
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    if "title" not in data:
        raise HTTPException(status_code=422, detail="'title' is required")

    return await handle_cli_request(data, _normalizer, settings, broker=request.app.state.message_broker)


@app.post("/webhooks/slack")
async def slack_webhook(request: Request) -> dict[str, Any]:
    """Slack event webhook endpoint with request signature verification."""
    settings = get_settings()
    body = await request.body()

    # Slack URL verification challenge must work even without signing secret
    # configured (initial setup flow).
    try:
        payload = json.loads(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    # Verify Slack request signature (fail-closed in staging/prod).
    _require_signing_secret(settings.environment, settings.slack_signing_secret, integration="slack")
    if settings.slack_signing_secret:
        ts = request.headers.get("X-Slack-Request-Timestamp", "")
        sig = request.headers.get("X-Slack-Signature", "")
        if not _verify_slack_signature(body, ts, sig, settings.slack_signing_secret):
            logger.warning("[slack] Invalid signature from %s", request.client.host if request.client else "unknown")
            raise HTTPException(status_code=401, detail="Invalid Slack signature")

    return await handle_slack_event(payload, _normalizer, settings, broker=request.app.state.message_broker)


@app.post("/webhooks/github")
async def github_webhook(request: Request) -> dict[str, Any]:
    """GitHub App webhook endpoint with HMAC-SHA256 signature verification."""
    settings = get_settings()
    body = await request.body()

    # Verify GitHub webhook signature (fail-closed in staging/prod).
    _require_signing_secret(settings.environment, settings.github_webhook_secret, integration="github")
    if settings.github_webhook_secret:
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_github_signature(body, sig, settings.github_webhook_secret):
            logger.warning("[github] Invalid signature from %s", request.client.host if request.client else "unknown")
            raise HTTPException(status_code=401, detail="Invalid GitHub signature")

    try:
        payload = json.loads(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    return await handle_github_webhook(payload, _normalizer, settings, broker=request.app.state.message_broker)


@app.post("/webhooks/jira")
async def jira_webhook(request: Request) -> dict[str, Any]:
    """Jira webhook endpoint with HMAC-SHA256 signature verification."""
    settings = get_settings()
    body = await request.body()

    # Verify Jira webhook signature (fail-closed in staging/prod).
    _require_signing_secret(settings.environment, settings.jira_webhook_secret, integration="jira")
    if settings.jira_webhook_secret:
        sig = request.headers.get("X-Atlassian-Webhook-Signature", "")
        if not _verify_jira_signature(body, sig, settings.jira_webhook_secret):
            logger.warning("[jira] Invalid signature from %s", request.client.host if request.client else "unknown")
            raise HTTPException(status_code=401, detail="Invalid Jira signature")

    try:
        payload = json.loads(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    return await handle_jira_webhook(payload, _normalizer, settings, broker=request.app.state.message_broker)


@app.post("/pubsub/task-planned")
async def task_planned_handler(request: Request) -> dict[str, Any]:
    """Pub/Sub push handler for task status updates."""
    settings = get_settings()
    await verify_pubsub_oidc(request, settings)
    try:
        envelope = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    message = envelope.get("message", {})
    data_b64 = message.get("data", "")
    try:
        data = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except Exception:
        logger.exception("Failed to decode task-planned Pub/Sub message data")
        data = {}

    logger.info("task-planned event received: %s", data)
    return {"status": "ok", "data": data}
