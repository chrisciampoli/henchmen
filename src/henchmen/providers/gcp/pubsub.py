"""GCP Pub/Sub implementation of MessageBroker."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from google.cloud import pubsub_v1  # type: ignore[attr-defined]

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)


class PubSubMessageBroker:
    """MessageBroker backed by Google Cloud Pub/Sub.

    Every SDK call is synchronous gRPC, so each one is dispatched through
    ``asyncio.to_thread``; running them inline would stall the FastAPI
    event loop for all concurrent requests whenever Pub/Sub is slow.
    """

    def __init__(self, settings: Settings) -> None:
        self._project_id = settings.gcp_project_id
        # Ordering has to be enabled when the client is built: publishing with a
        # non-empty ordering_key on a client without it raises ValueError.
        self._client = pubsub_v1.PublisherClient(
            publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
        )
        self._subscriber: Any | None = None

    def _get_subscriber(self) -> Any:
        """Lazy-init the subscriber client (only needed for DLQ pulls)."""
        if self._subscriber is None:
            self._subscriber = pubsub_v1.SubscriberClient()
        return self._subscriber

    async def publish(
        self,
        topic: str,
        data: bytes,
        ordering_key: str | None = None,
        **attributes: str,
    ) -> str:
        """Publish a message to a Pub/Sub topic. Returns message ID."""
        topic_path = self._client.topic_path(self._project_id, topic)
        kwargs: dict[str, Any] = {"data": data, **attributes}
        if ordering_key:
            kwargs["ordering_key"] = ordering_key
        future = self._client.publish(topic_path, **kwargs)
        # future.result() blocks until the batch is flushed and acknowledged.
        message_id = await asyncio.to_thread(future.result)
        return str(message_id)

    async def pull_dlq(
        self,
        subscription_name: str,
        max_messages: int = 10,
    ) -> list[dict[str, Any]]:
        """Pull and acknowledge dead-lettered messages from a Pub/Sub subscription.

        ``subscription_name`` is the short-name of the dead-letter
        subscription (e.g. ``henchmen-prod-dead-letter-sub``) — the
        full path is built against the configured project.
        """
        subscriber = self._get_subscriber()
        sub_path = f"projects/{self._project_id}/subscriptions/{subscription_name}"

        response = await asyncio.to_thread(
            subscriber.pull,
            request={"subscription": sub_path, "max_messages": max_messages},
        )

        messages: list[dict[str, Any]] = []
        ack_ids: list[str] = []
        for received in response.received_messages:
            ack_ids.append(received.ack_id)
            raw = received.message.data or b""
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError:
                decoded = raw.decode("utf-8", errors="replace")
            messages.append(
                {
                    "data": decoded,
                    "message_id": received.message.message_id,
                    "attributes": dict(received.message.attributes or {}),
                }
            )

        if ack_ids:
            await asyncio.to_thread(
                subscriber.acknowledge,
                request={"subscription": sub_path, "ack_ids": ack_ids},
            )

        return messages

    async def aclose(self) -> None:
        """Release the publisher/subscriber gRPC channels and worker threads."""
        if self._subscriber is not None:
            subscriber, self._subscriber = self._subscriber, None
            await asyncio.to_thread(subscriber.close)
        stop = getattr(self._client, "stop", None)
        if stop is not None:
            await asyncio.to_thread(stop)
