"""Merge queue - FIFO merge serialization for parallel Operatives using DocumentStore."""

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from henchmen.providers.interfaces.document_store import DocumentStore

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

_COLLECTION = "merge_queue"
_STATUS_PENDING = "pending"
_STATUS_MERGING = "merging"
_STATUS_MERGED = "merged"
_STATUS_FAILED = "failed"

# Maximum time an entry can stay in "merging" state before it is considered stale.
_MERGING_TTL = timedelta(minutes=30)

# How many pending entries to pull when picking the next candidate. The store
# interface only supports a single ``order_by``, so priority is applied in the
# client over this window.
_CANDIDATE_WINDOW = 50


def _now_iso() -> str:
    """Current UTC timestamp as a sortable ISO-8601 string.

    Timestamps are stored as strings, not ``datetime`` objects: SQLite and
    DynamoDB both serialise datetimes to ISO strings on write, so a stored
    value later compared against a ``datetime`` filter raises ``TypeError``.
    UTC ISO-8601 sorts lexicographically, so range filters still work.
    """
    return datetime.now(UTC).isoformat()


class MergeQueue:
    """FIFO merge serialization for parallel Operatives using DocumentStore."""

    def __init__(self, settings: "Settings", document_store: DocumentStore | None = None) -> None:
        self.settings = settings
        self._document_store = document_store

    def _get_store(self) -> DocumentStore:
        if self._document_store is not None:
            return self._document_store
        from henchmen.providers.registry import ProviderRegistry

        return ProviderRegistry(self.settings).get_document_store()

    async def enqueue(self, pr_url: str, task_id: str, priority: int = 0) -> str:
        """Add a PR to the merge queue. Returns queue entry ID.

        Higher *priority* entries are dequeued before lower ones; entries of
        equal priority are dequeued oldest-first.
        """
        store = self._get_store()
        entry_id = str(uuid4())
        entry = {
            "id": entry_id,
            "pr_url": pr_url,
            "task_id": task_id,
            "status": _STATUS_PENDING,
            "created_at": _now_iso(),
            "priority": priority,
            "error": None,
        }
        await store.set(_COLLECTION, entry_id, entry)
        return entry_id

    async def dequeue(self) -> dict[str, Any] | None:
        """Atomically claim the next pending PR from the queue.

        Returns ``None`` if the queue is empty or another replica is
        already merging. Claiming uses
        :meth:`DocumentStore.update_if` as a compare-and-swap
        (``status == 'pending'`` → ``status = 'merging'``), so two
        Forge replicas racing ``dequeue`` cannot both pick the same
        entry: at most one CAS succeeds and the others see ``None``.

        Backed by:

        * Firestore transactions
        * DynamoDB ``ConditionExpression``
        * SQLite per-doc asyncio locks

        The CAS alone only protects a *single* entry, so two replicas
        interleaving their reads could each claim a *different* entry and
        both believe they hold the queue. After a winning CAS the claim is
        therefore re-verified against every merging entry; a replica that
        finds an older concurrent claim releases its own entry back to
        ``pending`` and returns ``None``.

        Callers that lose a race can simply retry on the next poll.
        """
        store = self._get_store()

        # First, expire stale "merging" entries that exceeded the TTL.
        await self.expire_stale_merging()

        # Check if any entry is currently merging (serialization guard).
        merging_docs = await store.query(
            _COLLECTION,
            filters=[("status", "==", _STATUS_MERGING)],
            limit=1,
        )
        if merging_docs:
            # A merge is already in progress — do not start another.
            return None

        candidate = await self._next_candidate(store)
        if candidate is None:
            return None
        entry_id = candidate["id"]

        # Atomic claim via compare-and-set: only the replica that sees
        # ``status == 'pending'`` at commit time wins.
        claimed_at = _now_iso()
        claimed = await store.update_if(
            _COLLECTION,
            entry_id,
            "status",
            _STATUS_PENDING,
            {
                "status": _STATUS_MERGING,
                "merging_started_at": claimed_at,
            },
        )
        if not claimed:
            # Lost the race to another replica — back off and let the
            # caller retry on the next tick.
            logger.info(
                "[merge-queue] CAS lost for entry %s — another replica claimed it",
                entry_id,
            )
            return None

        if not await self._confirm_sole_claim(store, entry_id, claimed_at):
            return None

        candidate["status"] = _STATUS_MERGING
        candidate["merging_started_at"] = claimed_at
        return candidate

    async def _next_candidate(self, store: DocumentStore) -> dict[str, Any] | None:
        """Pick the next pending entry: highest priority first, then FIFO."""
        pending_docs = await store.query(
            _COLLECTION,
            filters=[("status", "==", _STATUS_PENDING)],
            order_by="created_at",
            limit=_CANDIDATE_WINDOW,
        )
        if not pending_docs:
            return None
        # ``order_by`` already gives FIFO; a stable sort on -priority keeps that
        # ordering within each priority band.
        return sorted(pending_docs, key=lambda doc: -int(doc.get("priority") or 0))[0]

    async def _confirm_sole_claim(self, store: DocumentStore, entry_id: str, claimed_at: str) -> bool:
        """Verify this replica holds the only merging claim, releasing it if not."""
        merging_docs = await store.query(_COLLECTION, filters=[("status", "==", _STATUS_MERGING)])
        # Ties are broken on entry id so that two replicas claiming in the same
        # microsecond cannot both decide to release.
        mine = (claimed_at, entry_id)
        rivals = [
            doc
            for doc in merging_docs
            if doc.get("id") != entry_id and (str(doc.get("merging_started_at") or ""), str(doc.get("id"))) < mine
        ]
        if not rivals:
            return True

        logger.info(
            "[merge-queue] Releasing entry %s — replica %s claimed concurrently",
            entry_id,
            rivals[0].get("id"),
        )
        await store.update(
            _COLLECTION,
            entry_id,
            {"status": _STATUS_PENDING, "merging_started_at": None},
        )
        return False

    async def expire_stale_merging(self) -> int:
        """Mark stale 'merging' entries as failed if they exceeded the TTL.

        Prevents a permanently blocked queue when a merge process crashes
        without completing. Returns the number of entries expired.
        """
        store = self._get_store()
        cutoff = (datetime.now(UTC) - _MERGING_TTL).isoformat()
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
        return len(stale_docs)

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
