"""AWS DynamoDB implementation of DocumentStore."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


_RESERVED_ATTRS = {"pk", "sk", "data"}

_SUPPORTED_OPERATORS = frozenset({"==", "!=", "<", "<=", ">", ">=", "in", "not-in", "array-contains"})


def _to_dynamo(value: Any) -> Any:
    """Convert a Python value into something boto3 can serialize.

    boto3's ``TypeSerializer`` rejects ``float`` ("Float types are not
    supported. Use Decimal types instead.") and ``datetime`` outright, so
    floats become ``Decimal`` and datetimes become ISO-8601 strings — the
    same representation the SQLite provider writes.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_to_dynamo(v) for v in value]
    return value


def _decimal_to_native(value: Any) -> Any:
    """Coerce DynamoDB ``Decimal`` values back to native int/float, recursively.

    The boto3 resource interface returns numeric attributes as
    ``decimal.Decimal`` — including numbers nested inside Map and List
    attributes, which ``update`` writes for dict/list fields. Callers of
    ``DocumentStore.get`` expect JSON-friendly ints/floats, so we normalize
    on read.
    """
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, dict):
        return {k: _decimal_to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimal_to_native(v) for v in value]
    return value


class DynamoDBDocumentStore:
    """DocumentStore backed by AWS DynamoDB (single-table design).

    Storage layout: ``pk`` = collection, ``sk`` = document_id, ``data`` =
    JSON-encoded body. The document body is the source of truth for
    ``get``/``query``.

    To support the atomic primitives ``increment`` and ``update_if``, we
    also project counter fields and CAS-visible fields onto top-level
    DynamoDB attributes alongside the JSON blob:

    * ``increment`` uses ``UpdateExpression="ADD #f :v"`` against a
      top-level attribute — atomic at the server.
    * ``update_if`` uses ``UpdateExpression + ConditionExpression`` to
      compare-and-swap a top-level attribute.

    ``set`` / ``update`` additionally mirror every scalar value onto the
    top-level attribute namespace so that a fresh ``set`` followed by an
    ``update_if`` against an expected field sees a consistent value. And
    ``get``/``query`` merge top-level attributes (excluding pk/sk/data)
    back into the returned dict, letting top-level counters shadow the
    JSON blob when both exist.
    """

    def __init__(self, settings: Settings) -> None:
        import boto3

        self._table: Any = boto3.resource("dynamodb", region_name=settings.aws_region).Table(
            settings.aws_dynamodb_table
        )

    async def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        """Fetch a document by ID. Returns None if not found.

        Top-level DynamoDB attributes (anything other than pk/sk/data)
        are merged into the returned dict and take precedence over fields
        of the same name in the JSON blob — this is what lets ``increment``
        results become visible to subsequent ``get`` callers.
        """
        response = await asyncio.to_thread(
            self._table.get_item,
            Key={"pk": collection, "sk": document_id},
        )
        item = response.get("Item")
        if not item:
            return None
        raw = item.get("data")
        data: dict[str, Any] = json.loads(raw) if raw else {}
        # Overlay top-level attributes (atomic counters, CAS fields) so they
        # shadow the JSON blob where present.
        for attr, value in item.items():
            if attr in _RESERVED_ATTRS:
                continue
            data[attr] = _decimal_to_native(value)
        data["_id"] = document_id
        return data

    async def set(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Create or replace a document.

        Mirrors scalar values to top-level attributes so that a CAS
        precondition on the field works immediately after ``set``.
        """
        clean = {k: v for k, v in data.items() if k != "_id"}
        item: dict[str, Any] = {"pk": collection, "sk": document_id, "data": json.dumps(clean, default=str)}
        for key, value in clean.items():
            if key in _RESERVED_ATTRS:
                continue
            # Mirror scalar values onto the top-level attribute namespace
            # so CAS preconditions and atomic counters can target them.
            # None is omitted because DynamoDB rejects null attribute values.
            if isinstance(value, int | float | str | bool) and value is not None:
                item[key] = _to_dynamo(value)
        await asyncio.to_thread(self._table.put_item, Item=item)

    async def update(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Partially update fields on a document, creating it when missing.

        Implemented as a single ``UpdateExpression`` over the supplied fields
        only. The previous read-modify-write (get + full ``put_item``) raced
        with ``increment``: an atomic counter bumped between the read and the
        write was silently rolled back to its stale value.
        """
        fields = {k: v for k, v in data.items() if k != "_id" and k not in _RESERVED_ATTRS}
        if not fields:
            return
        name_map: dict[str, str] = {}
        value_map: dict[str, Any] = {}
        set_parts: list[str] = []
        for idx, (field, value) in enumerate(fields.items()):
            name_map[f"#u{idx}"] = field
            value_map[f":u{idx}"] = _to_dynamo(value)
            set_parts.append(f"#u{idx} = :u{idx}")
        await asyncio.to_thread(
            self._table.update_item,
            Key={"pk": collection, "sk": document_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=name_map,
            ExpressionAttributeValues=value_map,
        )

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
        """Query documents in a collection with optional in-memory filtering.

        DynamoDB Query returns at most 1 MB per call, so every page is
        followed: without that, filtering/ordering/limit would silently apply
        to the first page only.
        """
        from boto3.dynamodb.conditions import Key

        raw_items: list[dict[str, Any]] = []
        query_kwargs: dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(collection)}
        while True:
            response = await asyncio.to_thread(self._table.query, **query_kwargs)
            raw_items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key

        items: list[dict[str, Any]] = []
        for item in raw_items:
            raw = item.get("data")
            doc: dict[str, Any] = json.loads(raw) if raw else {}
            # Overlay top-level attributes so atomic counters show through.
            for attr, value in item.items():
                if attr in _RESERVED_ATTRS:
                    continue
                doc[attr] = _decimal_to_native(value)
            doc["_id"] = item["sk"]
            items.append(doc)

        # In-memory filtering
        if filters:
            for field, op, value in filters:
                if op not in _SUPPORTED_OPERATORS:
                    raise ValueError(f"Unsupported query operator: {op!r}")
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
                elif op == "array-contains":
                    items = [d for d in items if isinstance(d.get(field), list) and value in d[field]]

        # In-memory sorting
        if order_by:
            reverse = order_direction == "DESCENDING"
            items.sort(key=lambda d: d.get(order_by, ""), reverse=reverse)

        if limit is not None:
            items = items[:limit]

        return items

    async def increment(
        self,
        collection: str,
        document_id: str,
        field_deltas: dict[str, int | float],
    ) -> None:
        """Atomically add deltas via DynamoDB ``UpdateExpression`` ``ADD``.

        Each field becomes a top-level attribute on the item (not inside
        the JSON body). ``get`` and ``query`` overlay these top-level
        attributes back into the returned dict, so callers see the
        updated totals. Missing items are created by DynamoDB's upsert
        semantics.
        """
        if not field_deltas:
            return
        name_map: dict[str, str] = {}
        value_map: dict[str, Any] = {}
        add_parts: list[str] = []
        for idx, (field, delta) in enumerate(field_deltas.items()):
            name_placeholder = f"#f{idx}"
            value_placeholder = f":v{idx}"
            name_map[name_placeholder] = field
            value_map[value_placeholder] = _to_dynamo(delta)
            add_parts.append(f"{name_placeholder} {value_placeholder}")
        update_expression = "ADD " + ", ".join(add_parts)
        await asyncio.to_thread(
            self._table.update_item,
            Key={"pk": collection, "sk": document_id},
            UpdateExpression=update_expression,
            ExpressionAttributeNames=name_map,
            ExpressionAttributeValues=value_map,
        )

    async def update_if(
        self,
        collection: str,
        document_id: str,
        expected_field: str,
        expected_value: Any,
        new_values: dict[str, Any],
    ) -> bool:
        """Conditional update via DynamoDB ``ConditionExpression``.

        Returns ``False`` on ``ConditionalCheckFailedException`` (the
        precondition didn't match) or when the item does not exist.
        Writes target top-level attributes; the top-level overlay on
        ``get``/``query`` ensures subsequent reads see the CAS result
        without requiring the JSON ``data`` blob to be rewritten.
        """
        from botocore.exceptions import ClientError

        name_map: dict[str, str] = {"#cond": expected_field}
        value_map: dict[str, Any] = {":cond": _to_dynamo(expected_value)}
        set_parts: list[str] = []
        for idx, (field, value) in enumerate(new_values.items()):
            name_placeholder = f"#v{idx}"
            value_placeholder = f":nv{idx}"
            name_map[name_placeholder] = field
            value_map[value_placeholder] = _to_dynamo(value)
            set_parts.append(f"{name_placeholder} = {value_placeholder}")
        update_expression = "SET " + ", ".join(set_parts)
        condition_expression = "#cond = :cond"
        try:
            await asyncio.to_thread(
                self._table.update_item,
                Key={"pk": collection, "sk": document_id},
                UpdateExpression=update_expression,
                ConditionExpression=condition_expression,
                ExpressionAttributeNames=name_map,
                ExpressionAttributeValues=value_map,
            )
        except ClientError as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return False
            raise
        return True
