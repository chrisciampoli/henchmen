"""MessageBroker interface — publish/subscribe messaging between components."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class MessageBroker(Protocol):
    """Abstraction over pub/sub messaging (GCP Pub/Sub, AWS SNS+SQS, in-memory)."""

    async def publish(
        self,
        topic: str,
        data: bytes,
        ordering_key: str | None = None,
        **attributes: str,
    ) -> str:
        """Publish a message to a topic. Returns message ID."""
        ...
