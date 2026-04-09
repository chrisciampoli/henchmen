"""MessageBroker interface — publish/subscribe messaging between components."""

from typing import Any, Protocol, runtime_checkable


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

    async def pull_dlq(
        self,
        subscription_name: str,
        max_messages: int = 10,
    ) -> list[dict[str, Any]]:
        """Pull and acknowledge dead-lettered messages.

        Providers that do not have a native DLQ concept (e.g. the in-memory
        broker used for local development) should return an empty list.

        Args:
            subscription_name: Provider-specific subscription or queue
                identifier for the dead-letter destination (e.g. on GCP
                this is the DLQ subscription short-name; on AWS it would
                be the DLQ SQS URL).
            max_messages: Maximum number of messages to pull in one call.

        Returns:
            A list of dicts, one per dead-lettered message, with at least
            ``data`` (the raw message payload, decoded where possible),
            ``message_id``, and ``attributes``.  Implementations should
            acknowledge the messages they return so they do not pile up.
        """
        ...
