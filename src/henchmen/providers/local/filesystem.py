"""Local filesystem implementation of ObjectStore."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from henchmen.providers.settings_access import optional_setting

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

_DEFAULT_STORAGE_DIR = Path.home() / ".henchmen" / "storage"


class FilesystemObjectStore:
    """ObjectStore backed by the local filesystem.

    Bucket and key are attacker-influenced in principle (a key can be derived
    from a task id or a dossier URI), so every resolved path is checked to be
    inside the store root before any I/O happens.
    """

    def __init__(self, settings: Settings, base_dir: str | None = None) -> None:
        # ``local_storage_dir`` is optional on Settings; read it defensively so
        # this keeps working against a Settings class that lacks the field.
        configured = base_dir or optional_setting(settings, "local_storage_dir")
        self._base = Path(configured).expanduser() if configured else _DEFAULT_STORAGE_DIR
        self._base.mkdir(parents=True, exist_ok=True)
        self._base = self._base.resolve()

    def _path(self, bucket: str, key: str) -> Path:
        """Resolve bucket/key under the store root, rejecting traversal."""
        candidate = (self._base / bucket / key).resolve()
        if candidate != self._base and self._base not in candidate.parents:
            raise ValueError(f"Object path escapes the store root: {bucket}/{key}")
        return candidate

    async def put(self, bucket: str, key: str, data: bytes) -> None:
        """Write bytes to the given bucket/key path."""
        path = self._path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def put_file(self, bucket: str, key: str, file_path: str) -> None:
        """Copy a local file into the store at the given bucket/key."""
        path = self._path(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, path)

    async def get(self, bucket: str, key: str) -> bytes:
        """Read bytes from the given bucket/key path."""
        return self._path(bucket, key).read_bytes()

    async def get_file(self, bucket: str, key: str, file_path: str) -> None:
        """Copy the stored object at bucket/key to a local file path."""
        shutil.copy2(self._path(bucket, key), file_path)

    async def exists(self, bucket: str, key: str) -> bool:
        """Return True if the given bucket/key exists."""
        return self._path(bucket, key).exists()

    async def delete(self, bucket: str, key: str) -> None:
        """Delete the object at the given bucket/key if it exists."""
        path = self._path(bucket, key)
        if path.exists():
            path.unlink()

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        """List all keys in a bucket, optionally filtered by prefix."""
        bucket_dir = self._path(bucket, "")
        if not bucket_dir.exists():
            return []
        results = []
        for path in bucket_dir.rglob("*"):
            if path.is_file():
                rel = str(path.relative_to(bucket_dir)).replace("\\", "/")
                if rel.startswith(prefix):
                    results.append(rel)
        return sorted(results)
