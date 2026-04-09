"""TaskCostAccumulator — persists cumulative task cost across scheme nodes.

Each scheme node runs in its own ephemeral operative process (Cloud Run Job),
so a per-node cost ceiling cannot protect against a task that walks across
many nodes and compounds cost. The TaskCostAccumulator reads the running
total from the same Firestore document that ``TaskTracker`` writes to, adds
per-step deltas as the operative runs, and exposes a ceiling check the
guardrails can consult before issuing the next model call.

This is the L5 fix companion to ``OperativeGuardrails``. The accumulator
is intentionally narrow: it only touches the ``estimated_cost_usd`` field
on the ``task_executions`` document. All other fields (tokens, node
metrics, etc.) are still owned by ``TaskTracker``.

Concurrency notes: the in-process ``asyncio.Lock`` serializes add/check
calls within a single operative process. Cross-process races between
parallel operatives for the same task are out of scope here — Firestore
Increment transforms would be the durable fix, but that requires plumbing
through the ``DocumentStore`` protocol. A brief stale read is acceptable
because the ceiling is advisory (we halt on the NEXT call rather than
rejecting a call in flight).
"""

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from henchmen.providers.interfaces.document_store import DocumentStore

logger = logging.getLogger(__name__)

_COLLECTION = "task_executions"
_COST_FIELD = "estimated_cost_usd"


class TaskCostAccumulator:
    """Tracks cumulative task cost across scheme nodes via the document store."""

    def __init__(
        self,
        document_store: "DocumentStore",
        task_id: str,
        ceiling_usd: float,
    ) -> None:
        self._store = document_store
        self._task_id = task_id
        self._ceiling_usd = ceiling_usd
        self._total_usd: float = 0.0
        self._loaded: bool = False
        self._lock = asyncio.Lock()

    async def _ensure_loaded(self) -> None:
        """Lazily load the current running total from the document store."""
        if self._loaded:
            return
        try:
            doc = await self._store.get(_COLLECTION, self._task_id)
            if doc is not None:
                self._total_usd = float(doc.get(_COST_FIELD, 0.0) or 0.0)
        except Exception as exc:
            logger.warning(
                "TaskCostAccumulator: failed to load cost for task %s: %s",
                self._task_id,
                exc,
            )
            self._total_usd = 0.0
        self._loaded = True

    async def add(self, delta_usd: float) -> None:
        """Increment the running total and persist it."""
        if delta_usd <= 0:
            return
        async with self._lock:
            await self._ensure_loaded()
            self._total_usd += delta_usd
            try:
                await self._store.update(
                    _COLLECTION,
                    self._task_id,
                    {_COST_FIELD: self._total_usd},
                )
            except Exception as exc:
                logger.warning(
                    "TaskCostAccumulator: failed to persist cost for task %s: %s",
                    self._task_id,
                    exc,
                )

    async def check_ceiling(self) -> bool:
        """Return True if the running total is still below the ceiling."""
        async with self._lock:
            await self._ensure_loaded()
            return self._total_usd < self._ceiling_usd

    async def current_total(self) -> float:
        """Return the current cumulative cost in USD."""
        async with self._lock:
            await self._ensure_loaded()
            return self._total_usd

    @property
    def ceiling_usd(self) -> float:
        """Return the configured task-level ceiling."""
        return self._ceiling_usd

    @property
    def cached_total_usd(self) -> float:
        """Return the last-known total without touching the store.

        Suitable for synchronous hot paths (e.g. guardrails.check_cost_ceiling)
        where blocking on the store is undesirable. Callers that need a fresh
        value should await ``current_total()``.
        """
        return self._total_usd
