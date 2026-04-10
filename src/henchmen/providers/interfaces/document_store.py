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

    async def increment(
        self,
        collection: str,
        document_id: str,
        field_deltas: dict[str, int | float],
    ) -> None:
        """Atomically add ``delta`` to each field in ``field_deltas``.

        Missing fields or missing documents are treated as zero — the field
        is created and set to the delta. Intended for counters and cost
        accumulators where a read-modify-write loop would race under
        concurrent writers. Providers use native atomic primitives
        (Firestore ``Increment`` transforms, DynamoDB ``ADD`` update
        expressions, SQLite per-doc locks).
        """
        ...

    async def update_if(
        self,
        collection: str,
        document_id: str,
        expected_field: str,
        expected_value: Any,
        new_values: dict[str, Any],
    ) -> bool:
        """Conditionally update ``new_values`` if ``expected_field`` equals ``expected_value``.

        Returns ``True`` on a successful compare-and-swap, ``False`` if the
        precondition failed or the document does not exist. Intended for
        single-writer claim operations (e.g. FIFO merge queue dequeue)
        where two replicas may otherwise race and both claim the same
        entry. Providers use native primitives (Firestore transactions,
        DynamoDB ``ConditionExpression``, SQLite per-doc locks).
        """
        ...
