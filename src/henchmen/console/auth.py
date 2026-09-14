"""Localhost-only access control for the Console.

Three layers, all required because the Console runs on a port any web page in
the user's browser can try to reach:

* **Host check** — rejects DNS-rebinding requests whose Host is not a loopback name.
* **Origin check** — state-changing requests must come from a loopback origin,
  so a malicious page cannot POST to the Console.
* **Session** — API calls need a cookie obtained by presenting the one-time
  setup token, which the launcher puts in the URL it opens.

Session cookies are HMAC-signed with a key kept in the data directory, so they
survive the restart that switches Henchmen from setup mode to run mode.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

SESSION_COOKIE = "henchmen_console"
_KEY_FILE_NAME = "console-session.key"
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _hostname(netloc: str) -> str:
    return (urlsplit(f"//{netloc}").hostname or "").lower()


def is_local_host(host_header: str | None) -> bool:
    """True when a Host header names this machine's loopback interface."""
    return bool(host_header) and _hostname(host_header or "") in _LOOPBACK_NAMES


def is_local_origin(origin: str | None) -> bool:
    """True when an Origin header is an http(s) loopback origin."""
    if not origin or origin == "null":
        return False
    parts = urlsplit(origin)
    return parts.scheme in {"http", "https"} and (parts.hostname or "").lower() in _LOOPBACK_NAMES


class ConsoleAuth:
    """Setup-token check plus signed, expiring session cookies."""

    def __init__(self, setup_token: str, signing_key: bytes, max_age_seconds: int = 30 * 24 * 3600) -> None:
        self.setup_token = setup_token
        self._key = signing_key
        self._max_age = max_age_seconds

    @classmethod
    def load(cls, secrets_dir: Path, setup_token: str | None) -> ConsoleAuth:
        """Load (or create) the signing key; use ``setup_token`` or generate one."""
        secrets_dir.mkdir(parents=True, exist_ok=True)
        key_path = secrets_dir / _KEY_FILE_NAME
        if key_path.is_file():
            key = key_path.read_bytes()
        else:
            key = secrets.token_bytes(32)
            key_path.write_bytes(key)
            if sys.platform != "win32":
                os.chmod(key_path, 0o600)
        return cls(setup_token=setup_token or secrets.token_urlsafe(32), signing_key=key)

    def check_setup_token(self, candidate: str) -> bool:
        """Constant-time comparison against the setup token."""
        return bool(candidate) and hmac.compare_digest(candidate.encode(), self.setup_token.encode())

    def _sign(self, issued_at: int) -> str:
        return hmac.new(self._key, f"console-session:{issued_at}".encode(), hashlib.sha256).hexdigest()

    def issue_session(self, now: float | None = None) -> str:
        """Return a new session cookie value."""
        issued_at = int(time.time() if now is None else now)
        return f"{issued_at}.{self._sign(issued_at)}"

    def verify_session(self, value: str | None, now: float | None = None) -> bool:
        """True when ``value`` was issued by this key and has not expired."""
        if not value or "." not in value:
            return False
        issued_raw, signature = value.split(".", 1)
        try:
            issued_at = int(issued_raw)
        except ValueError:
            return False
        if not hmac.compare_digest(signature, self._sign(issued_at)):
            return False
        current = time.time() if now is None else now
        return 0 <= current - issued_at <= self._max_age


class ConsoleGuard:
    """ASGI middleware enforcing host, origin and session checks on Console routes."""

    def __init__(self, app: ASGIApp, auth: ConsoleAuth, public_paths: frozenset[str]) -> None:
        self.app = app
        self.auth = auth
        self.public_paths = public_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        path: str = scope["path"]
        method: str = scope["method"]

        if not is_local_host(headers.get("host")):
            await _reject(send, 403, "The Henchmen Console only accepts requests addressed to this machine.")
            return
        if method not in _SAFE_METHODS and not is_local_origin(headers.get("origin")):
            await _reject(send, 403, "Cross-site request refused.")
            return
        if (
            path.startswith("/console/api/")
            and path not in self.public_paths
            and not self.auth.verify_session(_cookie(headers.get("cookie", ""), SESSION_COOKIE))
        ):
            await _reject(send, 401, "Open Henchmen from the link the Henchmen app gave you to sign in.")
            return
        await self.app(scope, receive, send)


def _cookie(header: str, name: str) -> str | None:
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


async def _reject(send: Send, status: int, message: str) -> None:
    body = json.dumps({"detail": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
