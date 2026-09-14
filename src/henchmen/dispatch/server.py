"""Dispatch service - FastAPI Cloud Run HTTP handler for task intake routing."""

import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import Scope

from henchmen.config.settings import Environment, get_settings
from henchmen.dispatch.api_models import CreateTaskRequest
from henchmen.dispatch.handlers.cli import handle_cli_request
from henchmen.dispatch.handlers.github import handle_github_webhook
from henchmen.dispatch.handlers.jira import handle_jira_webhook
from henchmen.dispatch.handlers.slack import handle_slack_event
from henchmen.dispatch.idempotency import TTLSet
from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiting middleware
# ---------------------------------------------------------------------------

# Maximum requests per window per client IP
_RATE_LIMIT = 60
_RATE_WINDOW_SECONDS = 60

# Path prefixes the limiter guards, matched against the ROUTE-relative path so
# the limiter still works when the app is mounted under a prefix (as
# ``henchmen serve`` does at /dispatch).
_RATE_LIMITED_PREFIXES = ("/webhooks/", "/api/v1/", "/pubsub/")


def _route_path(scope: Scope) -> str:
    """Return the request path with any mount prefix (``root_path``) removed."""
    path: str = scope.get("path", "")
    root_path: str = scope.get("root_path", "")
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return "/"
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory sliding-window rate limiter per client IP.

    Applies to ``/webhooks/*``, ``/api/v1/*`` and ``/pubsub/*`` paths only.

    This is a best-effort abuse guard, not an authentication control: behind a
    proxy the bucket key comes from ``X-Forwarded-For``, which a caller can
    spoof. Authenticity is enforced by the HMAC signature checks and by
    :func:`~henchmen.dispatch.pubsub_auth.verify_pubsub_oidc`. State is
    per-process, so a multi-instance deployment gets ``limit`` requests per
    window per instance.
    """

    def __init__(
        self,
        app: FastAPI,
        limit: int = _RATE_LIMIT,
        window_seconds: int = _RATE_WINDOW_SECONDS,
        trust_forwarded_for: bool = True,
    ) -> None:
        super().__init__(app)
        self._limit = limit
        self._window_seconds = window_seconds
        self._trust_forwarded_for = trust_forwarded_for
        self._requests: dict[str, list[float]] = {}

    def _client_key(self, request: Request) -> str:
        """Return the bucket key for *request*.

        Behind Cloud Run (or any reverse proxy) every request appears to come
        from the proxy, which would put all inbound webhook traffic into one
        bucket. Prefer the left-most ``X-Forwarded-For`` entry when present.
        """
        if self._trust_forwarded_for:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                first = forwarded.split(",")[0].strip()
                if first:
                    return first
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = _route_path(request.scope)
        if not path.startswith(_RATE_LIMITED_PREFIXES):
            return await call_next(request)

        client_ip = self._client_key(request)
        now = time.monotonic()
        window_start = now - self._window_seconds

        # Prune old entries and check limit
        timestamps = [t for t in self._requests.get(client_ip, []) if t > window_start]

        if len(timestamps) >= self._limit:
            self._requests[client_ip] = timestamps
            logger.warning(
                "[rate-limit] %s exceeded %d req/%ds on %s",
                client_ip,
                self._limit,
                self._window_seconds,
                path,
            )
            return Response(
                content=json.dumps({"detail": "Rate limit exceeded"}),
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(self._window_seconds)},
            )

        timestamps.append(now)
        self._requests[client_ip] = timestamps
        self._evict_idle(window_start)
        return await call_next(request)

    def _evict_idle(self, window_start: float) -> None:
        """Drop buckets whose most recent request fell out of the window."""
        stale = [key for key, stamps in self._requests.items() if not stamps or stamps[-1] <= window_start]
        for key in stale:
            del self._requests[key]


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

    # Built as bytes: a non-UTF-8 body must fail the HMAC check, not raise.
    sig_basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), sig_basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _verify_jira_signature(body: bytes, signature_header: str, secret: str) -> bool:
    """Verify Jira webhook HMAC-SHA256 signature.

    Jira Cloud sends the signature in the X-Hub-Signature header as
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
    """FastAPI lifespan: wire providers, start Slack Socket Mode, drain on exit.

    Signal handling is left to uvicorn: installing an ``asyncio``
    SIGTERM handler here would replace uvicorn's own ``handle_exit`` and the
    server would never shut down on SIGTERM.
    """
    from henchmen.observability.tracing import init_tracing, instrument_fastapi, shutdown_tracing

    settings = get_settings()
    init_tracing("dispatch", project_id=settings.gcp_project_id)
    instrument_fastapi(app)

    for problem in settings.validate_for_runtime():
        logger.warning("[dispatch] Configuration problem: %s", problem)

    registry = ProviderRegistry(settings)
    app.state.message_broker = registry.get_message_broker()

    from henchmen.dispatch.slack_bot import start_socket_mode

    app.state.slack_socket_handler = start_socket_mode(settings)

    logger.info("[dispatch] Service started")
    yield
    handler = getattr(app.state, "slack_socket_handler", None)
    if handler is not None:
        try:
            handler.close()
        except Exception:  # pragma: no cover - shutdown best effort
            logger.warning("[dispatch] Slack Socket Mode handler did not close cleanly", exc_info=True)
    shutdown_tracing()
    logger.info("[dispatch] Shutting down")


app = FastAPI(title="Henchmen Dispatch", description="Task intake router", lifespan=lifespan)
app.add_middleware(RateLimitMiddleware)  # type: ignore[arg-type]

_normalizer = TaskNormalizer()

# Replay guard for webhook redeliveries (see henchmen.dispatch.idempotency).
_delivery_guard = TTLSet()


def _duplicate_response(kind: str, key: str) -> dict[str, Any]:
    logger.info("[%s] Ignoring duplicate delivery %s", kind, key)
    return {"status": "ignored", "reason": "duplicate delivery", "dedup_key": key}


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/api/v1/tasks")
async def create_task(payload: CreateTaskRequest, request: Request) -> dict[str, Any]:
    """CLI handler - accepts JSON task creation requests."""
    settings = get_settings()
    repo = payload.repo or settings.github_default_repo
    if not repo:
        raise HTTPException(
            status_code=422,
            detail="'repo' is required (or set HENCHMEN_GITHUB_DEFAULT_REPO)",
        )
    payload = payload.model_copy(update={"repo": repo})

    return await handle_cli_request(payload, _normalizer, settings, broker=request.app.state.message_broker)


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
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Slack payload must be a JSON object")

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

    # Slack retries an event it did not get a 200 for within 3s.
    retry_num = request.headers.get("X-Slack-Retry-Num", "")
    if retry_num:
        logger.info("[slack] Ignoring retry #%s of event %s", retry_num, payload.get("event_id", ""))
        return {"status": "ignored", "reason": "slack retry"}

    dedup_key = f"slack:{payload['event_id']}" if payload.get("event_id") else ""
    if dedup_key and not _delivery_guard.add_if_absent(dedup_key):
        return _duplicate_response("slack", dedup_key)

    return await handle_slack_event(
        payload,
        _normalizer,
        settings,
        broker=request.app.state.message_broker,
        dedup_key=dedup_key or None,
    )


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
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="GitHub payload must be a JSON object")

    delivery = request.headers.get("X-GitHub-Delivery", "")
    dedup_key = f"github:{delivery}" if delivery else ""
    if dedup_key and not _delivery_guard.add_if_absent(dedup_key):
        return _duplicate_response("github", dedup_key)

    return await handle_github_webhook(
        payload,
        _normalizer,
        settings,
        broker=request.app.state.message_broker,
        dedup_key=dedup_key or None,
    )


@app.post("/webhooks/jira")
async def jira_webhook(request: Request) -> dict[str, Any]:
    """Jira webhook endpoint with HMAC-SHA256 signature verification."""
    settings = get_settings()
    body = await request.body()

    # Verify Jira webhook signature (fail-closed in staging/prod).
    _require_signing_secret(settings.environment, settings.jira_webhook_secret, integration="jira")
    if settings.jira_webhook_secret:
        # Jira Cloud sends X-Hub-Signature; the Atlassian-specific name is
        # kept as a fallback for older/self-hosted configurations.
        sig = request.headers.get("X-Hub-Signature", "") or request.headers.get("X-Atlassian-Webhook-Signature", "")
        if not _verify_jira_signature(body, sig, settings.jira_webhook_secret):
            logger.warning("[jira] Invalid signature from %s", request.client.host if request.client else "unknown")
            raise HTTPException(status_code=401, detail="Invalid Jira signature")

    try:
        payload = json.loads(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Jira payload must be a JSON object")

    dedup_key = _jira_dedup_key(payload)
    if dedup_key and not _delivery_guard.add_if_absent(dedup_key):
        return _duplicate_response("jira", dedup_key)

    return await handle_jira_webhook(
        payload,
        _normalizer,
        settings,
        broker=request.app.state.message_broker,
        dedup_key=dedup_key or None,
    )


def _jira_dedup_key(payload: dict[str, Any]) -> str:
    """Build a stable replay key for a Jira delivery.

    Jira Cloud stamps every delivery with ``timestamp`` (epoch ms); without it
    two legitimate transitions of the same issue would be indistinguishable
    from a redelivery, so dedup is skipped rather than guessed.
    """
    event = payload.get("webhookEvent", "")
    issue_key = (payload.get("issue") or {}).get("key", "")
    timestamp = payload.get("timestamp", "")
    if not (event and issue_key and timestamp):
        return ""
    return f"jira:{event}:{issue_key}:{timestamp}"
