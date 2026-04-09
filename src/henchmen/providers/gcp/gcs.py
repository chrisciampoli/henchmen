"""GCP Cloud Storage implementation of ObjectStore."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import google.cloud.storage as storage

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


class GCSObjectStore:
    """ObjectStore backed by Google Cloud Storage."""

    def __init__(self, settings: Settings) -> None:
        self._client = storage.Client(project=settings.gcp_project_id)

    async def put(self, bucket: str, key: str, data: bytes) -> None:
        """Upload bytes to a GCS object."""
        blob = self._client.bucket(bucket).blob(key)
        await asyncio.to_thread(blob.upload_from_string, data)

    async def put_file(self, bucket: str, key: str, file_path: str) -> None:
        """Upload a local file to a GCS object."""
        blob = self._client.bucket(bucket).blob(key)
        await asyncio.to_thread(blob.upload_from_filename, file_path)

    async def get(self, bucket: str, key: str) -> bytes:
        """Download a GCS object as bytes."""
        blob = self._client.bucket(bucket).blob(key)
        return await asyncio.to_thread(blob.download_as_bytes)

    async def get_file(self, bucket: str, key: str, file_path: str) -> None:
        """Download a GCS object to a local file."""
        blob = self._client.bucket(bucket).blob(key)
        await asyncio.to_thread(blob.download_to_filename, file_path)

    async def exists(self, bucket: str, key: str) -> bool:
        """Check whether a GCS object exists."""
        blob = self._client.bucket(bucket).blob(key)
        return bool(await asyncio.to_thread(blob.exists))

    async def delete(self, bucket: str, key: str) -> None:
        """Delete a GCS object."""
        blob = self._client.bucket(bucket).blob(key)
        await asyncio.to_thread(blob.delete)

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        """List all object keys in a bucket, optionally filtered by prefix."""
        blobs = await asyncio.to_thread(self._client.list_blobs, bucket, prefix=prefix)
        return [blob.name for blob in blobs]
