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
from pathlib import Path

from henchmen.cli.envfile import EnvFile
from henchmen.config.settings import SEEDED_SECRET_PLACEHOLDER

DISPATCH_API_TOKEN_KEY = "HENCHMEN_DISPATCH_API_TOKEN"
_DISPATCH_SECTION = "Dispatch"
_TOKEN_BYTES = 32


class ConfigStore:
    """Writes ``config_file`` (a dotenv file) and secret files in ``secrets_dir``."""

    def __init__(self, config_file: Path, secrets_dir: Path) -> None:
        self.config_file = config_file
        self.secrets_dir = secrets_dir

    def pending_dispatch_api_token(self) -> str | None:
        """A freshly generated Dispatch API token, or ``None`` when the file's own token is fine.

        Nothing is written here -- callers that must not change the
        configuration on a refused apply validate Settings with this token
        overlaid before persisting it through :meth:`write_dispatch_api_token`
        (ruling P6). A token is only ever considered replaceable when it is
        missing, blank, or the Terraform-seeded placeholder; a user's own
        token is never replaced.
        """
        env = EnvFile.load(self.config_file)
        current = env.get(DISPATCH_API_TOKEN_KEY).strip()
        if current and current != SEEDED_SECRET_PLACEHOLDER:
            return None
        return secrets.token_urlsafe(_TOKEN_BYTES)

    def write_dispatch_api_token(self, token: str) -> None:
        """Persist ``token`` as the Dispatch API token.

        Written through :meth:`EnvFile.write` with its default ``backup=True``,
        so the write is atomic and owner-only and an owner-only ``.bak`` of the
        previous content is kept (ruling P2).
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
