"""DocumentStore an operative on a desktop install uses instead of the data volume.

It implements the :class:`~henchmen.providers.interfaces.document_store.DocumentStore`
protocol but supports only the three operations an operative performs, mapped
onto the task-scoped routes in :mod:`henchmen.mastermind.internal_api`:

* ``get("task_executions", task_id)`` -> ``GET .../{task_id}/cost``
* ``update("task_executions", task_id, {"last_heartbeat"})`` -> ``POST .../{task_id}/heartbeat``
* ``update("task_executions", task_id, {interrupted report fields})`` -> ``PUT .../{task_id}/interrupted-report``

Everything else raises :class:`OperationNotAllowedError` without sending a
request. Every call carries the operative's task token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from henchmen.observability.tracker import TASK_EXECUTIONS_COLLECTION

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

_TIMEOUT_SECONDS = 15.0
_HEARTBEAT_FIELDS = frozenset({"last_heartbeat"})
_INTERRUPTED_FIELDS = frozenset({"interrupted_node_id", "interrupted_at", "interrupted_report", "execution_state"})


class OperationNotAllowedError(PermissionError):
    """The operative asked for a document-store operation its task token does not grant."""


class HttpDocumentStore:
    """Task-scoped DocumentStore over HTTP for operative containers."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        base_url = settings.local_forward_base_url.strip().rstrip("/")
        token = settings.operative_task_token.strip()
        if not base_url:
            raise ValueError("HENCHMEN_LOCAL_FORWARD_BASE_URL is required for the operative HTTP document store")
        if not token:
            raise ValueError("HENCHMEN_OPERATIVE_TASK_TOKEN is required for the operative HTTP document store")
        self._base = f"{base_url}/mastermind/internal/tasks"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport

    def _url(self, collection: str, document_id: str, action: str) -> str:
        if collection != TASK_EXECUTIONS_COLLECTION:
            raise OperationNotAllowedError(f"collection {collection!r} is not available to operatives")
        if not document_id:
            raise OperationNotAllowedError("a task id is required")
        return f"{self._base}/{quote(document_id, safe='')}/{action}"

    async def _send(self, method: str, url: str, body: Any = None) -> httpx.Response:
        # trust_env=False: never read HTTP(S)_PROXY / NO_PROXY from the environment for this
        # loopback call to Mastermind. A configured proxy would otherwise receive the task token.
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=self._transport, trust_env=False) as client:
            return await client.request(method, url, headers=self._headers, json=body)

    async def get(self, collection: str, document_id: str) -> dict[str, Any] | None:
        """Return ``{"estimated_cost_usd": ...}`` for the operative's task, or ``None`` when it has no record."""
        response = await self._send("GET", self._url(collection, document_id, "cost"))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return {"estimated_cost_usd": float(response.json().get("estimated_cost_usd", 0.0) or 0.0)}

    async def update(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Send a heartbeat or an interrupted report; any other update is refused."""
        fields = frozenset(data)
        if fields == _HEARTBEAT_FIELDS:
            response = await self._send("POST", self._url(collection, document_id, "heartbeat"))
        elif fields == _INTERRUPTED_FIELDS:
            response = await self._send(
                "PUT", self._url(collection, document_id, "interrupted-report"), data["interrupted_report"]
            )
        else:
            raise OperationNotAllowedError(f"updating {sorted(fields)} is not available to operatives")
        response.raise_for_status()

    async def set(self, collection: str, document_id: str, data: dict[str, Any]) -> None:
        """Refused: operatives cannot create or overwrite a document."""
        raise OperationNotAllowedError("set is not available to operatives")

    async def delete(self, collection: str, document_id: str) -> None:
        """Refused: operatives cannot delete a document."""
        raise OperationNotAllowedError("delete is not available to operatives")

    async def query(
        self,
        collection: str,
        filters: list[tuple[str, str, Any]] | None = None,
        order_by: str | None = None,
        order_direction: str = "ASCENDING",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Refused: operatives cannot query collections."""
        raise OperationNotAllowedError("query is not available to operatives")

    async def increment(self, collection: str, document_id: str, field_deltas: dict[str, int | float]) -> None:
        """Refused: operatives cannot increment fields directly."""
        raise OperationNotAllowedError("increment is not available to operatives")

    async def update_if(
        self,
        collection: str,
        document_id: str,
        expected_field: str,
        expected_value: Any,
        new_values: dict[str, Any],
    ) -> bool:
        """Refused: operatives cannot perform a conditional update."""
        raise OperationNotAllowedError("update_if is not available to operatives")

    async def aclose(self) -> None:
        """Nothing to release: every call uses its own short-lived client."""
