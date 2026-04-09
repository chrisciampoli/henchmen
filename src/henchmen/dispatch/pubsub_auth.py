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
- in DEV, logs a warning and allows the request through if verification is
  not configured (so that local ``docker-compose`` and the in-memory broker
  continue to work)
- in STAGING/PROD, any verification failure raises 401
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from henchmen.config.settings import Environment

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)


def _split_bearer(header_value: str | None) -> str | None:
    """Extract the bearer token from an Authorization header value."""
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


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

    # Development escape hatch: in DEV, if no audience is configured and no
    # token is present, we assume the caller is the local in-memory broker
    # and log a loud warning. STAGING/PROD never take this path.
    if env == Environment.DEV and not audience and not token:
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
        if env == Environment.DEV:
            logger.warning(
                "[pubsub-auth] DEV mode: google-auth missing; skipping OIDC verification"
            )
            return
        raise HTTPException(status_code=500, detail="OIDC verifier unavailable") from exc

    try:
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), audience)
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
