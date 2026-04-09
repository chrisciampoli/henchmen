"""GCP Firestore implementation of DocumentStore."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from google.cloud import firestore

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


class FirestoreDocumentStore:
    """DocumentStore backed by Google Cloud Firestore."""

    def __init__(self, settings: Settings) -> None:
        self._client = firestore.AsyncClient(
            project=settings.gcp_project_id,
            database=settings.firestore_database,
        )

    async def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        """Fetch a document by ID. Returns None if not found."""
        doc = await self._client.collection(collection).document(document_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        data["_id"] = doc.id
        return data

    async def set(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Create or replace a document."""
        await self._client.collection(collection).document(document_id).set(data)

    async def update(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Partially update a document (merge fields)."""
        await self._client.collection(collection).document(document_id).update(data)

    async def delete(self, collection: str, document_id: str) -> None:
        """Delete a document."""
        await self._client.collection(collection).document(document_id).delete()

    async def query(
        self,
        collection: str,
        filters: list[tuple[str, str, Any]] | None = None,
        order_by: str | None = None,
        order_direction: str = "ASCENDING",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query documents with optional filters, ordering, and limit."""
        ref: Any = self._client.collection(collection)
        if filters:
            for field, op, value in filters:
                ref = ref.where(field, op, value)
        if order_by:
            direction = firestore.Query.DESCENDING if order_direction == "DESCENDING" else firestore.Query.ASCENDING
            ref = ref.order_by(order_by, direction=direction)
        if limit:
            ref = ref.limit(limit)
        results = []
        async for doc in ref.stream():
            data = doc.to_dict() or {}
            data["_id"] = doc.id
            results.append(data)
        return results
