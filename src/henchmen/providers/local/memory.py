"""In-memory MessageBroker for local development."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any
from uuid import uuid4


class InMemoryMessageBroker:
    """MessageBroker backed by in-process async queues."""

    def __init__(self) -> None:
        self._messages: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._subscribers: dict[str, list[Callable[..., Any]]] = defaultdict(list)

    async def publish(self, topic: str, data: bytes, ordering_key: str | None = None, **attributes: str) -> str:
        """Publish a message to the given topic. Returns a local message ID."""
        msg_id = f"local-{uuid4().hex[:8]}"
        self._messages[topic].append({"id": msg_id, "data": data, "attributes": attributes})
        for callback in self._subscribers.get(topic, []):
            callback(data, **attributes)
        return msg_id

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
