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
import re
import secrets
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from henchmen.config.secret_files import (
    create_secret_file,
    ensure_secrets_dir,
    read_or_create_secret,
    replace_with_retry,
    sweep_stale_sibling_files,
    tokens_match,
    write_secret_file,
)

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "henchmen_console"
_KEY_FILE_NAME = "console-session.key"
SETUP_TOKEN_FILE_NAME = "setup-token"
_SEEDED_MARKER_SUFFIX = ".seeded"
_SETUP_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_FORBIDDEN_NETLOC_CHARS = frozenset("/?#\\")


def _parse_netloc(netloc: str, default_scheme: str) -> tuple[str, int] | None:
    """Return ``(lowercase hostname, port)`` for a Host header or an Origin's netloc.

    Returns ``None`` for anything malformed (including an unparsable IPv6 host),
    carrying userinfo (``user@host``), or containing ``/ ? # \\`` or whitespace
    — a bare Host or Origin netloc must never legitimately contain any of
    these, and ``urlsplit`` silently strips a path/query/fragment suffix
    (``127.0.0.1/x`` would otherwise parse as the bare, allowed hostname
    ``127.0.0.1``) rather than rejecting it.
    """
    if "@" in netloc or any(char in _FORBIDDEN_NETLOC_CHARS or char.isspace() for char in netloc):
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


def _host_header(scope: Scope) -> str | None:
    """The sole ``Host`` header value from an ASGI scope, or ``None`` when absent or duplicated.

    HTTP server implementations disagree about which of several ``Host``
    headers on one request they honor (some the first, some the last), so a
    request carrying more than one is never trusted as naming either value:
    it is treated the same as carrying no Host header at all, which every
    caller here already fails closed on.
    """
    values = [value.decode("latin-1") for key, value in scope["headers"] if key.lower() == b"host"]
    return values[0] if len(values) == 1 else None


def is_allowed_host(host_header: str | None, allowed_hostnames: frozenset[str]) -> bool:
    """True when a Host header names one of ``allowed_hostnames`` (any port; malformed or userinfo: False)."""
    if not host_header:
        return False
    parsed = _parse_netloc(host_header, default_scheme="http")
    return parsed is not None and parsed[0] in allowed_hostnames


def is_local_host(host_header: str | None) -> bool:
    """True when a Host header names this machine's loopback interface."""
    return is_allowed_host(host_header, _LOOPBACK_NAMES)


def desktop_allowed_hostnames(container_hostname: str) -> frozenset[str]:
    """Loopback names plus the container name operatives use on the private Docker network."""
    name = container_hostname.strip().lower()
    return _LOOPBACK_NAMES | {name} if name else _LOOPBACK_NAMES


def is_local_origin(origin: str | None) -> bool:
    """True when an Origin header is an http(s) loopback origin."""
    return _origin_parts(origin) is not None


