"""Owner-only secret files in a data directory's ``secrets/`` folder.

Every secret a desktop install keeps on disk (Console session key, one-time
setup token, internal push token, operative task-token key) goes through this
module, so each one is created the same way:

* ``O_CREAT | O_EXCL | O_WRONLY`` with mode 0600 from the first byte — never
  created world-readable and chmod-ed afterwards;
* ``O_BINARY`` on Windows, where a text-mode descriptor rewrites ``\\n`` to
  ``\\r\\n`` and silently corrupts random key material;
* replaced atomically (temp file + ``os.replace``), never truncated in place;
* a file shorter than :data:`MIN_SECRET_BYTES` is never trusted (an empty key
  from a crash or a full disk would let anyone forge an HMAC) and is regenerated.

Secret values are never logged.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_SECRET_BYTES = 32


def ensure_secrets_dir(directory: Path) -> None:
    """Create ``directory`` (and parents) owner-only if it does not exist."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)


def create_secret_file(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively with mode 0600 and write ``data``; raise ``FileExistsError`` if present."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def write_secret_file(path: Path, data: bytes) -> None:
    """Atomically replace (or create) ``path`` with ``data``, owner-only from creation."""
    tmp_path = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    create_secret_file(tmp_path, data)
    try:
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def read_or_create_secret(path: Path, *, nbytes: int = MIN_SECRET_BYTES) -> bytes:
    """Return the secret in ``path``, creating or regenerating it when missing or too short."""
    ensure_secrets_dir(path.parent)
    try:
        existing: bytes | None = path.read_bytes()
    except FileNotFoundError:
        existing = None

    if existing is not None and len(existing) >= nbytes:
        if sys.platform != "win32" and path.stat().st_mode & 0o077:
            os.chmod(path, 0o600)
        return existing

    fresh = secrets.token_bytes(max(nbytes, MIN_SECRET_BYTES))
    if existing is None:
        try:
            create_secret_file(path, fresh)
            return fresh
        except FileExistsError:
            # Another process created it between our read and our create: use theirs when valid.
            existing = path.read_bytes()
            if len(existing) >= nbytes:
                return existing

    logger.warning("Secret file %s is missing or too short; regenerating it.", path.name)
    write_secret_file(path, fresh)
    return fresh
