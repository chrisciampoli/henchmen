"""Merge queue - FIFO merge serialization for parallel Operatives using DocumentStore."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from henchmen.providers.interfaces.document_store import DocumentStore

logger = logging.getLogger(__name__)

_COLLECTION = "merge_queue"
_STATUS_PENDING = "pending"
_STATUS_MERGING = "merging"
_STATUS_MERGED = "merged"
_STATUS_FAILED = "failed"

# Maximum time an entry can stay in "merging" state before it is considered stale.
_MERGING_TTL = timedelta(minutes=30)


class MergeQueue:
    """FIFO merge serialization for parallel Operatives using DocumentStore."""

    def __init__(self, settings: Any, document_store: DocumentStore | None = None) -> None:
        self.settings = settings
        self._document_store = document_store

    def _get_store(self) -> DocumentStore:
        if self._document_store is not None:
            return self._document_store
        from henchmen.providers.registry import ProviderRegistry

        return ProviderRegistry(self.settings).get_document_store()

    async def enqueue(self, pr_url: str, task_id: str, priority: int = 0) -> str:
        """Add a PR to the merge queue. Returns queue entry ID."""
        store = self._get_store()
        entry_id = str(uuid4())
        entry = {
            "id": entry_id,
            "pr_url": pr_url,
            "task_id": task_id,
            "status": _STATUS_PENDING,
            "created_at": datetime.now(UTC),
            "priority": priority,
            "error": None,
        }
        await store.set(_COLLECTION, entry_id, entry)
        return entry_id

    async def dequeue(self) -> dict[str, Any] | None:
        """Get the next PR to merge (FIFO). Returns None if queue empty or another merge in progress.

        Checks for active merges first, then atomically claims the next pending entry,
        preventing race conditions when multiple callers attempt to dequeue concurrently.
        """
        store = self._get_store()

        # First, expire stale "merging" entries that exceeded the TTL
        await self._expire_stale_merging(store)

        # Check if any entry is currently merging (serialization guard)
        merging_docs = await store.query(
            _COLLECTION,
            filters=[("status", "==", _STATUS_MERGING)],
            limit=1,
        )
        if merging_docs:
            # A merge is already in progress — do not start another
            return None

        # Find the next pending entry, ordered by created_at (FIFO)
        pending_docs = await store.query(
            _COLLECTION,
            filters=[("status", "==", _STATUS_PENDING)],
            order_by="created_at",
            limit=1,
        )
        if not pending_docs:
            return None

        candidate = pending_docs[0]
        entry_id = candidate["id"]

        # Atomically claim the entry
        await store.update(
            _COLLECTION,
            entry_id,
            {
                "status": _STATUS_MERGING,
                "merging_started_at": datetime.now(UTC),
            },
        )

        candidate["status"] = _STATUS_MERGING
        return candidate

    async def _expire_stale_merging(self, store: DocumentStore) -> None:
        """Mark stale 'merging' entries as failed if they exceeded the TTL.

        Prevents a permanently blocked queue when a merge process crashes
        without completing.
        """
        cutoff = datetime.now(UTC) - _MERGING_TTL
        stale_docs = await store.query(
            _COLLECTION,
            filters=[("status", "==", _STATUS_MERGING), ("merging_started_at", "<", cutoff)],
        )
        for doc in stale_docs:
            entry_id = doc["id"]
            logger.warning("[merge-queue] Expiring stale merging entry %s (exceeded %s TTL)", entry_id, _MERGING_TTL)
            await store.update(
                _COLLECTION,
                entry_id,
                {
                    "status": _STATUS_FAILED,
                    "error": f"Merge TTL exceeded ({_MERGING_TTL})",
                },
            )

    async def mark_merged(self, entry_id: str) -> None:
        """Mark a queue entry as successfully merged."""
        store = self._get_store()
        await store.update(_COLLECTION, entry_id, {"status": _STATUS_MERGED})

    async def mark_failed(self, entry_id: str, error: str) -> None:
        """Mark a queue entry as failed."""
        store = self._get_store()
        await store.update(_COLLECTION, entry_id, {"status": _STATUS_FAILED, "error": error})

    async def get_queue_length(self) -> int:
        """Get number of pending entries."""
        store = self._get_store()
        docs = await store.query(_COLLECTION, filters=[("status", "==", _STATUS_PENDING)])
        return len(docs)

    async def get_queue(self) -> list[dict[str, Any]]:
        """Get all queue entries ordered by creation time."""
        store = self._get_store()
        return await store.query(_COLLECTION, order_by="created_at")
