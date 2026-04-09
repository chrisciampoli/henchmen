"""GCP Pub/Sub implementation of MessageBroker."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from google.cloud import pubsub_v1  # type: ignore[attr-defined]

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)


class PubSubMessageBroker:
    """MessageBroker backed by Google Cloud Pub/Sub."""

    def __init__(self, settings: Settings) -> None:
        self._project_id = settings.gcp_project_id
        self._client = pubsub_v1.PublisherClient()
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
        return str(future.result())

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

        response = subscriber.pull(
            request={"subscription": sub_path, "max_messages": max_messages}
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
            subscriber.acknowledge(request={"subscription": sub_path, "ack_ids": ack_ids})

        return messages