class SetupTokenStore:
    """The one-time Console sign-in token, kept in ``<secrets>/setup-token``.

    Rotated on every process start and by ``henchmen console-link``; consumed
    by a successful exchange, which replaces it with a random value nobody
    holds (the file stays present, so the launcher's seed can never apply
    again). The file is re-read on every check, so a rotation made by another
    process takes effect immediately.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _seeded_marker(self) -> Path:
        """The permanent, never-deleted marker recording that a token was issued at least once.

        Its presence (not the token file's) is the source of truth for "has a seed already
        been used here", because the token file itself can legitimately disappear (a crash
        between the claim rename and the recreate in :meth:`consume`, a failed
        :func:`create_secret_file`, or an ``unlink`` race) without that meaning setup never
        ran. Trusting the token file's absence would let a lost file re-arm the launcher's
        seed and grant a session again, or let a second process racing the very first
        ``rotate`` call reuse the same seed.
        """
        return self.path.with_name(self.path.name + _SEEDED_MARKER_SUFFIX)

    def current(self) -> str | None:
        """The token a sign-in link must carry, or ``None`` when there is no usable token."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("Could not read the setup token file %s; treating it as absent.", self.path.name)
            return None
        token = raw.decode("ascii", errors="replace").strip()
        if not _SETUP_TOKEN_PATTERN.fullmatch(token):
            logger.warning("Setup token file %s has unusable content; treating it as absent.", self.path.name)
            return None
        return token

    def rotate(self, seed: str | None = None) -> str:
        """Issue a new token, invalidating any earlier link; ``seed`` is used only for the very first token."""
        with self._lock:
            ensure_secrets_dir(self.path.parent)
            sweep_stale_sibling_files(self.path, "claim")
            candidate = (seed or "").strip()
            valid_seed = bool(candidate) and _SETUP_TOKEN_PATTERN.fullmatch(candidate) is not None
            # The exclusive create below succeeds at most once, ever, for this path: that is
            # what makes "is this truly the first token" safe against a lost token file and
            # against a second process racing this same first call.
            try:
                create_secret_file(self._seeded_marker(), b"1")
                is_first_ever = True
            except FileExistsError:
                is_first_ever = False
            use_seed = valid_seed and is_first_ever
            if candidate and not use_seed:
                logger.info(
                    "Ignoring the setup token seed: it applies only to the first token ever "
                    "issued for this data directory and needs 32+ URL-safe characters."
                )
            token = candidate if use_seed else secrets.token_urlsafe(32)
            try:
                write_secret_file(self.path, token.encode("ascii"))
            except OSError:
                if not use_seed:
                    raise
                # The marker is already permanently created, so the seed can never be
                # retried anyway; fall back to a random token rather than leaving no
                # usable token file at all. If this second write also fails, propagate
                # it as before -- there is nothing left to fall back to.
                logger.warning(
                    "Could not write the seeded setup token to %s; falling back to a random token.",
                    self.path.name,
                )
                token = secrets.token_urlsafe(32)
                write_secret_file(self.path, token.encode("ascii"))
            return token

    def consume(self, candidate: str) -> bool:
        """Accept ``candidate`` at most once; any race or I/O problem fails closed."""
        if not candidate:
            return False
        with self._lock:
            sweep_stale_sibling_files(self.path, "claim")
            current = self.current()
            if current is None or not tokens_match(candidate, current):
                return False
            claim = self.path.with_name(f"{self.path.name}.{secrets.token_hex(8)}.claim")
            try:
                replace_with_retry(self.path, claim)
            except OSError:
                # Another process claimed or rotated it first; never the token value itself.
                logger.info("Setup token claim on %s lost a race or failed; failing closed.", self.path.name)
                return False
            # The rename preserves the original file's mtime, which can already be older
            # than the stale-file threshold; refresh it to "now" so a concurrent sweep
            # (this process's own next call, or another process's) never mistakes this
            # in-flight claim for an abandoned one while we are still working with it.
            with suppress(OSError):
                os.utime(claim, None)
            try:
                claimed = claim.read_bytes().decode("ascii", errors="replace").strip()
            except OSError:
                logger.warning("Could not read the claimed setup token file %s; failing closed.", claim.name)
                claimed = ""
            matched = tokens_match(candidate, claimed)
            try:
                if matched:
                    # Consumed: replace with a fresh random token nobody holds yet.
                    try:
                        create_secret_file(self.path, secrets.token_urlsafe(32).encode("ascii"))
                    except FileExistsError:
                        pass
                    except OSError:
                        logger.warning("Could not write the replacement setup token to %s.", self.path.name)
                        raise
                else:
                    # candidate did not match what we actually claimed: most likely another
                    # process rotated the token between our current() read and the claim rename
                    # above, so what we hold in `claim` is that newer token, not a spent one.
                    # Restore it instead of discarding it under a fresh random value.
                    try:
                        os.link(str(claim), str(self.path))
                    except FileExistsError:
                        pass  # a newer token is already back in place; nothing to restore
                    except OSError:
                        logger.warning(
                            "Could not restore setup token %s after a failed claim; failing closed.", self.path.name
                        )
            finally:
                # However the write above went, the claim file must never linger:
                # it holds a real token value and this is the one guaranteed chance
                # to remove it before returning (or propagating a write failure).
                try:
                    claim.unlink(missing_ok=True)
                except OSError:
                    logger.debug("Could not remove claim file %s; a later sweep will remove it.", claim.name)
            return matched


class ConsoleAuth:
    """Setup-token check plus signed, expiring session cookies."""

    def __init__(
        self,
        setup_token: str,
        signing_key: bytes,
        max_age_seconds: int = 30 * 24 * 3600,
        token_store: SetupTokenStore | None = None,
    ) -> None:
        self._memory_token = setup_token
        self._token_store = token_store
        self._key = signing_key
        self._max_age = max_age_seconds

    @property
    def setup_token(self) -> str:
        """The current one-time sign-in token (``""`` once consumed)."""
        if self._token_store is not None:
            return self._token_store.current() or ""
        return self._memory_token

    @property
    def max_age_seconds(self) -> int:
        """How long a session cookie stays valid after it is issued."""
        return self._max_age

    @classmethod
    def load(cls, secrets_dir: Path, setup_token: str | None) -> ConsoleAuth:
        """Load (or create) the signing key and rotate the one-time setup token.

        ``setup_token`` (the launcher's ``HENCHMEN_CONSOLE_SETUP_TOKEN``) seeds
        only the very first token of this data directory; every later start
        issues a fresh random one, so a link from an earlier start stops working.
        """
        key = read_or_create_secret(secrets_dir / _KEY_FILE_NAME)
        token_store = SetupTokenStore(secrets_dir / SETUP_TOKEN_FILE_NAME)
        token = token_store.rotate(seed=setup_token)
        return cls(setup_token=token, signing_key=key, token_store=token_store)

    def check_setup_token(self, candidate: str) -> bool:
        """Constant-time comparison against the current token, without consuming it."""
        return tokens_match(candidate, self.setup_token)

    def consume_setup_token(self, candidate: str) -> bool:
        """Accept ``candidate`` once; a second use of the same token is refused."""
        if self._token_store is not None:
            return self._token_store.consume(candidate)
        if not self.check_setup_token(candidate):
            return False
        self._memory_token = ""
        return True

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
        host_header = _host_header(scope)

        if not is_local_host(host_header):
            message = "The Henchmen Console only accepts requests addressed to this machine."
            await _deny(send, is_websocket, 403, message)
            return

        method = scope.get("method") or "GET"
        needs_origin_check = is_websocket or method not in _SAFE_METHODS
        if needs_origin_check:
            origin_parts = _origin_parts(headers.get("origin"))
            host_port = _host_port(host_header or "", scope.get("scheme") or "http")
            if origin_parts is None or host_port is None or origin_parts[1] != host_port:
                await _deny(send, is_websocket, 403, "Cross-site request refused.")
                return

        needs_session = is_websocket or (path.startswith("/console/api/") and path not in self.public_paths)
        if needs_session and not self.auth.verify_session(_cookie(headers.get("cookie", ""), SESSION_COOKIE)):
            await _deny(send, is_websocket, 401, "Open Henchmen from the link the Henchmen app gave you to sign in.")
            return

        await self.app(scope, receive, send)


