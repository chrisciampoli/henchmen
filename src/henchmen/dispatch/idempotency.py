"""In-process replay guard for webhook deliveries.

Slack retries an event up to three times when it does not get a 200 within
three seconds, and GitHub's "Redeliver" button re-sends a payload with the
same ``X-GitHub-Delivery`` id. Each delivery would otherwise produce a fresh
``HenchmenTask`` id and a fresh (paid) operative run.

Two layers guard against that:

1. This TTL set, which short-circuits a repeat delivery inside the process
   that saw the first one.
2. The ``dedup_key`` message attribute Dispatch attaches to every published
   task, which Mastermind checks against its document store — the
   authoritative, cross-instance layer.

This set is deliberately in-process: Dispatch stays free of state stores, and
a redelivery that lands on a different Cloud Run instance is still caught by
layer 2.
"""

import threading
import time

_DEFAULT_TTL_SECONDS = 600.0
_DEFAULT_MAX_ENTRIES = 10_000


class TTLSet:
    """A thread-safe set of keys that expire after a fixed time-to-live."""

    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        """Drop expired keys, then oldest-first if still over the cap."""
        expired = [key for key, stamp in self._seen.items() if now - stamp > self._ttl]
        for key in expired:
            del self._seen[key]
        overflow = len(self._seen) - self._max_entries
        if overflow > 0:
            for key, _ in sorted(self._seen.items(), key=lambda item: item[1])[:overflow]:
                del self._seen[key]

    def add_if_absent(self, key: str) -> bool:
        """Record *key* and return True if it had not been seen recently.

        Returns False when *key* is a replay of a delivery still inside the TTL
        window. An empty key is always treated as new (nothing to dedupe on).
        """
        if not key:
            return True
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if key in self._seen:
                return False
            self._seen[key] = now
            self._prune(now)
            return True

    def clear(self) -> None:
        """Forget every recorded key (used by tests)."""
        with self._lock:
            self._seen.clear()
