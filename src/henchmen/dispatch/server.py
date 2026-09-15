"""Dispatch service - FastAPI Cloud Run HTTP handler for task intake routing."""

import hashlib
import hmac
import json
import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import Scope

from henchmen.config.paths import is_desktop_install
from henchmen.config.posture import fail_open_allowed
from henchmen.config.secret_files import tokens_match
from henchmen.config.settings import Settings, get_settings
from henchmen.dispatch.api_models import CreateTaskRequest
from henchmen.dispatch.handlers.cli import handle_cli_request
from henchmen.dispatch.handlers.github import handle_github_webhook
from henchmen.dispatch.handlers.jira import handle_jira_webhook
from henchmen.dispatch.handlers.slack import handle_slack_event
from henchmen.dispatch.idempotency import TTLSet
from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.dispatch.pubsub_auth import split_bearer
from henchmen.utils.lifespan import run_shutdown
from henchmen.utils.redaction import install_secret_redaction

logger = logging.getLogger(__name__)

# Redact token-shaped secrets in every log record this process emits: intake
# payloads (Slack events, webhook bodies) can carry them.
install_secret_redaction()

# ---------------------------------------------------------------------------
# Rate limiting middleware
# ---------------------------------------------------------------------------

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

    Any argument left as ``None`` comes from Settings
    (``HENCHMEN_DISPATCH_RATE_LIMIT_REQUESTS``,
    ``HENCHMEN_DISPATCH_RATE_LIMIT_WINDOW_SECONDS``,
    ``HENCHMEN_DISPATCH_TRUST_FORWARDED_FOR``), read on the first request so
    importing this module never needs a loadable configuration.
    """

    def __init__(
        self,
        app: FastAPI,
        limit: int | None = None,
        window_seconds: float | None = None,
        trust_forwarded_for: bool | None = None,
    ) -> None:
        super().__init__(app)
        self._limit_override = limit
        self._window_override = window_seconds
        self._trust_override = trust_forwarded_for
        self._configured = False
        self._limit = 0
        self._window_seconds = 0.0
        self._trust_forwarded_for = False
        self._requests: dict[str, list[float]] = {}

    def _configure(self) -> None:
        """Resolve every limit not passed to the constructor from Settings (once)."""
        if self._configured:
            return
        limit, window, trust = self._limit_override, self._window_override, self._trust_override
        if limit is None or window is None or trust is None:
            settings = get_settings()
            limit = settings.dispatch_rate_limit_requests if limit is None else limit
            window = settings.dispatch_rate_limit_window_seconds if window is None else window
            trust = settings.dispatch_trust_forwarded_for if trust is None else trust
        self._limit = limit
        self._window_seconds = float(window)
        self._trust_forwarded_for = trust
        self._configured = True

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

        self._configure()
        client_ip = self._client_key(request)
        now = time.monotonic()
        window_start = now - self._window_seconds

        # Prune old entries and check limit
        timestamps = [t for t in self._requests.get(client_ip, []) if t > window_start]

        if len(timestamps) >= self._limit:
            self._requests[client_ip] = timestamps
            logger.warning(
                "[rate-limit] %s exceeded %d req/%gs on %s",
                client_ip,
                self._limit,
                self._window_seconds,
                path,
            )
            return Response(
                content=json.dumps({"detail": "Rate limit exceeded"}),
                status_code=429,
                media_type="application/json",
                # Retry-After is an integer number of seconds (RFC 9110).
                headers={"Retry-After": str(max(1, math.ceil(self._window_seconds)))},
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
    settings: Settings,
    secret: str,
    *,
    integration: str,
) -> None:
    """Raise 401 if a signing secret is required but missing.

    Fail-closed policy: a signing secret is required unless
    ``fail_open_allowed(settings)`` (dev on a repository checkout), which
    tolerates a missing secret for local iteration but logs a warning. STAGING,
    PROD and every desktop (data-directory) install always refuse.
    """
    if secret:
        return
    if not fail_open_allowed(settings):
        logger.error(
            "[%s] Refusing request: signing secret is not configured (%s)",
            integration,
            _fail_closed_reason(settings),
        )
        raise HTTPException(
            status_code=401,
            detail=f"{integration} webhook signing secret is not configured",
        )
    logger.warning(
        "[%s] Signing secret is empty; accepting the request because fail-open is allowed (dev checkout)",
        integration,
    )


def _fail_closed_reason(settings: Settings) -> str:
    """Why a fail-open path is refused, for log lines: a desktop install, or the environment."""
    return "on a desktop install" if is_desktop_install() else f"in the {settings.environment.value} environment"


# ---------------------------------------------------------------------------
# REST intake authentication
# ---------------------------------------------------------------------------

# Set once the "no API token while fail-open is allowed" warning has been logged, so a busy local
# session does not log it on every request.
_open_api_warning_logged = False


async def require_api_token(request: Request) -> None:
    """FastAPI dependency guarding ``POST /api/v1/tasks`` with a bearer token.

    Every accepted request launches paid operative runs, so the route is
    fail-closed: with ``HENCHMEN_DISPATCH_API_TOKEN`` unset it is open only
    when ``fail_open_allowed(settings)`` is true (dev, and not a desktop
    install) -- with a one-time warning -- and returns 401 in STAGING, PROD
    and on every desktop install. The token is compared in constant time and
    never logged or echoed.
    """
    global _open_api_warning_logged
    settings = get_settings()
    # Settings already maps Terraform's seeded placeholder secret to empty.
    expected = settings.dispatch_api_token.strip()
    if not expected:
        if not fail_open_allowed(settings):
            logger.error(
                "[api] Refusing task creation: HENCHMEN_DISPATCH_API_TOKEN is not configured (%s)",
                _fail_closed_reason(settings),
            )
            raise HTTPException(status_code=401, detail="Dispatch API token is not configured")
        if not _open_api_warning_logged:
            logger.warning(
                "[api] HENCHMEN_DISPATCH_API_TOKEN is empty; /api/v1/tasks is unauthenticated because "
                "fail-open is allowed (dev checkout)"
            )
            _open_api_warning_logged = True
        return

    if not tokens_match(split_bearer(request.headers.get("Authorization")), expected):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
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
    if not fail_open_allowed(settings) and not settings.dispatch_api_token:
        logger.warning(
            "[dispatch] Configuration problem: HENCHMEN_DISPATCH_API_TOKEN is empty, so POST /api/v1/tasks "
            "returns 401 %s",
            _fail_closed_reason(settings),
        )

    # One broker for the whole process: every intake route and the Slack bot
    # publish through it. A broker injected on app.state beforehand belongs to
    # whoever injected it; one created here is closed and dropped on shutdown.
    #
    # Imported here (like mastermind's and forge's own lifespans), not at module level:
    # a module-level `from x import Y` binds a name in *this* module's namespace once,
    # the first time this module is imported -- a test that monkeypatches
    # `henchmen.providers.registry.ProviderRegistry` around a fresh import of this module
    # would then have that mock captured here permanently, immune to the patch being
    # undone afterward. A local import re-resolves the current attribute every call.
    from henchmen.providers.registry import ProviderRegistry

    owns_broker = getattr(app.state, "message_broker", None) is None
    if owns_broker:
        app.state.message_broker = ProviderRegistry(settings).get_message_broker()

    from henchmen.dispatch.slack_bot import start_socket_mode

    app.state.slack_socket_handler = start_socket_mode(settings, broker=app.state.message_broker)

    logger.info("[dispatch] Service started")

    async def _shutdown() -> None:
        handler = getattr(app.state, "slack_socket_handler", None)
        if handler is not None:
            try:
                handler.close()
            except Exception:  # pragma: no cover - shutdown best effort
                logger.warning("[dispatch] Slack Socket Mode handler did not close cleanly", exc_info=True)
        shutdown_tracing()
        logger.info("[dispatch] Shutting down")
        if owns_broker:
            await _close_broker(app)

    original: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        original = exc
        raise
    finally:
        # A sub-app entered after this one (mastermind, forge) can fail to start; the
        # combined app's AsyncExitStack then unwinds this lifespan by throwing that
        # exception in at `yield`, so shutdown must run from `finally`, not after a bare
        # `yield` -- otherwise the Slack Socket Mode handler (background threads) stays
        # connected for as long as the process (through needs-attention mode included),
        # still accepting Slack messages and publishing them to a broker nothing drains.
        # A shutdown-path error (a CancelledError included) never masks `original`.
        await run_shutdown("dispatch", _shutdown, original=original)


async def _close_broker(app: FastAPI) -> None:
    """Release the lifespan's broker (e.g. its Pub/Sub publisher) and drop it from ``app.state``."""
    broker = getattr(app.state, "message_broker", None)
    app.state.message_broker = None
    aclose = getattr(broker, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception as exc:
        logger.warning("[dispatch] Failed to close message broker: %s", exc)


app = FastAPI(title="Henchmen Dispatch", description="Task intake router", lifespan=lifespan)
# Limits come from HENCHMEN_DISPATCH_RATE_LIMIT_* on the first guarded request.
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


@app.post("/api/v1/tasks", dependencies=[Depends(require_api_token)])
async def create_task(payload: CreateTaskRequest, request: Request) -> dict[str, Any]:
    """CLI handler - accepts JSON task creation requests (bearer-token authenticated)."""
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
    _require_signing_secret(settings, settings.slack_signing_secret, integration="slack")
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
    _require_signing_secret(settings, settings.github_webhook_secret, integration="github")
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
    _require_signing_secret(settings, settings.jira_webhook_secret, integration="jira")
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
