"""In-memory MessageBroker for local development."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

# Module-level singleton for single-process mode. When set, all calls to
# InMemoryMessageBroker() return this instance so Dispatch, Mastermind,
# and Forge share the same broker (and its forward map).
_shared_instance: InMemoryMessageBroker | None = None


def set_shared_broker(broker: InMemoryMessageBroker) -> None:
    """Designate *broker* as the process-wide singleton."""
    global _shared_instance
    _shared_instance = broker


def get_shared_broker() -> InMemoryMessageBroker | None:
    """Return the shared broker, or None if not in singleton mode."""
    return _shared_instance


class InMemoryMessageBroker:
    """MessageBroker backed by in-process async queues.

    Optionally forwards publishes as HTTP POSTs to simulate Pub/Sub push
    delivery when running all services in a single process.
    """

    def __new__(cls) -> InMemoryMessageBroker:
        if _shared_instance is not None:
            return _shared_instance
        return super().__new__(cls)

    def __init__(self) -> None:
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._messages: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._subscribers: dict[str, list[Callable[..., Any]]] = defaultdict(list)
        self._forward_map: dict[str, str] = {}
        # Strong references to in-flight forward tasks. Without this, the asyncio
        # event loop only holds weak references and background tasks can be
        # garbage collected mid-run (silent message loss in local dev).
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def drain(self) -> None:
        """Wait for all in-flight forward tasks to complete.

        Call this from an application lifespan shutdown hook to avoid losing
        in-transit messages on clean shutdown.
        """
        if not self._background_tasks:
            return
        await asyncio.gather(*self._background_tasks, return_exceptions=True)

    def set_forward_map(self, mapping: dict[str, str]) -> None:
        """Set topic -> URL mapping for HTTP forwarding.

        When a message is published to a topic in the map, an HTTP POST is
        sent to the URL with a Pub/Sub-style envelope. This simulates
        Pub/Sub push subscriptions for local development.
        """
        self._forward_map = mapping

    async def publish(self, topic: str, data: bytes, ordering_key: str | None = None, **attributes: str) -> str:
        """Publish a message to the given topic. Returns a local message ID."""
        msg_id = f"local-{uuid4().hex[:8]}"
        self._messages[topic].append({"id": msg_id, "data": data, "attributes": attributes})
        for callback in self._subscribers.get(topic, []):
            callback(data, **attributes)

        # HTTP forwarding (non-blocking, best-effort). Hold a strong reference
        # to the task and clean up via a done-callback to prevent GC from reaping
        # the in-flight forward before it completes.
        url = self._forward_map.get(topic)
        if url:
            task = asyncio.create_task(self._forward_to_http(url, msg_id, data, attributes))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return msg_id

    async def _forward_to_http(self, url: str, msg_id: str, data: bytes, attributes: dict[str, str]) -> None:
        """POST a Pub/Sub-style envelope to a local HTTP endpoint."""
        import httpx

        envelope = {
            "message": {
                "data": base64.b64encode(data).decode("utf-8"),
                "attributes": attributes,
                "messageId": msg_id,
            },
            "subscription": "local-dev",
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=envelope, timeout=1800)
                logger.debug("Forwarded %s to %s (status=%d)", msg_id, url, resp.status_code)
        except Exception as exc:
            logger.warning("HTTP forward failed for %s -> %s: %s", msg_id, url, exc)

    async def pull_dlq(
        self,
        subscription_name: str,
        max_messages: int = 10,
    ) -> list[dict[str, Any]]:
        """Return an empty list — the in-memory broker has no DLQ concept.

        Local dev does not dead-letter messages; failed handlers surface
        as exceptions in the same process.  Returning an empty list lets
        ``check_dlq_handler`` run in local mode without special-casing
        the provider.
        """
        return []

    def subscribe(self, topic: str, callback: Callable[..., Any]) -> None:
        """Register a callback to be invoked synchronously on publish."""
        self._subscribers[topic].append(callback)

    def get_messages(self, topic: str) -> list[dict[str, Any]]:
        """Return all messages published to a topic (for test inspection)."""
        return self._messages[topic]

    def clear(self) -> None:
        """Clear all stored messages and subscribers."""
        self._messages.clear()
        self._subscribers.clear()
