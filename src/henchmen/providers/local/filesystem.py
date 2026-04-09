"""Local filesystem implementation of ObjectStore."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


class FilesystemObjectStore:
    """ObjectStore backed by the local filesystem."""

    def __init__(self, settings: Settings, base_dir: str | None = None) -> None:
        self._base = Path(base_dir) if base_dir else Path.home() / ".henchmen" / "storage"
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, bucket: str, key: str) -> Path:
        return self._base / bucket / key

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
        bucket_dir = self._base / bucket
        if not bucket_dir.exists():
            return []
        results = []
        for path in bucket_dir.rglob("*"):
            if path.is_file():
                rel = str(path.relative_to(bucket_dir)).replace("\\", "/")
                if rel.startswith(prefix):
                    results.append(rel)
        return sorted(results)
