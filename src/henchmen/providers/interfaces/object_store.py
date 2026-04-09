"""ObjectStore interface — blob/object storage."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class ObjectStore(Protocol):
    """Abstraction over blob storage (GCS, S3, local filesystem)."""

    async def put(self, bucket: str, key: str, data: bytes) -> None:
        """Upload data to a key."""
        ...

    async def put_file(self, bucket: str, key: str, file_path: str) -> None:
        """Upload a local file to a key."""
        ...

    async def get(self, bucket: str, key: str) -> bytes:
        """Download data from a key."""
        ...

    async def get_file(self, bucket: str, key: str, file_path: str) -> None:
        """Download a key to a local file."""
        ...

    async def exists(self, bucket: str, key: str) -> bool:
        """Check if a key exists."""
        ...

    async def delete(self, bucket: str, key: str) -> None:
        """Delete a key."""
        ...

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        """List keys with an optional prefix filter."""
        ...
