"""SQLite implementation of DocumentStore for local development."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

# Collection names become SQLite table names. They are internal constants
# today, but the name is interpolated into DDL/DML, so anything that is not a
# plain identifier is rejected rather than quoted-and-hoped.
_SAFE_COLLECTION = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")

_SUPPORTED_OPERATORS = frozenset({"==", "!=", "<", "<=", ">", ">=", "in", "not-in", "array-contains"})


def _json_default(obj: object) -> str:
    """Handle non-serializable types (datetime, timedelta) for json.dumps."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _normalize_filter_value(value: Any) -> Any:
    """Coerce a filter operand so it compares against stored JSON values.

    Documents are stored as JSON, so a ``datetime`` field round-trips as an
    ISO-8601 string. Comparing a stored string against a ``datetime`` operand
    raises ``TypeError``, so datetimes in filters are converted the same way
    :func:`_json_default` converts them on write.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list | tuple | set | frozenset):
        return [_normalize_filter_value(item) for item in value]
    return value


def default_db_path(settings: Settings) -> Path:
    """Return the SQLite file for this environment.

    ``local_sqlite_path`` wins when configured. Otherwise the file lives at
    ``~/.henchmen/henchmen_<env>.db``: a working-directory-relative default
    gave ``henchmen serve`` and ``henchmen chat`` started from different
    directories different databases, and dropped database files into
    whatever checkout the process was launched from.
    """
    configured = settings.local_sqlite_path.strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".henchmen" / f"henchmen_{settings.environment.value}.db"


class SQLiteDocumentStore:
    """DocumentStore backed by SQLite.

    Every statement runs through ``asyncio.to_thread`` guarded by a single
    ``threading.Lock``: the connection is shared across threads
    (``check_same_thread=False``) and sqlite3 connections are not safe for
    concurrent use.
    """

    def __init__(self, settings: Settings, db_path: str | None = None) -> None:
        path = Path(db_path) if db_path else default_db_path(settings)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._db_lock = threading.Lock()
        self._known_tables: set[str] = set()
        # Per-document asyncio locks serialize read-modify-write blocks
        # (update, increment, update_if) within the current process.
        # Cross-process serialization relies on SQLite's own write lock.
        self._doc_locks: dict[str, asyncio.Lock] = {}

    @property
    def path(self) -> Path:
        """Filesystem location of the SQLite database."""
        return self._path

    def _get_lock(self, collection: str, document_id: str) -> asyncio.Lock:
        """Return the per-document lock, lazily creating it on first use."""
        key = f"{collection}/{document_id}"
        lock = self._doc_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._doc_locks[key] = lock
        return lock

    @staticmethod
    def _check_collection(collection: str) -> str:
        if not _SAFE_COLLECTION.match(collection):
            raise ValueError(f"Unsafe collection name: {collection!r}")
        return collection

    def _ensure_table(self, collection: str) -> None:
        """Create the backing table once per process (caller holds the lock)."""
        if collection in self._known_tables:
            return
        self._conn.execute(f"CREATE TABLE IF NOT EXISTS [{collection}] (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self._conn.commit()
        self._known_tables.add(collection)

    async def _run(self, fn: Any, collection: str) -> Any:
        """Run a blocking DB callable off the event loop under the DB lock."""
        self._check_collection(collection)

        def _locked() -> Any:
            with self._db_lock:
                self._ensure_table(collection)
                return fn()

        return await asyncio.to_thread(_locked)

    async def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        """Retrieve a document by ID, or None if not found."""

        def _select() -> Any:
            return self._conn.execute(f"SELECT data FROM [{collection}] WHERE id = ?", (document_id,)).fetchone()

        row = await self._run(_select, collection)
        if row is None:
            return None
        data: dict[str, Any] = json.loads(row[0])
        data["_id"] = document_id
        return data

    async def set(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Insert or replace a document."""
        clean = {k: v for k, v in data.items() if k != "_id"}
        payload = json.dumps(clean, default=_json_default)

        def _write() -> None:
            self._conn.execute(
                f"INSERT OR REPLACE INTO [{collection}] (id, data) VALUES (?, ?)",
                (document_id, payload),
            )
            self._conn.commit()

        await self._run(_write, collection)

    async def update(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Merge data into an existing document, or create it if missing.

        Runs under the per-document lock so it cannot clobber a concurrent
        ``increment`` or ``update_if`` on the same document.
        """
        async with self._get_lock(collection, document_id):
            existing = await self.get(collection, document_id)
            if existing is None:
                await self.set(collection, document_id, data)
                return
            existing.pop("_id", None)
            existing.update(data)
            await self.set(collection, document_id, existing)

    async def delete(self, collection: str, document_id: str) -> None:
        """Delete a document by ID."""

        def _delete() -> None:
            self._conn.execute(f"DELETE FROM [{collection}] WHERE id = ?", (document_id,))
            self._conn.commit()

        await self._run(_delete, collection)

    async def query(
        self,
        collection: str,
        filters: list[tuple[str, str, Any]] | None = None,
        order_by: str | None = None,
        order_direction: str = "ASCENDING",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query documents with optional filters, ordering, and limit."""

        def _select_all() -> Any:
            return self._conn.execute(f"SELECT id, data FROM [{collection}]").fetchall()

        rows = await self._run(_select_all, collection)
        normalized = [(field, op, _normalize_filter_value(value)) for field, op, value in filters] if filters else None
        results = []
        for doc_id, raw in rows:
            data: dict[str, Any] = json.loads(raw)
            data["_id"] = doc_id
            if normalized and not self._matches_filters(data, normalized):
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

    async def aclose(self) -> None:
        """Close the underlying SQLite connection (idempotent).

        ``henchmen serve`` calls this on shutdown after draining the broker, so
        the WAL is checkpointed and the file handle released.
        """

        def _close() -> None:
            with self._db_lock:
                self._conn.close()

        await asyncio.to_thread(_close)

    async def close(self) -> None:
        """Alias for :meth:`aclose`."""
        await self.aclose()

    @staticmethod
    def _matches_filters(data: dict[str, Any], filters: list[tuple[str, str, Any]]) -> bool:
        for field, op, value in filters:
            if op not in _SUPPORTED_OPERATORS:
                raise ValueError(f"Unsupported query operator: {op!r}")
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
            if op == "not-in" and actual in value:
                return False
            if op == "array-contains" and (not isinstance(actual, list) or value not in actual):
                return False
        return True
