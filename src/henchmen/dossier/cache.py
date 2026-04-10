"""SnapshotCache – ObjectStore-based repository snapshot cache for fast workspace setup."""

import asyncio
import hashlib
import logging
import os
import tarfile
import tempfile
from typing import TYPE_CHECKING

from henchmen.providers.interfaces.object_store import ObjectStore

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

_SNAPSHOT_PREFIX = "snapshots/"
_SNAPSHOT_SUFFIX = ".tar.gz"


class SnapshotCache:
    """Manages repository snapshot cache for faster workspace setup."""

    def __init__(self, settings: "Settings", object_store: ObjectStore | None = None) -> None:
        self.settings = settings
        self._object_store = object_store

    def _get_object_store(self) -> ObjectStore:
        """Lazy-create ObjectStore via ProviderRegistry if not injected."""
        if self._object_store is None:
            from henchmen.providers.registry import ProviderRegistry

            self._object_store = ProviderRegistry(self.settings).get_object_store()
        return self._object_store

    async def get_snapshot(self, repo_url: str, branch: str = "main") -> str | None:
        """Return the GCS URI of a cached snapshot, or None if not cached."""
        bucket = self.settings.gcs_bucket_snapshots
        if not bucket:
            return None

        key = self._snapshot_key(repo_url, branch)
        blob_name = f"{_SNAPSHOT_PREFIX}{key}{_SNAPSHOT_SUFFIX}"

        try:
            object_store = self._get_object_store()
            if await object_store.exists(bucket, blob_name):
                uri = f"gs://{bucket}/{blob_name}"
                logger.info("Snapshot cache hit: %s", uri)
                return uri
        except Exception as exc:
            logger.warning("Error checking snapshot cache: %s", exc)

        return None

    async def save_snapshot(self, workspace_dir: str, repo_url: str, branch: str = "main") -> str:
        """Create a tarball of the workspace and upload it via ObjectStore.

        Returns the GCS URI of the uploaded snapshot.
        """
        bucket = self.settings.gcs_bucket_snapshots
        if not bucket:
            raise ValueError("gcs_bucket_snapshots is not configured")

        key = self._snapshot_key(repo_url, branch)
        blob_name = f"{_SNAPSHOT_PREFIX}{key}{_SNAPSHOT_SUFFIX}"

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # Create the tarball in a thread to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _create_tarball, workspace_dir, tmp_path)

            object_store = self._get_object_store()
            await object_store.put_file(bucket, blob_name, tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        uri = f"gs://{bucket}/{blob_name}"
        logger.info("Snapshot saved to %s", uri)
        return uri

    async def restore_snapshot(self, snapshot_uri: str, target_dir: str) -> None:
        """Download and extract a snapshot tarball into target_dir."""
        if not snapshot_uri.startswith("gs://"):
            raise ValueError(f"Invalid GCS URI: {snapshot_uri}")

        without_prefix = snapshot_uri[len("gs://") :]
        bucket, blob_name = without_prefix.split("/", 1)

        os.makedirs(target_dir, exist_ok=True)

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            object_store = self._get_object_store()
            await object_store.get_file(bucket, blob_name, tmp_path)

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _extract_tarball, tmp_path, target_dir)
            logger.info("Snapshot restored from %s → %s", snapshot_uri, target_dir)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _snapshot_key(self, repo_url: str, branch: str) -> str:
        """Generate a deterministic cache key from repo URL and branch."""
        raw = f"{repo_url}#{branch}"
        return hashlib.sha256(raw.encode()).hexdigest()[:40]


# ---------------------------------------------------------------------------
# Helpers (run in thread executor to avoid blocking event loop)
# ---------------------------------------------------------------------------


def _create_tarball(source_dir: str, dest_path: str) -> None:
    """Create a .tar.gz archive of source_dir at dest_path."""
    with tarfile.open(dest_path, "w:gz") as tar:
        tar.add(source_dir, arcname=".")


def _extract_tarball(tarball_path: str, dest_dir: str) -> None:
    """Extract a .tar.gz archive into dest_dir."""
    with tarfile.open(tarball_path, "r:gz") as tar:
        tar.extractall(dest_dir)