HEALTH_ONLY: frozenset[str] = frozenset({"/health"})


class HostAllowlistGuard:
    """ASGI middleware for the whole desktop app: refuse any Host that is not this machine or the container name.

    Blocks DNS rebinding against Dispatch, Mastermind and Forge — a page on
    ``attacker.example`` whose name resolves to 127.0.0.1 still sends
    ``Host: attacker.example`` — while operatives on the private Docker network
    reach ``http://henchmen:8000``. ``exempt_paths`` (``/health``) answer any
    Host. This is not authentication: operatives can send any Host, and the
    internal tokens authenticate them.
    """

    def __init__(
        self, app: ASGIApp, allowed_hostnames: frozenset[str], exempt_paths: frozenset[str] = HEALTH_ONLY
    ) -> None:
        self.app = app
        self.allowed_hostnames = allowed_hostnames
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type not in {"http", "websocket"} or scope["path"] in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        host_header = _host_header(scope)
        if not is_allowed_host(host_header, self.allowed_hostnames):
            logger.debug("Refusing request with disallowed Host %r (desktop allowlist).", host_header)
            message = "Henchmen only accepts requests addressed to this machine."
            await _deny(send, scope_type == "websocket", 403, message)
            return
        await self.app(scope, receive, send)


def forward_host_problem(settings: Settings) -> str | None:
    """A plain-language problem when operatives cannot reach this desktop install, else ``None``.

    A desktop install's whole-app Host allowlist (:class:`HostAllowlistGuard`)
    would silently refuse an operative that calls back on
    ``settings.local_forward_base`` if that hostname is not in
    :func:`desktop_allowed_hostnames`. The default ``local_forward_base``
    (``http://host.docker.internal:<port>``) is exactly this case, so this
    surfaces it instead of leaving operatives to fail with an opaque 403.

    A loopback hostname (``127.0.0.1``, ``localhost``, ``::1``) is also a
    problem, unconditionally: :class:`~henchmen.providers.local.docker.DockerOrchestrator`
    never runs an operative container with ``--network host`` -- it only joins
    the default bridge network or a named ``local_docker_network`` -- so that
    hostname always names the operative's own container, never this machine,
    regardless of ``local_docker_network``.
    """
    from henchmen.config.paths import is_desktop_install

    if not is_desktop_install():
        return None

    container_hostname = settings.local_container_hostname.strip().lower()
    if not container_hostname or any(char in ":/" or char.isspace() for char in container_hostname):
        return (
            f"HENCHMEN_LOCAL_CONTAINER_HOSTNAME ({settings.local_container_hostname!r}) must be a bare "
            "hostname such as 'henchmen', with no port, path or whitespace."
        )

    try:
        split = urlsplit(settings.local_forward_base)
        hostname = split.hostname
        _ = split.port  # accessed for its side effect: raises ValueError on an unparsable port
    except ValueError:
        hostname = None
    if not hostname:
        return f"HENCHMEN_LOCAL_FORWARD_BASE_URL ({settings.local_forward_base!r}) is not a usable URL."

    suggestion = f"http://{container_hostname}:{settings.local_serve_port}"
    lowered = hostname.lower()
    if lowered in _LOOPBACK_NAMES:
        return (
            f"Operatives call Henchmen at {hostname}, its own loopback address inside the operative's "
            f"container, which never reaches this machine. Set HENCHMEN_LOCAL_FORWARD_BASE_URL={suggestion}"
        )
    if lowered in desktop_allowed_hostnames(container_hostname):
        return None
    return (
        f"Operatives call Henchmen at {hostname}, which a desktop install refuses. "
        f"Set HENCHMEN_LOCAL_FORWARD_BASE_URL={suggestion}"
    )


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
