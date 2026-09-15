"""Credentials for Henchmen's own internal HTTP calls on a desktop install.

In the cloud, Pub/Sub push requests carry Google-signed OIDC tokens. The local
stand-in (the in-memory broker POSTing to the mounted services) has no such
issuer, so a desktop install keeps two secrets under ``<data dir>/secrets``:

* ``internal-push.token`` — the bearer the server's own broker sends with every
  forwarded push. It never leaves the server process (amendment A2): it is not
  injected into an operative's container environment, unlike the operative
  runtime contract in ``lair_manager``.
* ``operative-task.key`` — an HMAC key. Each Lair receives
  ``HMAC-SHA256(key, "henchmen-operative-task:" + task_id)``, which
  authenticates that operative's report and its task-state calls and nothing
  else (a leaked token is worthless for any other task).

Both files are created by :mod:`henchmen.config.secret_files`. Comparisons go
through that module's :func:`henchmen.config.secret_files.tokens_match`
(constant-time); tokens are never logged or repr-ed.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from pathlib import Path

from henchmen.config import paths
from henchmen.config.secret_files import read_or_create_secret, tokens_match

INTERNAL_PUSH_TOKEN_FILE_NAME = "internal-push.token"
OPERATIVE_TASK_KEY_FILE_NAME = "operative-task.key"
_TASK_TOKEN_CONTEXT = b"henchmen-operative-task:"


@dataclass(frozen=True)
class InternalAuth:
    """The push bearer and the task-token key of one desktop install."""

    push_token: str = field(repr=False)
    task_key: bytes = field(repr=False)

    def verify_push_token(self, candidate: str | None) -> bool:
        """True only for this install's internal push token."""
        return tokens_match(candidate, self.push_token)

    def task_token(self, task_id: str) -> str:
        """The bearer token an operative working on ``task_id`` receives."""
        if not task_id:
            raise ValueError("task_id is required to derive an operative task token")
        return hmac.new(self.task_key, _TASK_TOKEN_CONTEXT + task_id.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_task_token(self, task_id: str, candidate: str | None) -> bool:
        """True only for the token derived for exactly ``task_id``."""
        if not task_id:
            return False
        return tokens_match(candidate, self.task_token(task_id))


_cache: dict[Path, InternalAuth] = {}
_cache_lock = threading.Lock()


def load_internal_auth(secrets_dir: Path) -> InternalAuth:
    """Load (creating when missing or short) both internal secrets from ``secrets_dir``.

    Cached per resolved directory so repeated calls within one process (and
    within one install) return the same credentials without re-reading disk.
    """
    key = secrets_dir.resolve()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is None:
            raw_push = read_or_create_secret(secrets_dir / INTERNAL_PUSH_TOKEN_FILE_NAME)
            task_key = read_or_create_secret(secrets_dir / OPERATIVE_TASK_KEY_FILE_NAME)
            push_token = urlsafe_b64encode(raw_push).rstrip(b"=").decode("ascii")
            cached = InternalAuth(push_token=push_token, task_key=task_key)
            _cache[key] = cached
        return cached


def desktop_internal_auth() -> InternalAuth | None:
    """This install's internal credentials, or ``None`` when this is not a data-directory install."""
    secrets_dir = paths.secrets_dir()
    return None if secrets_dir is None else load_internal_auth(secrets_dir)


def clear_cache() -> None:
    """Drop every cached :class:`InternalAuth`.

    Test-only: a test that points ``HENCHMEN_DATA_DIR`` at a fresh ``tmp_path``
    still shares this process-wide cache with every other test, so a suite
    that (re)creates secrets under the same resolved path across tests must
    clear it first -- otherwise a later test silently reads an earlier test's
    cached credentials instead of the ones on disk.
    """
    with _cache_lock:
        _cache.clear()
