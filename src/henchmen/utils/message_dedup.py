"""Two-phase push-message dedup markers in the ``processed_messages`` collection.

Shared by every push handler that must not process the same delivery twice
(Mastermind's Pub/Sub handlers, and Forge's ``forge-request`` on a desktop
install), so the marker format and its lifecycle exist once:

1. :func:`claim_message` marks a key ``in_flight`` with an acquisition time
   and returns ``False``; a delivery that finds a ``done`` marker, or an
   ``in_flight`` one younger than the TTL, is a duplicate (``True``). An
   ``in_flight`` marker older than the TTL belongs to a handler that crashed
   and is reclaimed.
2. :func:`mark_message_done` upgrades the marker once processing committed.
3. :func:`release_message_claim` drops it after a failure a retry should redo.

``tracker.cleanup_processed_messages`` reaps old markers by ``processed_at``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

PROCESSED_MESSAGES_COLLECTION = "processed_messages"

#: In-flight markers older than this are reclaimable by a redelivery.
DEFAULT_INFLIGHT_TTL_SECONDS = 900


async def claim_message(
    store: Any, key: str, *, handler: str, ttl_seconds: float = DEFAULT_INFLIGHT_TTL_SECONDS
) -> bool:
    """Claim ``key`` for processing; ``True`` when it is a duplicate that must be skipped. Store errors propagate."""
    now = datetime.now(UTC)
    existing = await store.get(PROCESSED_MESSAGES_COLLECTION, key)
    if existing is not None:
        if existing.get("status", "done") == "done":
            return True
        acquired_raw = existing.get("acquired_at") or existing.get("processed_at")
        if acquired_raw:
            try:
                acquired = datetime.fromisoformat(acquired_raw)
            except ValueError:
                acquired = now  # Treat as freshly acquired on parse failure
            age_seconds = (now - acquired).total_seconds()
            if age_seconds < ttl_seconds:
                logger.info("[dedup] %s is in_flight (age=%.0fs), treating retry as duplicate", key, age_seconds)
                return True
            logger.warning("[dedup] %s in_flight marker is stale (age=%.0fs); reclaiming for retry", key, age_seconds)
    await store.set(
        PROCESSED_MESSAGES_COLLECTION,
        key,
        {
            "status": "in_flight",
            "acquired_at": now.isoformat(),
            # ``cleanup_processed_messages`` filters on processed_at, so
            # in_flight markers need it too or they are never reaped.
            "processed_at": now.isoformat(),
            "handler": handler,
            "key": key,
        },
    )
    return False


async def mark_message_done(store: Any, key: str, *, handler: str) -> None:
    """Upgrade ``key``'s marker to ``done``. Best effort: a failure is logged, and the marker later expires."""
    try:
        await store.set(
            PROCESSED_MESSAGES_COLLECTION,
            key,
            {"status": "done", "processed_at": datetime.now(UTC).isoformat(), "handler": handler, "key": key},
        )
    except Exception as exc:
        logger.warning("[dedup] Failed to mark %s as done: %s", key, exc)


async def release_message_claim(store: Any, key: str) -> None:
    """Drop ``key``'s in-flight marker so a retry is not a duplicate. Best effort, logged."""
    try:
        await store.delete(PROCESSED_MESSAGES_COLLECTION, key)
    except Exception as exc:
        logger.warning("[dedup] Failed to release the in-flight marker %s: %s", key, exc)


__all__ = [
    "DEFAULT_INFLIGHT_TTL_SECONDS",
    "PROCESSED_MESSAGES_COLLECTION",
    "claim_message",
    "mark_message_done",
    "release_message_claim",
]
