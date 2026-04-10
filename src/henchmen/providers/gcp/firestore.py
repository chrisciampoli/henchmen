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

    async def increment(
        self,
        collection: str,
        document_id: str,
        field_deltas: dict[str, int | float],
    ) -> None:
        """Atomically add deltas via Firestore Increment transforms.

        Unlike a read-modify-write loop, the Increment transform is
        server-side and safe under any number of concurrent writers.
        """
        if not field_deltas:
            return
        payload: dict[str, Any] = {field: firestore.Increment(delta) for field, delta in field_deltas.items()}
        await self._client.collection(collection).document(document_id).update(payload)

    async def update_if(
        self,
        collection: str,
        document_id: str,
        expected_field: str,
        expected_value: Any,
        new_values: dict[str, Any],
    ) -> bool:
        """Conditionally update under a Firestore transaction."""
        doc_ref = self._client.collection(collection).document(document_id)
        transaction = self._client.transaction()

        @firestore.async_transactional
        async def _txn(txn: Any) -> bool:
            snapshot = await doc_ref.get(transaction=txn)
            if not snapshot.exists:
                return False
            current = snapshot.to_dict() or {}
            if current.get(expected_field) != expected_value:
                return False
            txn.update(doc_ref, new_values)
            return True

        result: bool = await _txn(transaction)
        return result
