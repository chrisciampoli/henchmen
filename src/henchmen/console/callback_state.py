"""Single-use, expiring ``state`` values for the public GitHub callbacks (D-P11, spec §7 as amended).

GitHub sends the browser back to the Console with a cross-site redirect, on
which the SameSite=Strict session cookie is not sent. The callbacks are
therefore public routes, and each is authorised by a ``state`` value that a
session-authenticated route issued moments before. A state is:

* **random** -- 256 bits from :func:`secrets.token_urlsafe`;
* **single use** -- consuming a state removes it, whether or not the purpose
  matched and whether or not it had expired, and the removal is written to
  disk before the state is honoured. The read-remove-write happens under a
  lock shared by every store over the same file, so two concurrent callbacks
  carrying the same state can never both succeed. If the removal cannot be
  written, the state is refused (fail closed: it could otherwise be replayed).
  This guarantee holds within one process (a ``threading.Lock``, no file
  lock): ``henchmen serve`` runs the Console in a single process, and two
  processes must never serve the same data directory;
* **expiring** -- one hour by default, the lifetime of a manifest ``code``;
* **purpose-bound** -- a manifest state cannot complete an installation;
* **persisted** in ``secrets/github-callback-states.json`` (mode 0600, written
  atomically through :mod:`henchmen.config.secret_files`) so a restart between
  leaving for github.com and coming back does not strand the user.

The file stores only the SHA-256 digest of each state, never the state itself,
so reading the file does not yield a usable state. A presented state is found
by looking its digest up in that mapping rather than by comparing it with
:func:`henchmen.config.secret_files.tokens_match` (ruling M-20): the state has
256 bits of entropy and only its hash is ever compared, so the lookup's timing
reveals nothing an attacker could use to build a valid state.

The file is read through :func:`~henchmen.config.secret_files.check_secret_path`:
a symbolic link, a non-regular file or a file owned by another user is treated
as holding no states (every callback is refused) and is never followed.

The data carried with a state is non-secret (an account type, an organisation
login, an App slug) and never includes a credential.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from henchmen.config.secret_files import check_secret_path, ensure_secrets_dir, write_secret_file

__all__ = ["DEFAULT_TTL_SECONDS", "MAX_PENDING_STATES", "STATE_FILE_NAME", "CallbackStateStore"]

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "github-callback-states.json"
DEFAULT_TTL_SECONDS = 3600
MAX_PENDING_STATES = 20
_STATE_BYTES = 32
_MAX_STATE_LENGTH = 256

# Keyed by the resolved file path rather than per instance, so two stores over the
# same file (a second app in the same process, a test) still consume atomically.
_FILE_LOCKS: dict[Path, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = path.resolve()
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(key, threading.Lock())


def _digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _expiry(entry: Mapping[str, Any]) -> float:
    """The entry's expiry as a finite number, or ``-inf`` (already expired) when unreadable."""
    raw = entry.get("expires_at")
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
        return -math.inf
    return float(raw)


class CallbackStateStore:
    """Issue and consume callback ``state`` values."""

    def __init__(
        self, path: Path, *, ttl_seconds: int = DEFAULT_TTL_SECONDS, clock: Callable[[], float] = time.time
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.path = path
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = _lock_for(path)

    def __repr__(self) -> str:
        return f"CallbackStateStore(path={self.path!s})"

    def issue(self, purpose: str, data: Mapping[str, str]) -> str:
        """Create a state for ``purpose`` carrying non-secret ``data``; return the state to hand to GitHub.

        Raises ``OSError`` when the state cannot be saved; the caller must not
        send the browser to GitHub with a state that was never stored.
        """
        if not purpose:
            raise ValueError("a callback state needs a purpose")
        state = secrets.token_urlsafe(_STATE_BYTES)
        entry = {
            "purpose": purpose,
            "data": {str(key): str(value) for key, value in data.items()},
            "expires_at": self._clock() + self._ttl,
        }
        with self._lock:
            entries = self._live(self._read())
            entries[_digest(state)] = entry
            if len(entries) > MAX_PENDING_STATES:
                oldest = sorted(entries, key=lambda digest: _expiry(entries[digest]))
                for digest in oldest[: len(entries) - MAX_PENDING_STATES]:
                    del entries[digest]
            self._write(entries)
        return state

    def consume(self, purpose: str, state: str) -> dict[str, str] | None:
        """Remove ``state`` and return its data when it was issued for ``purpose`` and has not expired.

        ``None`` for a blank, oversized, unknown, expired or wrong-purpose
        state, and whenever the removal could not be saved.
        """
        if not isinstance(state, str) or not state or len(state) > _MAX_STATE_LENGTH:
            return None
        digest = _digest(state)
        with self._lock:
            entries = self._read()
            entry = entries.pop(digest, None)
            live = self._live(entries)
            if entry is not None or len(live) != len(entries):
                try:
                    self._write(live)
                except OSError as exc:
                    logger.warning(
                        "Could not save GitHub callback state file %s (%s); refusing the callback",
                        self.path.name,
                        type(exc).__name__,
                    )
                    return None
        if entry is None or entry.get("purpose") != purpose:
            return None
        if _expiry(entry) <= self._clock():
            return None
        raw_data = entry.get("data")
        if not isinstance(raw_data, dict):
            return {}
        return {str(key): str(value) for key, value in raw_data.items()}

    def _live(self, entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        now = self._clock()
        return {digest: entry for digest, entry in entries.items() if _expiry(entry) > now}

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            check_secret_path(self.path)
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # SecretFileError (a symlink, another user's file) is an OSError: fail closed,
            # never follow it. Only the file name and the error type are logged.
            logger.warning("Ignoring unreadable GitHub callback state file %s (%s)", self.path.name, type(exc).__name__)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(digest): entry for digest, entry in raw.items() if isinstance(entry, dict)}

    def _write(self, entries: dict[str, dict[str, Any]]) -> None:
        ensure_secrets_dir(self.path.parent)
        write_secret_file(self.path, json.dumps(entries, sort_keys=True).encode("utf-8"))
