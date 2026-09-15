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
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_SECRET_BYTES = 32

# How long, and how many times, to retry reading a file that a concurrent
# creator's directory entry beat us to before its content has necessarily
# landed (5 x 20ms = 100ms of tolerance for a same-instant race).
_RACE_RETRY_ATTEMPTS = 5
_RACE_RETRY_DELAY_SECONDS = 0.02


def ensure_secrets_dir(directory: Path) -> None:
    """Create ``directory`` (and parents) owner-only; tighten it if it already exists loosely (POSIX)."""
    existed = directory.is_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if existed and sys.platform != "win32" and directory.stat().st_mode & 0o077:
        os.chmod(directory, 0o700)


def create_secret_file(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively with mode 0600 and write ``data``; raise ``FileExistsError`` if present.

    If the write fails after the exclusive create (disk full, EIO, an interrupting
    signal), the partially written file is removed before re-raising -- safe
    because ``O_EXCL`` guarantees this call is the one that created it, so no
    other process's data can be deleted.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
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


def _tighten_if_loose(path: Path) -> None:
    """Tighten an existing secret file's inherited group/other permission bits (POSIX only)."""
    if sys.platform != "win32" and path.stat().st_mode & 0o077:
        logger.warning("Secret file %s has group/other permissions; tightening it to 0600.", path.name)
        os.chmod(path, 0o600)


def _publish_new_secret(path: Path, data: bytes, *, minimum: int) -> bytes:
    """Create ``path`` for the first time with ``data``, tolerating a concurrent creator.

    A complete, private temp file is written first and then published with an
    exclusive hard link, so no process ever observes ``path`` existing but empty
    or truncated -- the failure mode of creating the final name directly and
    writing into it, which lets a racing reader mistake a fresh file for a
    corrupt one and overwrite it out from under the process that just created it.

    If ``path`` already exists (we lost the race), its content is re-read with a
    few short retries in case a concurrent writer's bytes have not landed on disk
    yet; only once it is still shorter than ``minimum`` after those retries is it
    treated as genuinely corrupt (e.g. a crash mid-write) and replaced. When hard
    links are not supported on this filesystem, falls back to creating ``path``
    directly.
    """
    tmp_path = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    create_secret_file(tmp_path, data)
    try:
        published = False
        try:
            os.link(str(tmp_path), str(path))
            published = True
        except FileExistsError:
            pass
        except OSError:
            # Hard links unsupported here: fall back to a direct exclusive create,
            # tolerating the same kind of same-instant race below if it loses too.
            try:
                create_secret_file(path, data)
                published = True
            except FileExistsError:
                pass

        if published:
            return data

        existing = b""
        for attempt in range(_RACE_RETRY_ATTEMPTS):
            try:
                existing = path.read_bytes()
            except FileNotFoundError:
                existing = b""
            if len(existing) >= minimum:
                return existing
            if attempt < _RACE_RETRY_ATTEMPTS - 1:
                time.sleep(_RACE_RETRY_DELAY_SECONDS)

        logger.warning("Secret file %s is missing or too short; regenerating it.", path.name)
        os.replace(tmp_path, path)
        return data
    finally:
        tmp_path.unlink(missing_ok=True)


def read_or_create_secret(path: Path, *, nbytes: int = MIN_SECRET_BYTES) -> bytes:
    """Return the secret in ``path``, creating or regenerating it when missing or too short.

    Whether ``path`` was absent or already held a too-short value at the initial
    read, regeneration always goes through :func:`_publish_new_secret` so a file
    that merely *looks* short because a concurrent writer's bytes have not landed
    yet is never mistaken for corrupt and overwritten out from under it.
    """
    minimum = max(nbytes, MIN_SECRET_BYTES)
    ensure_secrets_dir(path.parent)
    try:
        existing: bytes | None = path.read_bytes()
    except FileNotFoundError:
        existing = None

    if existing is not None and len(existing) >= minimum:
        _tighten_if_loose(path)
        return existing

    fresh = secrets.token_bytes(minimum)
    return _publish_new_secret(path, fresh, minimum=minimum)
