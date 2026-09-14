"""Localhost-only access control for the Console.

Three layers, all required because the Console runs on a port any web page in
the user's browser can try to reach:

* **Host check** — rejects DNS-rebinding requests whose Host is not a loopback name.
* **Origin check** — state-changing requests (and every WebSocket handshake) must
  come from a loopback origin whose port matches the Host, so a malicious page on
  another localhost port cannot ride the session cookie in cross-site requests.
* **Session** — API calls need a cookie obtained by presenting the one-time
  setup token, which the launcher puts in the URL it opens.

Session cookies are HMAC-signed with a key kept in the data directory, so they
survive the restart that switches Henchmen from setup mode to run mode. A
signing key file that is missing or too short (an empty file from a crash or a
full disk) is never trusted as-is: it is treated as absent and regenerated,
because using it as an HMAC key would let anyone forge a valid session.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

SESSION_COOKIE = "henchmen_console"
_KEY_FILE_NAME = "console-session.key"
_MIN_KEY_BYTES = 32
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _parse_netloc(netloc: str, default_scheme: str) -> tuple[str, int] | None:
    """Return ``(lowercase hostname, port)`` for a Host header or an Origin's netloc.

    Returns ``None`` for anything malformed (including an unparsable IPv6 host)
    or carrying userinfo (``user@host``), which a Host or Origin header must
    never legitimately contain.
    """
    if "@" in netloc:
        return None
    try:
        parts = urlsplit(f"//{netloc}")
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not hostname:
        return None
    return hostname.lower(), port if port is not None else _DEFAULT_PORTS.get(default_scheme, 80)


def _origin_parts(origin: str | None) -> tuple[str, int] | None:
    """Return ``(loopback hostname, port)`` for a valid http(s) loopback Origin, else ``None``."""
    if not origin or origin == "null":
        return None
    try:
        parts = urlsplit(origin)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"}:
        return None
    parsed = _parse_netloc(parts.netloc, default_scheme=parts.scheme)
    if parsed is None or parsed[0] not in _LOOPBACK_NAMES:
        return None
    return parsed


def _host_port(host_header: str, default_scheme: str) -> int | None:
    """Return the port named by a Host header, defaulting by ``default_scheme``."""
    parsed = _parse_netloc(host_header, default_scheme=default_scheme)
    return parsed[1] if parsed is not None else None


def is_local_host(host_header: str | None) -> bool:
    """True when a Host header names this machine's loopback interface."""
    if not host_header:
        return False
    parsed = _parse_netloc(host_header, default_scheme="http")
    return parsed is not None and parsed[0] in _LOOPBACK_NAMES


def is_local_origin(origin: str | None) -> bool:
    """True when an Origin header is an http(s) loopback origin."""
    return _origin_parts(origin) is not None


def _write_key_file(path: Path, key: bytes) -> None:
    """Create ``path`` atomically, mode 0600 from the start (harmless flag on Windows)."""
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)


class ConsoleAuth:
    """Setup-token check plus signed, expiring session cookies."""

    def __init__(self, setup_token: str, signing_key: bytes, max_age_seconds: int = 30 * 24 * 3600) -> None:
        self.setup_token = setup_token
        self._key = signing_key
        self._max_age = max_age_seconds

    @classmethod
    def load(cls, secrets_dir: Path, setup_token: str | None) -> ConsoleAuth:
        """Load (or create) the signing key; use ``setup_token`` or generate one.

        A missing, empty, or too-short key file is never trusted: it is
        regenerated (never logging the key material itself).
        """
        secrets_dir.mkdir(parents=True, exist_ok=True)
        key_path = secrets_dir / _KEY_FILE_NAME
        key: bytes | None = None
        file_exists = key_path.is_file()
        if file_exists:
            existing = key_path.read_bytes()
            if len(existing) >= _MIN_KEY_BYTES:
                key = existing
            else:
                logger.warning("Console session key at %s is missing or too short; regenerating it.", key_path)
        if key is None:
            key = secrets.token_bytes(32)
            if file_exists:
                tmp_path = key_path.with_name(f"{key_path.name}.{secrets.token_hex(8)}.tmp")
                _write_key_file(tmp_path, key)
                os.replace(tmp_path, key_path)
            else:
                _write_key_file(key_path, key)
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
        if not (issued_raw.isascii() and issued_raw.isdigit() and 1 <= len(issued_raw) <= 12):
            return False
        issued_at = int(issued_raw)
        try:
            provided = signature.encode("ascii")
            expected = self._sign(issued_at).encode("ascii")
        except UnicodeEncodeError:
            return False
        if not hmac.compare_digest(provided, expected):
            return False
        current = time.time() if now is None else now
        return 0 <= current - issued_at <= self._max_age


class ConsoleGuard:
    """ASGI middleware enforcing host, origin and session checks on Console routes.

    HTTP and WebSocket scopes are both guarded; only ``lifespan`` (and any other
    non-http, non-websocket scope) passes straight through. A WebSocket
    handshake always carries an Origin, so it is always checked (and always
    needs a session), unlike plain HTTP GETs which only need a local Host.
    """

    def __init__(self, app: ASGIApp, auth: ConsoleAuth, public_paths: frozenset[str]) -> None:
        self.app = app
        self.auth = auth
        self.public_paths = public_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        is_websocket = scope_type == "websocket"
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        path: str = scope["path"]

        if not is_local_host(headers.get("host")):
            message = "The Henchmen Console only accepts requests addressed to this machine."
            await _deny(send, is_websocket, 403, message)
            return

        method = scope.get("method") or "GET"
        needs_origin_check = is_websocket or method not in _SAFE_METHODS
        if needs_origin_check:
            origin_parts = _origin_parts(headers.get("origin"))
            host_port = _host_port(headers.get("host") or "", scope.get("scheme") or "http")
            if origin_parts is None or host_port is None or origin_parts[1] != host_port:
                await _deny(send, is_websocket, 403, "Cross-site request refused.")
                return

        needs_session = is_websocket or (path.startswith("/console/api/") and path not in self.public_paths)
        if needs_session and not self.auth.verify_session(_cookie(headers.get("cookie", ""), SESSION_COOKIE)):
            await _deny(send, is_websocket, 401, "Open Henchmen from the link the Henchmen app gave you to sign in.")
            return

        await self.app(scope, receive, send)


def _cookie(header: str, name: str) -> str | None:
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


async def _deny(send: Send, is_websocket: bool, status: int, message: str) -> None:
    if is_websocket:
        await _reject_websocket(send)
    else:
        await _reject(send, status, message)


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


async def _reject_websocket(send: Send) -> None:
    await send({"type": "websocket.close", "code": 1008})
