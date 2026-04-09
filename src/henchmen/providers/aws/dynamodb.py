"""AWS DynamoDB implementation of DocumentStore."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


class DynamoDBDocumentStore:
    """DocumentStore backed by AWS DynamoDB (single-table design).

    Uses pk=collection, sk=document_id, data=JSON string.
    """

    def __init__(self, settings: Settings) -> None:
        import boto3

        region = getattr(settings, "aws_region", "us-east-1")
        table_name = getattr(settings, "aws_dynamodb_table", "henchmen")
        self._table: Any = boto3.resource("dynamodb", region_name=region).Table(table_name)

    async def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        """Fetch a document by ID. Returns None if not found."""
        response = await asyncio.to_thread(
            self._table.get_item,
            Key={"pk": collection, "sk": document_id},
        )
        item = response.get("Item")
        if not item:
            return None
        data: dict[str, Any] = json.loads(item["data"])
        data["_id"] = document_id
        return data

    async def set(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Create or replace a document."""
        clean = {k: v for k, v in data.items() if k != "_id"}
        await asyncio.to_thread(
            self._table.put_item,
            Item={"pk": collection, "sk": document_id, "data": json.dumps(clean)},
        )

    async def update(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Partially update fields on an existing document (merge)."""
        existing = await self.get(collection, document_id) or {}
        existing.pop("_id", None)
        existing.update({k: v for k, v in data.items() if k != "_id"})
        await self.set(collection, document_id, existing)

    async def delete(self, collection: str, document_id: str) -> None:
        """Delete a document."""
        await asyncio.to_thread(
            self._table.delete_item,
            Key={"pk": collection, "sk": document_id},
        )

    async def query(
        self,
        collection: str,
        filters: list[tuple[str, str, Any]] | None = None,
        order_by: str | None = None,
        order_direction: str = "ASCENDING",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query documents in a collection with optional in-memory filtering."""
        from boto3.dynamodb.conditions import Key

        response = await asyncio.to_thread(
            self._table.query,
            KeyConditionExpression=Key("pk").eq(collection),
        )
        items: list[dict[str, Any]] = []
        for item in response.get("Items", []):
            doc: dict[str, Any] = json.loads(item["data"])
            doc["_id"] = item["sk"]
            items.append(doc)

        # In-memory filtering
        if filters:
            for field, op, value in filters:
                if op == "==":
                    items = [d for d in items if d.get(field) == value]
                elif op == "!=":
                    items = [d for d in items if d.get(field) != value]
                elif op == "<":
                    items = [d for d in items if d.get(field) is not None and d[field] < value]
                elif op == "<=":
                    items = [d for d in items if d.get(field) is not None and d[field] <= value]
                elif op == ">":
                    items = [d for d in items if d.get(field) is not None and d[field] > value]
                elif op == ">=":
                    items = [d for d in items if d.get(field) is not None and d[field] >= value]
                elif op == "in":
                    items = [d for d in items if d.get(field) in value]
                elif op == "not-in":
                    items = [d for d in items if d.get(field) not in value]

        # In-memory sorting
        if order_by:
            reverse = order_direction == "DESCENDING"
            items.sort(key=lambda d: d.get(order_by, ""), reverse=reverse)

        if limit is not None:
            items = items[:limit]

        return items
