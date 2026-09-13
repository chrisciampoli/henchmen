"""AWS S3 implementation of ObjectStore."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


class S3ObjectStore:
    """ObjectStore backed by AWS S3."""

    def __init__(self, settings: Settings) -> None:
        import boto3

        self._client: Any = boto3.client("s3", region_name=settings.aws_region)

    async def put(self, bucket: str, key: str, data: bytes) -> None:
        """Upload bytes to an S3 object."""
        await asyncio.to_thread(self._client.put_object, Bucket=bucket, Key=key, Body=data)

    async def put_file(self, bucket: str, key: str, file_path: str) -> None:
        """Upload a local file to an S3 object."""
        await asyncio.to_thread(self._client.upload_file, file_path, bucket, key)

    async def get(self, bucket: str, key: str) -> bytes:
        """Download an S3 object as bytes."""

        def _read() -> bytes:
            # Body.read() streams the object over the network, so it has to
            # run in the worker thread too, not back on the event loop.
            response = self._client.get_object(Bucket=bucket, Key=key)
            return bytes(response["Body"].read())

        return await asyncio.to_thread(_read)

    async def get_file(self, bucket: str, key: str, file_path: str) -> None:
        """Download an S3 object to a local file."""
        await asyncio.to_thread(self._client.download_file, bucket, key, file_path)

    async def exists(self, bucket: str, key: str) -> bool:
        """Check whether an S3 object exists."""
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=bucket, Key=key)
            return True
        except Exception as exc:
            # botocore.exceptions.ClientError has a .response dict with Error.Code
            response: dict[str, Any] = getattr(exc, "response", {}) or {}
            error_code = response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                return False
            # Fallback: check str representation
            if "NoSuchKey" in type(exc).__name__ or "404" in str(exc):
                return False
            raise

    async def delete(self, bucket: str, key: str) -> None:
        """Delete an S3 object."""
        await asyncio.to_thread(self._client.delete_object, Bucket=bucket, Key=key)

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        """List S3 object keys with an optional prefix.

        ``list_objects_v2`` caps a response at 1000 keys, so every page is
        followed via ``ContinuationToken``; stopping at the first page would
        silently truncate larger buckets.
        """
        kwargs: dict[str, Any] = {"Bucket": bucket}
        if prefix:
            kwargs["Prefix"] = prefix

        def _list_all() -> list[str]:
            keys: list[str] = []
            page_kwargs = dict(kwargs)
            while True:
                response = self._client.list_objects_v2(**page_kwargs)
                keys.extend(str(obj["Key"]) for obj in response.get("Contents", []))
                token = response.get("NextContinuationToken")
                if not response.get("IsTruncated") or not token:
                    break
                page_kwargs["ContinuationToken"] = token
            return keys

        return await asyncio.to_thread(_list_all)
