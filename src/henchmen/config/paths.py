"""Filesystem layout for a data-directory install.

The local container image and the setup Console keep every piece of state in
one directory (a Docker volume mounted at ``/data``). Its location is a
*bootstrap* variable read straight from the environment: it decides which
dotenv file ``Settings`` loads, so it cannot itself be a ``Settings`` field.

Without ``HENCHMEN_DATA_DIR`` nothing changes: ``Settings`` reads ``.env.local``
and ``.env`` from the working directory, as it always has.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR_ENV = "HENCHMEN_DATA_DIR"
SETUP_TOKEN_ENV = "HENCHMEN_CONSOLE_SETUP_TOKEN"

_CONFIG_FILE_NAME = "henchmen.env"
_SETUP_STATE_FILE_NAME = "setup-state.json"
_SECRETS_DIR_NAME = "secrets"
_DEFAULT_ENV_FILES: tuple[str, ...] = (".env.local", ".env")


def data_dir() -> Path | None:
    """Return the data directory, or ``None`` when this is not a data-dir install."""
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    return Path(raw) if raw else None


def env_files() -> tuple[str, ...]:
    """Dotenv files ``Settings`` should read, highest priority first."""
    base = data_dir()
    if base is None:
        return _DEFAULT_ENV_FILES
    return (str(base / _CONFIG_FILE_NAME),)


def config_file() -> Path:
    """The file setup tools write configuration to."""
    base = data_dir()
    return base / _CONFIG_FILE_NAME if base is not None else Path(_DEFAULT_ENV_FILES[0])


def setup_state_file() -> Path | None:
    """Where the Console persists setup progress, or ``None`` without a data dir."""
    base = data_dir()
    return base / _SETUP_STATE_FILE_NAME if base is not None else None


def secrets_dir() -> Path | None:
    """Directory for secret files (keys, session signing key), or ``None``."""
    base = data_dir()
    return base / _SECRETS_DIR_NAME if base is not None else None
