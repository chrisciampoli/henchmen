"""The single writer of Console configuration changes (decision D-P10).

Every Console write to configuration goes through :class:`ConfigStore`, which
wraps :class:`henchmen.cli.envfile.EnvFile` against ``paths.config_file()``
with secret files under ``paths.secrets_dir()``. Plan 2A adds only what apply
needs -- a generated Dispatch API token; Plan 2B extends the class.

Apply must not change the configuration when it refuses (ruling P6): the token
is computed in memory by :meth:`ConfigStore.pending_dispatch_api_token`
without writing anything, so the caller can validate Settings with it
overlaid first and write it via :meth:`ConfigStore.write_dispatch_api_token`
only once validation passes.

Every Console step writes settings through ConfigStore, never by touching
henchmen.env directly, so three rules hold everywhere: only keys naming a real
Settings field (HENCHMEN_<FIELD>) can be written, so a request cannot plant
PATH or HENCHMEN_DATA_DIR; no value may contain a line break or NUL, which
would smuggle a second assignment into the dotenv file; and writes are
serialised and atomic (EnvFile.write: temp file, rename, owner-only file and
backup), with secret files in secrets/ at mode 0600. Values read back through
get() are for server-side use only; anything that reaches the browser goes
through masked().
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from henchmen.cli.envfile import EnvFile, is_secret_key
from henchmen.config.secret_files import ensure_secrets_dir, write_secret_file
from henchmen.config.settings import SEEDED_SECRET_PLACEHOLDER, Settings
from henchmen.config.validation import DISPATCH_API_TOKEN_KEY, settings_problems

__all__ = ["CONFIGURED", "DISPATCH_API_TOKEN_KEY", "ConfigStore", "ConfigStoreError", "settings_env_names"]

_DISPATCH_SECTION = "Dispatch"
_TOKEN_BYTES = 32

CONFIGURED = "configured"
_FORBIDDEN_CHARACTERS: tuple[str, ...] = ("\n", "\r", "\x00")

# Keyed by the resolved config-file path rather than per instance (ruling C16):
# apply builds its own ConfigStore for each request, and a step router's store
# is a different instance again, so only a lock shared by path -- not by
# instance -- keeps every load-modify-write against the same file serialised.
_FILE_LOCKS: dict[Path, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _lock_for(config_file: Path) -> threading.Lock:
    key = config_file.resolve()
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(key, threading.Lock())


class ConfigStoreError(ValueError):
    """A write was refused: unknown key, unsafe value or unsafe file name."""


def settings_env_names() -> frozenset[str]:
    """Every dotenv key that maps onto a ``Settings`` field."""
    return frozenset(f"HENCHMEN_{name.upper()}" for name in Settings.model_fields)


class ConfigStore:
    """Writes ``config_file`` (a dotenv file) and secret files in ``secrets_dir``."""

    def __init__(self, config_file: Path, secrets_dir: Path) -> None:
        self.config_file = config_file
        self.secrets_dir = secrets_dir
        self._lock = _lock_for(config_file)
        self._allowed = settings_env_names()

    def pending_dispatch_api_token(
        self, *, env_files: Sequence[str] | None = None, seeded_env: Mapping[str, str] | None = None
    ) -> str | None:
        """A freshly generated Dispatch API token, or ``None`` when a token is already usable.

        Nothing is written here -- callers that must not change the
        configuration on a refused apply validate Settings with this token
        overlaid before persisting it through :meth:`write_dispatch_api_token`
        (ruling P6).

        When ``env_files`` is given, the check looks at the same merged view
        of the file and the process environment that
        :func:`henchmen.config.validation.settings_problems` builds for a real
        run (``seeded_env`` masked the same way, D-P8). A token already usable
        there -- whether the file sets it or the environment does (e.g. a
        Secret Manager mount, or a bare ``DISPATCH_API_TOKEN``) -- means
        generation is skipped: the environment outranks the file at runtime
        anyway, so writing a generated token to the file would be pointless.
        Without ``env_files`` (callers with no environment context, such as
        ``henchmen init``), only the file itself is consulted.

        A token is only ever considered replaceable when it is missing,
        blank, or the Terraform-seeded placeholder; a user's own token is
        never replaced.
        """
        if env_files is not None:
            settings, _problems = settings_problems(env_files, seeded_env=seeded_env)
            current = (settings.dispatch_api_token if settings is not None else "").strip()
        else:
            env = EnvFile.load(self.config_file)
            current = env.get(DISPATCH_API_TOKEN_KEY).strip()
        if current and current != SEEDED_SECRET_PLACEHOLDER:
            return None
        return secrets.token_urlsafe(_TOKEN_BYTES)

    def write_dispatch_api_token(self, token: str) -> None:
        """Persist ``token`` as the Dispatch API token.

        Written through :meth:`EnvFile.write` with its default ``backup=True``,
        so the write is atomic and owner-only and an owner-only ``.bak`` of the
        previous content is kept (ruling P2). Raises ``OSError`` on a failed
        write (permissions, full disk); callers must not mark setup complete
        or restart when this raises. Serialised against every other write to
        this config file, wherever the ``ConfigStore`` instance came from
        (ruling C16).
        """
        with self._lock:
            env = EnvFile.load(self.config_file)
            env.set(DISPATCH_API_TOKEN_KEY, token, section=_DISPATCH_SECTION)
            env.write()

    def ensure_dispatch_api_token(self) -> bool:
        """Convenience for callers that do not validate first; True when a new token was written.

        The read, the replaceability check and the write happen under one
        lock acquisition so a concurrent writer (an ``update``, or another
        ``ensure_dispatch_api_token``/``write_dispatch_api_token`` call
        against the same file) can never interleave with it (ruling C16).
        """
        with self._lock:
            env = EnvFile.load(self.config_file)
            current = env.get(DISPATCH_API_TOKEN_KEY).strip()
            if current and current != SEEDED_SECRET_PLACEHOLDER:
                return False
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            env.set(DISPATCH_API_TOKEN_KEY, token, section=_DISPATCH_SECTION)
            env.write()
            return True

    def _check_key(self, key: str) -> None:
        if key not in self._allowed:
            raise ConfigStoreError(f"{key!r} is not a Henchmen setting")

    def get(self, key: str, default: str = "") -> str:
        """Current value of ``key`` in the config file. Server-side use only."""
        self._check_key(key)
        with self._lock:
            return EnvFile.load(self.config_file).get(key, default)

    def is_set(self, key: str) -> bool:
        """True when ``key`` has a non-blank value in the config file."""
        return bool(self.get(key).strip())

    def update(self, values: Mapping[str, str], *, section: str) -> None:
        """Set every key in ``values`` in one atomic write; any bad entry refuses the whole batch."""
        for key, value in values.items():
            self._check_key(key)
            if any(character in value for character in _FORBIDDEN_CHARACTERS):
                raise ConfigStoreError(f"The value for {key} contains a line break or NUL character")
        if not values:
            return
        with self._lock:
            env = EnvFile.load(self.config_file)
            for key, value in values.items():
                env.set(key, value, section=section)
            env.write(backup=True)

    def unset(self, keys: Iterable[str]) -> None:
        """Remove ``keys``; a missing file or absent keys are left untouched."""
        names = list(keys)
        for key in names:
            self._check_key(key)
        with self._lock:
            env = EnvFile.load(self.config_file)
            present = set(env.keys())
            if not env.exists or not present.intersection(names):
                return
            for key in names:
                env.unset(key)
            env.write(backup=True)

    def write_secret_file(self, name: str, data: bytes) -> Path:
        """Write ``data`` to ``secrets/<name>`` (mode 0600) and return the path.

        Creates ``secrets_dir`` (owner-only) first when it does not exist yet,
        so a fresh install with no ``secrets/`` directory does not fail here.
        """
        if not name or name in {".", ".."} or "\\" in name or Path(name).name != name:
            raise ConfigStoreError(f"{name!r} is not a plain file name")
        path = self.secrets_dir / name
        with self._lock:
            ensure_secrets_dir(self.secrets_dir)
            write_secret_file(path, data)
        return path

    def masked(self, keys: Iterable[str]) -> dict[str, str]:
        """Values safe to send to the browser: secrets become ``"configured"`` or ``""``."""
        names = list(keys)
        for key in names:
            self._check_key(key)
        with self._lock:
            env = EnvFile.load(self.config_file)
        result: dict[str, str] = {}
        for key in names:
            value = env.get(key)
            result[key] = (CONFIGURED if value else "") if is_secret_key(key) else value
        return result
