"""OIDC token verification for Pub/Sub push endpoints.

Every ``/pubsub/*`` HTTP handler in Henchmen is reached by Google Pub/Sub via
an authenticated push subscription. Pub/Sub includes an OIDC ID token in the
``Authorization`` header whose audience matches the subscription's configured
audience. Cloud Run's ``--no-allow-unauthenticated`` + IAM invoker policy
normally enforces this at the edge, but two realistic failure modes make
in-app verification worth having:

1. An operator debugging a staging environment grants ``allUsers`` the
   ``roles/run.invoker`` role "just for a minute" and forgets to revoke it.
2. Terraform drift strips the IAM binding during an apply that was intended
   to touch something unrelated.

Both scenarios would expose every ``/pubsub/*`` handler to arbitrary internet
callers. This module closes that gap by verifying the ID token inside the
handler, so the app fails closed regardless of edge policy.

The verifier:

- reads the ``Authorization: Bearer <jwt>`` header
- uses ``google.oauth2.id_token.verify_oauth2_token`` to validate the signature
- checks the ``aud`` claim matches the configured audience (the service URL)
- optionally checks the ``email`` claim is in an allow-list of publisher SAs
- in DEV on a repository checkout, logs a warning and allows the request
  through if verification is not configured (so that local
  ``docker-compose`` and the in-memory broker continue to work) — never on a
  desktop (data-directory) install; see :mod:`henchmen.config.posture`
- in STAGING/PROD, and on every desktop install, any verification failure raises 401
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect

from henchmen.config.internal_auth import InternalAuth, desktop_internal_auth
from henchmen.config.posture import fail_open_allowed

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

# A real OperativeReport, base64-encoded inside a Pub/Sub-style envelope, is
# normally a few KB; ``git_diff`` can be sizable for a large change. 4 MiB is
# generous headroom for that while still bounding memory use per request.
MAX_OPERATIVE_REPORT_BYTES = 4 * 1024 * 1024

# HMAC-SHA256 hex digest: exactly 64 lowercase hex characters. Anything else
# is rejected before the body is ever touched, so a caller without this shape
# of bearer (and not the push token) cannot make the server read or parse an
# arbitrarily large body.
_TASK_TOKEN_FORMAT = re.compile(r"^[0-9a-f]{64}$")


def _split_bearer(header_value: str | None) -> str | None:
    """Extract the bearer token from an Authorization header value."""
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _load_desktop_internal_auth() -> InternalAuth | None:
    """``desktop_internal_auth()``, turned into a 503 (never an unhandled 500) on a secrets I/O failure.

    A permissions problem or a full disk while reading or creating the internal secret files under
    ``<data dir>/secrets`` must fail closed inside a request, not surface as a raw traceback. The
    log line names only the exception -- a path/errno message -- which never carries secret content;
    the secret bytes themselves are never logged here or anywhere else.
    """
    try:
        return desktop_internal_auth()
    except OSError as exc:
        logger.error("[pubsub-auth] Could not load internal push credentials: %s", exc)
        raise HTTPException(status_code=503, detail="Internal credentials unavailable") from exc


def local_push_auth(settings: Settings) -> InternalAuth | None:
    """This install's internal push credentials, for a desktop install on any broker provider.

    A thin alias of :func:`desktop_internal_auth` (via :func:`_load_desktop_internal_auth`), kept so
    callers that already carry a ``Settings`` instance (:func:`verify_pubsub_oidc`) have a single
    name to call. ``settings`` is accepted only for that interface convenience and is never
    consulted: ``henchmen serve`` always wires the shared in-memory broker as the transport for
    every desktop install's ``/pubsub/*`` pushes, whatever ``message_broker_provider`` resolves to
    in ``henchmen.env`` -- a desktop install naming a cloud broker there does not mean Google
    Pub/Sub, rather than this process's own broker, is what actually delivers the push. So a
    desktop install accepts *only* the internal push token here, never OIDC or the DEV fail-open,
    regardless of its broker setting (controller ruling).
    """
    return _load_desktop_internal_auth()


async def require_internal_caller(request: Request) -> None:
    """FastAPI dependency guarding Henchmen's own maintenance routes (amendment A8).

    In the cloud these routes (watchdog, DLQ check, cleanup, merge-queue tick)
    are invoked by Cloud Scheduler behind Cloud Run IAM, which this dependency
    leaves unchanged. On *every* desktop install -- whatever the message
    broker provider resolves to -- anyone who can reach the port can otherwise
    call them, so the check is based on :func:`desktop_internal_auth` directly
    rather than on :func:`local_push_auth`. Without a data directory this is a
    no-op.
    """
    internal = _load_desktop_internal_auth()
    if internal is None:
        return
    if not internal.verify_push_token(_split_bearer(request.headers.get("Authorization"))):
        logger.warning(
            "[pubsub-auth] Desktop install: refusing maintenance request without the internal push token from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid internal token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def verify_pubsub_oidc(request: Request, settings: Settings) -> None:
    """Verify the OIDC bearer token on a Pub/Sub push request.

    Raises :class:`fastapi.HTTPException` (401) on any verification failure
    in STAGING/PROD. In DEV, logs a warning and returns without raising so
    that local dev loops (in-memory broker, docker-compose) keep working.

    Settings consumed:

    - ``pubsub_oidc_audience`` — expected ``aud`` claim. If empty in DEV, the
      check is skipped; if empty in STAGING/PROD, raises 401.
    - ``pubsub_oidc_allowed_emails`` — optional comma-separated allow-list of
      publisher service-account emails. If set, the token's ``email`` claim
      must match one of the entries.
    """
    audience = getattr(settings, "pubsub_oidc_audience", "") or ""
    allowed_raw = getattr(settings, "pubsub_oidc_allowed_emails", "") or ""
    allowed_emails = {e.strip() for e in allowed_raw.split(",") if e.strip()}
    env = settings.environment

    token = _split_bearer(request.headers.get("Authorization"))

    # Any desktop install, whatever the message broker provider resolves to:
    # the only valid caller is this server's own shared broker, which carries
    # the internal push token. No OIDC, no fail-open, and an operative's task
    # token is not accepted here.
    internal = local_push_auth(settings)
    if internal is not None:
        if internal.verify_push_token(token):
            request.state.pubsub_internal_caller = True
            return
        logger.warning(
            "[pubsub-auth] Desktop install: refusing /pubsub/* request without the internal push token from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=401, detail="Missing or invalid internal push token")

    # Development escape hatch: in DEV, if no audience is configured and no
    # token is present, we assume the caller is the local in-memory broker
    # and log a loud warning. STAGING/PROD never take this path.
    if fail_open_allowed(settings) and not audience and not token:
        logger.warning(
            "[pubsub-auth] DEV mode: allowing unauthenticated /pubsub/* request from %s — "
            "configure HENCHMEN_PUBSUB_OIDC_AUDIENCE to enforce verification",
            request.client.host if request.client else "unknown",
        )
        return

    # Everything past this point is fail-closed.
    if not audience:
        logger.error(
            "[pubsub-auth] HENCHMEN_PUBSUB_OIDC_AUDIENCE is not set in %s environment — refusing request",
            env.value,
        )
        raise HTTPException(status_code=401, detail="OIDC audience is not configured")

    if not token:
        logger.warning(
            "[pubsub-auth] Missing Authorization bearer token on /pubsub/* from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=401, detail="Missing OIDC bearer token")

    try:
        # Imported lazily so the module works in test environments without
        # google-auth installed.
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
    except ImportError as exc:
        logger.error("[pubsub-auth] google-auth not available: %s", exc)
        # In DEV we still let the request through with a warning.
        if fail_open_allowed(settings):
            logger.warning("[pubsub-auth] DEV mode: google-auth missing; skipping OIDC verification")
            return
        raise HTTPException(status_code=500, detail="OIDC verifier unavailable") from exc

    try:
        # google-auth ships without py.typed, so the call is untyped.
        claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            token, google_requests.Request(), audience
        )
    except ValueError as exc:
        logger.warning("[pubsub-auth] OIDC verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid OIDC token") from exc

    if allowed_emails:
        email = claims.get("email", "")
        if email not in allowed_emails:
            logger.warning(
                "[pubsub-auth] Rejecting /pubsub/* request from email '%s' — not in allow-list",
                email,
            )
            raise HTTPException(status_code=401, detail="OIDC email not authorized")

    # Success: attach claims to the request so downstream handlers can log them.
    request.state.pubsub_oidc_claims = claims


async def _read_capped_body(request: Request, max_bytes: int) -> None:
    """Read *request*'s body from the raw ASGI stream, rejecting past ``max_bytes``.

    Reading via :meth:`Request.stream` rather than :meth:`Request.body` means an
    oversized body is rejected as soon as more than ``max_bytes`` has arrived,
    without ever buffering the rest -- a lying or absent ``Content-Length``
    cannot force unbounded memory use. The bytes read so far are then stashed
    on ``request._body``, which is exactly the attribute Starlette's own
    ``Request.body()``/``Request.json()`` check first and populate themselves;
    setting it here means every later read in this request -- the handler's
    own included -- returns the same bytes from that cache instead of trying
    (and failing) to re-consume the now-exhausted ASGI receive channel.

    A client that disconnects mid-upload is not a server error: Starlette
    surfaces that as :class:`~starlette.requests.ClientDisconnect` from the
    stream, which is turned into a 400 here (logged at debug, since a peer
    hanging up is routine and not evidence of an attack) instead of
    propagating as an unhandled exception -- this call happens before the
    handler's own ``try:``, so an uncaught exception here would otherwise
    surface as a 500.
    """
    if hasattr(request, "_body"):
        return
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                logger.warning(
                    "[pubsub-auth] Desktop install: refusing an operative report body over %d bytes from %s",
                    max_bytes,
                    request.client.host if request.client else "unknown",
                )
                raise HTTPException(status_code=401, detail="Operative report body too large")
            chunks.append(chunk)
    except ClientDisconnect as exc:
        logger.debug(
            "[pubsub-auth] Client disconnected while streaming an operative report body from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=400, detail="Client disconnected") from exc
    request._body = b"".join(chunks)


async def _reported_task_id(request: Request) -> str:
    """The ``task_id`` inside a Pub/Sub-style envelope, or ``""`` when it cannot be read.

    Envelope parsing is untrusted input: bad base64, bad JSON, a missing or
    non-string ``task_id``, or any other decoding failure all fall through to
    ``""`` (never a raw exception) so the caller fails closed with a 401
    instead of a 500. Starlette caches the body, so this read does not
    prevent the handler from parsing it again afterwards.
    """
    try:
        envelope = await request.json()
        data = json.loads(base64.b64decode(envelope["message"]["data"], validate=True).decode("utf-8"))
    except Exception:
        return ""
    task_id = data.get("task_id") if isinstance(data, dict) else None
    return task_id if isinstance(task_id, str) else ""


async def verify_operative_report(request: Request, settings: Settings) -> None:
    """Authenticate a POST to ``/pubsub/operative-complete``.

    Outside desktop local mode this is exactly :func:`verify_pubsub_oidc`. On a
    desktop install the report comes straight from an operative container,
    which holds only the token derived for its own task: it is accepted when it
    matches the ``task_id`` the report carries. The internal push token is also
    accepted (Mastermind's own broker still delivers reports that way in some
    paths). Anything else -- including an undecodable envelope -- is 401.

    The body is never read for a caller that cannot possibly be a legitimate
    operative: a missing bearer, or one that is neither the push token nor
    shaped like a task token (64 lowercase hex characters -- an HMAC-SHA256
    hex digest), is rejected before any body access at all. Once the shape
    checks out, an oversized body is rejected by ``Content-Length`` when
    present, and unconditionally by :func:`_read_capped_body` while streaming
    -- so a request with no credentials, or with a malformed one, can never
    make the server buffer or JSON-parse an unbounded body.
    """
    client_host = request.client.host if request.client else "unknown"
    internal = local_push_auth(settings)
    if internal is None:
        await verify_pubsub_oidc(request, settings)
        return
    token = _split_bearer(request.headers.get("Authorization"))
    if internal.verify_push_token(token):
        request.state.pubsub_internal_caller = True
        return
    if not token or not _TASK_TOKEN_FORMAT.fullmatch(token):
        logger.warning(
            "[pubsub-auth] Desktop install: refusing an operative report without a valid task token from %s",
            client_host,
        )
        raise HTTPException(status_code=401, detail="Missing or invalid operative task token")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > MAX_OPERATIVE_REPORT_BYTES:
            logger.warning(
                "[pubsub-auth] Desktop install: refusing an operative report declaring %s bytes from %s",
                content_length,
                client_host,
            )
            raise HTTPException(status_code=401, detail="Operative report body too large")

    await _read_capped_body(request, MAX_OPERATIVE_REPORT_BYTES)

    task_id = await _reported_task_id(request)
    if task_id and internal.verify_task_token(task_id, token):
        request.state.operative_task_id = task_id
        return
    logger.warning(
        "[pubsub-auth] Desktop install: refusing an operative report without a valid task token from %s",
        client_host,
    )
    raise HTTPException(status_code=401, detail="Missing or invalid operative task token")
