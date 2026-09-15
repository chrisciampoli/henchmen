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
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from pathlib import Path

from henchmen.cli.envfile import EnvFile
from henchmen.config.settings import SEEDED_SECRET_PLACEHOLDER
from henchmen.config.validation import DISPATCH_API_TOKEN_KEY, settings_problems

__all__ = ["DISPATCH_API_TOKEN_KEY", "ConfigStore"]

_DISPATCH_SECTION = "Dispatch"
_TOKEN_BYTES = 32


class ConfigStore:
    """Writes ``config_file`` (a dotenv file) and secret files in ``secrets_dir``."""

    def __init__(self, config_file: Path, secrets_dir: Path) -> None:
        self.config_file = config_file
        self.secrets_dir = secrets_dir

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
        or restart when this raises.
        """
        env = EnvFile.load(self.config_file)
        env.set(DISPATCH_API_TOKEN_KEY, token, section=_DISPATCH_SECTION)
        env.write()

    def ensure_dispatch_api_token(self) -> bool:
        """Convenience for callers that do not validate first; True when a new token was written."""
        token = self.pending_dispatch_api_token()
        if token is None:
            return False
        self.write_dispatch_api_token(token)
        return True
