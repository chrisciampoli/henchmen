"""SQLite implementation of DocumentStore for local development."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any


def _json_default(obj: object) -> str:
    """Handle non-serializable types (datetime, timedelta) for json.dumps."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


if TYPE_CHECKING:
    from henchmen.config.settings import Settings


class SQLiteDocumentStore:
    """DocumentStore backed by SQLite."""

    def __init__(self, settings: Settings, db_path: str | None = None) -> None:
        path = db_path or f"henchmen_{settings.environment.value}.db"
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Per-document asyncio locks serialize read-modify-write blocks
        # (increment, update_if) within the current process. Cross-process
        # serialization relies on SQLite's own write lock.
        self._doc_locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, collection: str, document_id: str) -> asyncio.Lock:
        """Return the per-document lock, lazily creating it on first use."""
        key = f"{collection}/{document_id}"
        lock = self._doc_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._doc_locks[key] = lock
        return lock

    def _ensure_table(self, collection: str) -> None:
        self._conn.execute(f"CREATE TABLE IF NOT EXISTS [{collection}] (id TEXT PRIMARY KEY, data TEXT NOT NULL)")

    async def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        """Retrieve a document by ID, or None if not found."""
        self._ensure_table(collection)
        row = self._conn.execute(f"SELECT data FROM [{collection}] WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            return None
        data: dict[str, Any] = json.loads(row[0])
        data["_id"] = document_id
        return data

    async def set(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Insert or replace a document."""
        self._ensure_table(collection)
        clean = {k: v for k, v in data.items() if k != "_id"}
        self._conn.execute(
            f"INSERT OR REPLACE INTO [{collection}] (id, data) VALUES (?, ?)",
            (document_id, json.dumps(clean, default=_json_default)),
        )
        self._conn.commit()

    async def update(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Merge data into an existing document, or create it if missing."""
        existing = await self.get(collection, document_id)
        if existing is None:
            await self.set(collection, document_id, data)
            return
        existing.pop("_id", None)
        existing.update(data)
        await self.set(collection, document_id, existing)

    async def delete(self, collection: str, document_id: str) -> None:
        """Delete a document by ID."""
        self._ensure_table(collection)
        self._conn.execute(f"DELETE FROM [{collection}] WHERE id = ?", (document_id,))
        self._conn.commit()

    async def query(
        self,
        collection: str,
        filters: list[tuple[str, str, Any]] | None = None,
        order_by: str | None = None,
        order_direction: str = "ASCENDING",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query documents with optional filters, ordering, and limit."""
        self._ensure_table(collection)
        rows = self._conn.execute(f"SELECT id, data FROM [{collection}]").fetchall()
        results = []
        for doc_id, raw in rows:
            data: dict[str, Any] = json.loads(raw)
            data["_id"] = doc_id
            if filters and not self._matches_filters(data, filters):
                continue
            results.append(data)
        if order_by:
            results.sort(key=lambda d: d.get(order_by, ""), reverse=(order_direction == "DESCENDING"))
        if limit:
            results = results[:limit]
        return results

    async def increment(
        self,
        collection: str,
        document_id: str,
        field_deltas: dict[str, int | float],
    ) -> None:
        """Atomically add deltas to numeric fields under a per-doc lock.

        Missing fields and missing documents are treated as zero. The
        ``asyncio.Lock`` keyed by ``(collection, document_id)`` serializes
        concurrent callers within this process; SQLite's file lock covers
        cross-process concurrency.
        """
        if not field_deltas:
            return
        async with self._get_lock(collection, document_id):
            existing = await self.get(collection, document_id)
            base: dict[str, Any] = {}
            if existing is not None:
                base = {k: v for k, v in existing.items() if k != "_id"}
            for field, delta in field_deltas.items():
                current = base.get(field, 0) or 0
                base[field] = current + delta
            await self.set(collection, document_id, base)

    async def update_if(
        self,
        collection: str,
        document_id: str,
        expected_field: str,
        expected_value: Any,
        new_values: dict[str, Any],
    ) -> bool:
        """Conditional update under a per-doc asyncio lock (compare-and-swap)."""
        async with self._get_lock(collection, document_id):
            existing = await self.get(collection, document_id)
            if existing is None:
                return False
            if existing.get(expected_field) != expected_value:
                return False
            merged = {k: v for k, v in existing.items() if k != "_id"}
            merged.update(new_values)
            await self.set(collection, document_id, merged)
            return True

    @staticmethod
    def _matches_filters(data: dict[str, Any], filters: list[tuple[str, str, Any]]) -> bool:
        for field, op, value in filters:
            actual = data.get(field)
            if op == "==" and actual != value:
                return False
            if op == "!=" and actual == value:
                return False
            if op == "<" and not (actual is not None and actual < value):
                return False
            if op == "<=" and not (actual is not None and actual <= value):
                return False
            if op == ">" and not (actual is not None and actual > value):
                return False
            if op == ">=" and not (actual is not None and actual >= value):
                return False
            if op == "in" and actual not in value:
                return False
            if op == "array-contains" and (not isinstance(actual, list) or value not in actual):
                return False
        return True
