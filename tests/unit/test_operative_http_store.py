"""Operatives keep task state through task-scoped Mastermind routes, never the data volume (D-P4)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from henchmen.config.internal_auth import InternalAuth, load_internal_auth
from henchmen.config.settings import Settings
from henchmen.models.operative import OperativeReport, OperativeStatus
from henchmen.providers.local.http_store import HttpDocumentStore, OperationNotAllowedError

TASK = "task-1"


def _settings(token: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "provider": "local",
        "local_forward_base_url": "http://henchmen:8000",
        "operative_task_token": token,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _report(task_id: str = TASK, status: OperativeStatus = OperativeStatus.INTERRUPTED) -> OperativeReport:
    return OperativeReport(
        task_id=task_id,
        scheme_id="bugfix_standard",
        node_id="implement_fix",
        operative_id="op-1",
        status=status,
        summary="partial",
        confidence_score=0.1,
        started_at=datetime.now(UTC),
    )


@pytest.fixture
def internal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> InternalAuth:
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    return load_internal_auth(tmp_path / "secrets")


@pytest.fixture
def routes(internal: InternalAuth) -> Iterator[tuple[TestClient, MagicMock]]:
    from henchmen.mastermind import server

    store = MagicMock()
    store.get = AsyncMock(return_value={"estimated_cost_usd": 2.5, "task_payload": {"title": "not for operatives"}})
    store.update = AsyncMock()
    agent = MagicMock()
    agent.tracker._store = store
    with patch.object(server, "get_agent", return_value=agent):
        yield TestClient(server.app, raise_server_exceptions=False), store


def _auth(internal: InternalAuth, task_id: str = TASK) -> dict[str, str]:
    return {"Authorization": f"Bearer {internal.task_token(task_id)}"}


class TestRoutes:
    def test_cost_returns_only_the_cost(self, routes, internal) -> None:
        client, store = routes
        resp = client.get(f"/internal/tasks/{TASK}/cost", headers=_auth(internal))
        assert resp.status_code == 200
        assert resp.json() == {"estimated_cost_usd": 2.5}
        store.get.assert_awaited_once_with("task_executions", TASK)

    def test_missing_task_document_is_404(self, routes, internal) -> None:
        client, store = routes
        store.get.return_value = None
        assert client.get(f"/internal/tasks/{TASK}/cost", headers=_auth(internal)).status_code == 404

    @pytest.mark.parametrize("kind", ["none", "other-task", "push", "basic"])
    def test_wrong_credentials_are_rejected(self, routes, internal, kind: str) -> None:
        client, store = routes
        headers = {
            "none": {},
            "other-task": _auth(internal, "task-2"),
            "push": {"Authorization": f"Bearer {internal.push_token}"},
            "basic": {"Authorization": "Basic dXNlcjpwYXNz"},
        }[kind]
        for method, path in (("GET", "cost"), ("POST", "heartbeat")):
            resp = client.request(method, f"/internal/tasks/{TASK}/{path}", headers=headers)
            assert resp.status_code == 401
            assert resp.headers["WWW-Authenticate"] == "Bearer"
        store.get.assert_not_awaited()
        store.update.assert_not_awaited()

    def test_heartbeat_is_stamped_by_the_server(self, routes, internal) -> None:
        client, store = routes
        resp = client.post(
            f"/internal/tasks/{TASK}/heartbeat",
            headers=_auth(internal),
            json={"last_heartbeat": "2999-01-01T00:00:00+00:00"},
        )
        assert resp.status_code == 204
        collection, task_id, fields = store.update.await_args.args
        assert (collection, task_id, list(fields)) == ("task_executions", TASK, ["last_heartbeat"])
        stamped = datetime.fromisoformat(fields["last_heartbeat"])
        assert stamped.tzinfo is not None and stamped.year < 2999

    def test_interrupted_report_is_written(self, routes, internal) -> None:
        client, store = routes
        report = _report()
        resp = client.put(
            f"/internal/tasks/{TASK}/interrupted-report",
            headers=_auth(internal),
            content=report.model_dump_json(),
        )
        assert resp.status_code == 204
        _, _, fields = store.update.await_args.args
        assert set(fields) == {"interrupted_node_id", "interrupted_at", "interrupted_report", "execution_state"}
        assert fields["interrupted_node_id"] == "implement_fix"
        assert fields["execution_state"] == "interrupted"
        assert fields["interrupted_report"]["task_id"] == TASK

    @pytest.mark.parametrize(
        "report",
        [_report(task_id="task-2"), _report(status=OperativeStatus.COMPLETED)],
        ids=["other-task", "completed"],
    )
    def test_report_for_another_task_or_not_interrupted_is_refused(self, routes, internal, report) -> None:
        client, store = routes
        resp = client.put(
            f"/internal/tasks/{TASK}/interrupted-report", headers=_auth(internal), content=report.model_dump_json()
        )
        assert resp.status_code == 422
        store.update.assert_not_awaited()

    def test_authentication_is_checked_before_the_body(self, routes) -> None:
        client, _ = routes
        assert client.put(f"/internal/tasks/{TASK}/interrupted-report", content=b"{}").status_code == 401

    def test_malformed_task_id_is_refused(self, routes, internal) -> None:
        client, store = routes
        assert client.get("/internal/tasks/bad%20id/cost", headers=_auth(internal)).status_code == 422
        store.get.assert_not_awaited()

    def test_routes_do_not_exist_outside_a_desktop_install(self, monkeypatch, tmp_path) -> None:
        from henchmen.mastermind import server

        monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
        token = load_internal_auth(tmp_path / "elsewhere").task_token(TASK)
        client = TestClient(server.app, raise_server_exceptions=False)
        assert (
            client.get(f"/internal/tasks/{TASK}/cost", headers={"Authorization": f"Bearer {token}"}).status_code == 404
        )


class TestClientStore:
    @staticmethod
    def _store(handler) -> HttpDocumentStore:
        return HttpDocumentStore(_settings("t" * 64), transport=httpx.MockTransport(handler))

    @pytest.mark.asyncio
    async def test_calls_carry_the_token_and_target_the_task_routes(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/cost"):
                return httpx.Response(200, json={"estimated_cost_usd": 1.5})
            return httpx.Response(204)

        store = self._store(handler)
        assert await store.get("task_executions", TASK) == {"estimated_cost_usd": 1.5}
        await store.update("task_executions", TASK, {"last_heartbeat": "now"})
        report = _report().model_dump(mode="json")
        await store.update(
            "task_executions",
            TASK,
            {
                "interrupted_node_id": "n",
                "interrupted_at": "x",
                "interrupted_report": report,
                "execution_state": "interrupted",
            },
        )
        assert [(r.method, r.url.path) for r in seen] == [
            ("GET", "/mastermind/internal/tasks/task-1/cost"),
            ("POST", "/mastermind/internal/tasks/task-1/heartbeat"),
            ("PUT", "/mastermind/internal/tasks/task-1/interrupted-report"),
        ]
        assert all(r.url.host == "henchmen" for r in seen)
        assert all(r.headers["Authorization"] == "Bearer " + "t" * 64 for r in seen)
        assert json.loads(seen[2].content)["task_id"] == TASK

    @pytest.mark.asyncio
    async def test_missing_document_reads_as_none_and_errors_raise(self) -> None:
        store = self._store(lambda request: httpx.Response(404))
        assert await store.get("task_executions", TASK) is None
        failing = self._store(lambda request: httpx.Response(401))
        with pytest.raises(httpx.HTTPStatusError):
            await failing.update("task_executions", TASK, {"last_heartbeat": "now"})

    @pytest.mark.asyncio
    async def test_every_other_operation_is_refused_without_a_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request may be sent")

        store = self._store(handler)
        with pytest.raises(OperationNotAllowedError):
            await store.get("operative_reports", TASK)
        with pytest.raises(OperationNotAllowedError):
            await store.update("task_executions", TASK, {"estimated_cost_usd": 0})
        with pytest.raises(OperationNotAllowedError):
            await store.set("task_executions", TASK, {})
        with pytest.raises(OperationNotAllowedError):
            await store.delete("task_executions", TASK)
        with pytest.raises(OperationNotAllowedError):
            await store.query("task_executions")
        with pytest.raises(OperationNotAllowedError):
            await store.increment("task_executions", TASK, {"estimated_cost_usd": 1.0})
        with pytest.raises(OperationNotAllowedError):
            await store.update_if("task_executions", TASK, "a", 1, {})

    def test_needs_a_base_url_and_a_token(self) -> None:
        with pytest.raises(ValueError, match="LOCAL_FORWARD_BASE_URL"):
            HttpDocumentStore(_settings("t" * 64, local_forward_base_url=""))
        with pytest.raises(ValueError, match="OPERATIVE_TASK_TOKEN"):
            HttpDocumentStore(_settings(""))

    def test_registry_selects_it_only_with_a_task_token(self, tmp_path: Path) -> None:
        from henchmen.providers.local.sqlite import SQLiteDocumentStore
        from henchmen.providers.registry import ProviderRegistry

        assert isinstance(ProviderRegistry(_settings("t" * 64)).get_document_store(), HttpDocumentStore)
        sqlite_store = ProviderRegistry(_settings("", local_sqlite_path=str(tmp_path / "s.db"))).get_document_store()
        assert isinstance(sqlite_store, SQLiteDocumentStore)
        sqlite_store._conn.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_operative_code_round_trips_through_the_real_routes(internal: InternalAuth, tmp_path: Path) -> None:
    """Accumulator seed, heartbeat and interrupted report, end to end over ASGI into SQLite."""
    from henchmen.mastermind import server
    from henchmen.observability.cost_accumulator import TaskCostAccumulator
    from henchmen.operative.bootstrap import _persist_interrupted_report
    from henchmen.providers.local.sqlite import SQLiteDocumentStore

    backing = SQLiteDocumentStore(Settings(_env_file=None), db_path=str(tmp_path / "state.db"))  # type: ignore[call-arg]
    try:
        await backing.set("task_executions", TASK, {"estimated_cost_usd": 1.25})
        agent = MagicMock()
        agent.tracker._store = backing
        parent = FastAPI()
        parent.mount("/mastermind", server.app)
        store = HttpDocumentStore(_settings(internal.task_token(TASK)), transport=httpx.ASGITransport(app=parent))
        with patch.object(server, "get_agent", return_value=agent):
            assert await TaskCostAccumulator(store, TASK, ceiling_usd=6.0).current_total() == 1.25
            await store.update("task_executions", TASK, {"last_heartbeat": "ignored"})
            await _persist_interrupted_report(store, _report())

        doc = await backing.get("task_executions", TASK)
        assert doc is not None
        assert datetime.fromisoformat(doc["last_heartbeat"]).tzinfo is not None
        assert doc["interrupted_report"]["status"] == "interrupted"
        assert doc["estimated_cost_usd"] == 1.25
    finally:
        await backing.aclose()
