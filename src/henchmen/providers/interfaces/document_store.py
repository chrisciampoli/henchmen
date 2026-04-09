"""DocumentStore interface — structured state persistence."""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DocumentStore(Protocol):
    """Abstraction over document databases (Firestore, DynamoDB, SQLite)."""

    async def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        """Get a document by ID. Returns None if not found."""
        ...

    async def set(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Create or overwrite a document."""
        ...

    async def update(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Partially update fields on an existing document."""
        ...

    async def delete(self, collection: str, document_id: str) -> None:
        """Delete a document."""
        ...

    async def query(
        self,
        collection: str,
        filters: list[tuple[str, str, Any]] | None = None,
        order_by: str | None = None,
        order_direction: str = "ASCENDING",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query documents with optional filters, ordering, and limit.

        Filters are tuples of (field, operator, value).
        Operators: ==, !=, <, <=, >, >=, in, not-in, array-contains.
        """
        ...
