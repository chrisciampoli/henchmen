"""GCP Pub/Sub implementation of MessageBroker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from google.cloud import pubsub_v1  # type: ignore[attr-defined]

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


class PubSubMessageBroker:
    """MessageBroker backed by Google Cloud Pub/Sub."""

    def __init__(self, settings: Settings) -> None:
        self._project_id = settings.gcp_project_id
        self._client = pubsub_v1.PublisherClient()

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
