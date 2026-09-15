"""Tests for the internal route that gives an operative a fresh repo-scoped GitHub token (amendment A5)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from henchmen.config import internal_auth
from henchmen.config.settings import Settings
from henchmen.mastermind import internal_api
from henchmen.utils.github_auth import MAX_MIN_TTL_SECONDS, GitHubAuthError, InstallationToken

PATH = "/internal/tasks/{task_id}/github-token"
TASK_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
NODE_ID = "implement_fix"
LAIR_ID = "lair-0f8fad5b-implement-fix-1a2b3c"
BODY = {"node_id": NODE_ID, "operative_id": LAIR_ID}
MINTED_TOKEN = "ghs_refreshedTokenValue0123456789"
BEARER = "task-token-that-must-never-be-echoed-0123456789"
_APP = {
    "github_app_id": "4242",
    "github_app_installation_id": "77",
    "github_app_private_key_path": "/data/secrets/github-app.pem",
}


def _execution(**overrides: object) -> dict[str, object]:
    execution: dict[str, object] = {
        "task_id": TASK_ID,
        "execution_state": "running",
        "task_payload": {"id": TASK_ID, "context": {"repo": "acme/webapp", "branch": "main"}},
    }
    execution.update(overrides)
    return execution


@dataclass
class Harness:
    client: TestClient
    mint: AsyncMock
    store: MagicMock
    lair_manager: MagicMock
    settings: Settings

    def post(self, body: Any = BODY, **kwargs: Any) -> Any:
        return self.client.post(
            f"/internal/tasks/{TASK_ID}/github-token",
            json=body,
            headers={"Authorization": f"Bearer {BEARER}"},
            **kwargs,
        )


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    execution: dict[str, object] | None,
    app_settings: bool = True,
    minted: AsyncMock | None = None,
    launched: bool = True,
    min_ttl: int | None = 1500,
) -> Harness:
    from henchmen.mastermind import server

    settings = Settings(**{"_env_file": None, **(_APP if app_settings else {})})
    monkeypatch.setattr(internal_api, "get_settings", lambda: settings)
    mint = minted or AsyncMock(return_value=InstallationToken(token=MINTED_TOKEN, expires_at=1_900_000_000.0))
    monkeypatch.setattr(internal_api, "get_installation_token_async", mint)
    store = MagicMock()
    store.get = AsyncMock(return_value=execution)
    monkeypatch.setattr(internal_api, "task_store", lambda: store)
    agent = MagicMock()
    agent.lair_manager.accepts_report_from = MagicMock(return_value=launched)
    agent.lair_manager.operative_token_min_ttl_seconds = MagicMock(return_value=min_ttl)
    monkeypatch.setattr(server, "get_agent", lambda: agent)
    app = FastAPI()
    app.include_router(internal_api.router)
    app.dependency_overrides[internal_api.require_task_token] = lambda: None
    return Harness(TestClient(app), mint, store, agent.lair_manager, settings)


def test_route_requires_the_task_token() -> None:
    (route,) = [r for r in internal_api.router.routes if getattr(r, "path", "") == PATH]
    dependencies = [dependency.call for dependency in route.dependant.dependencies]
    # Exactly once: the router carries it, the route does not repeat it (M-3).
    assert dependencies.count(internal_api.require_task_token) == 1


@pytest.fixture
def desktop_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    internal_auth.clear_cache()
    yield tmp_path
    internal_auth.clear_cache()


def test_mastermind_serves_the_route_behind_the_task_token(desktop_data_dir: Path) -> None:
    from henchmen.mastermind import server

    client = TestClient(server.app, raise_server_exceptions=False)
    response = client.post(f"/internal/tasks/{TASK_ID}/github-token", json=BODY)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_wrong_task_token_gets_401_and_is_not_echoed(desktop_data_dir: Path) -> None:
    from henchmen.mastermind import server

    client = TestClient(server.app, raise_server_exceptions=False)
    response = client.post(
        f"/internal/tasks/{TASK_ID}/github-token", json=BODY, headers={"Authorization": f"Bearer {BEARER}"}
    )
    assert response.status_code == 401
    assert BEARER not in response.text


def test_returns_a_fresh_token_for_the_tasks_own_repository(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    harness = _harness(monkeypatch, execution=_execution(), min_ttl=1234)
    with caplog.at_level(logging.DEBUG):
        response = harness.post()
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"token", "expires_at"}
    assert body["token"] == MINTED_TOKEN
    assert datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")) == datetime.fromtimestamp(
        1_900_000_000.0, UTC
    )
    assert BEARER not in response.text
    assert harness.mint.await_args.args == ("acme/webapp",)
    assert harness.mint.await_args.kwargs == {"settings": harness.settings, "min_ttl_seconds": 1234}
    harness.store.get.assert_awaited_once_with("task_executions", TASK_ID)
    harness.lair_manager.accepts_report_from.assert_called_once_with(TASK_ID, NODE_ID, LAIR_ID)
    harness.lair_manager.operative_token_min_ttl_seconds.assert_called_once_with(TASK_ID, NODE_ID)
    assert MINTED_TOKEN not in caplog.text
    assert BEARER not in caplog.text


def test_a_clone_url_repository_is_scoped_as_owner_name(monkeypatch: pytest.MonkeyPatch) -> None:
    execution = _execution(task_payload={"context": {"repo": "https://github.com/acme/webapp.git"}})
    harness = _harness(monkeypatch, execution=execution)
    assert harness.post().status_code == 200
    assert harness.mint.await_args.args == ("acme/webapp",)


def test_without_a_github_app_there_is_nothing_to_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch, execution=_execution(), app_settings=False)
    response = harness.post()
    assert response.status_code == 409
    assert "token" not in response.json()
    harness.mint.assert_not_awaited()
    harness.store.get.assert_not_awaited()


def test_unknown_task(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch, execution=None)
    assert harness.post().status_code == 404
    harness.mint.assert_not_awaited()


@pytest.mark.parametrize("state", sorted(internal_api.TERMINAL_EXECUTION_STATES))
def test_finished_task_gets_no_token(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    harness = _harness(monkeypatch, execution=_execution(execution_state=state))
    assert harness.post().status_code == 409
    harness.mint.assert_not_awaited()
    harness.lair_manager.accepts_report_from.assert_not_called()


def test_a_lair_that_was_not_launched_gets_no_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    harness = _harness(monkeypatch, execution=_execution(), launched=False)
    with caplog.at_level(logging.WARNING, logger="henchmen.mastermind.internal_api"):
        response = harness.post()
    assert response.status_code == 409
    harness.mint.assert_not_awaited()
    assert "no matching active lair" in caplog.text


def test_a_lair_that_disappeared_after_the_binding_check_gets_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch, execution=_execution(), min_ttl=None)
    assert harness.post().status_code == 409
    harness.mint.assert_not_awaited()


def test_report_binding_uses_the_same_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(internal_api, "require_launched_lair", lambda *args: calls.append(args))
    report = MagicMock(task_id=TASK_ID, node_id=NODE_ID, operative_id=LAIR_ID)
    internal_api.require_report_from_launched_lair(report)
    assert calls == [(TASK_ID, NODE_ID, LAIR_ID)]


@pytest.mark.parametrize("payload", [{"context": {"repo": ""}}, {"context": {}}, {}, None])
def test_a_task_without_a_repository_gets_no_token(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    harness = _harness(monkeypatch, execution=_execution(task_payload=payload))
    assert harness.post().status_code == 404
    harness.mint.assert_not_awaited()


def test_an_unusable_repository_is_never_widened_to_the_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch, execution=_execution(task_payload={"context": {"repo": "acme/webapp/tree/main"}}))
    assert harness.post().status_code == 502
    harness.mint.assert_not_awaited()


def test_github_refusal_is_a_502_without_details(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = AsyncMock(side_effect=GitHubAuthError("GitHub refused to issue an installation token (HTTP 401: Bad)"))
    harness = _harness(monkeypatch, execution=_execution(), minted=failing)
    response = harness.post()
    assert response.status_code == 502
    assert "HTTP 401" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"node_id": NODE_ID},
        {"operative_id": LAIR_ID},
        {**BODY, "repo": "evil/other"},
        {"node_id": "", "operative_id": ""},
    ],
)
def test_a_malformed_body_is_422(monkeypatch: pytest.MonkeyPatch, body: dict[str, str]) -> None:
    harness = _harness(monkeypatch, execution=_execution())
    assert harness.post(body).status_code == 422
    harness.mint.assert_not_awaited()


def test_a_non_json_body_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch, execution=_execution())
    response = harness.client.post(f"/internal/tasks/{TASK_ID}/github-token", content=b"not json")
    assert response.status_code == 422
    harness.mint.assert_not_awaited()


def test_an_oversized_body_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch, execution=_execution())
    response = harness.post({"node_id": "x" * 5000, "operative_id": LAIR_ID})
    assert response.status_code == 413
    harness.mint.assert_not_awaited()


class TestOperativeTokenLifetime:
    """The refreshed token must outlive the lair's remaining budget plus grace, capped (amendment A5)."""

    @staticmethod
    def _manager(timeout: int, started_seconds_ago: float) -> Any:
        from henchmen.mastermind.lair_manager import LairManager

        manager = LairManager(Settings(_env_file=None, provider="local"))
        manager._active_lairs[LAIR_ID] = {
            "execution_id": "exec-1",
            "task_id": TASK_ID,
            "node_id": NODE_ID,
            "timeout_seconds": timeout,
            "created_at": (datetime.now(UTC) - timedelta(seconds=started_seconds_ago)).isoformat(),
        }
        return manager

    def test_remaining_budget_plus_grace(self) -> None:
        from henchmen.mastermind.lair_manager import _WAIT_GRACE_SECONDS

        ttl = self._manager(timeout=1800, started_seconds_ago=600).operative_token_min_ttl_seconds(TASK_ID, NODE_ID)
        assert ttl is not None
        assert 1200 + _WAIT_GRACE_SECONDS - 5 <= ttl <= 1200 + _WAIT_GRACE_SECONDS

    def test_capped_at_what_an_installation_token_guarantees(self) -> None:
        ttl = self._manager(timeout=7200, started_seconds_ago=0).operative_token_min_ttl_seconds(TASK_ID, NODE_ID)
        assert ttl == MAX_MIN_TTL_SECONDS

    def test_past_the_budget_only_the_grace_is_left(self) -> None:
        from henchmen.mastermind.lair_manager import _WAIT_GRACE_SECONDS

        ttl = self._manager(timeout=600, started_seconds_ago=700).operative_token_min_ttl_seconds(TASK_ID, NODE_ID)
        assert ttl == _WAIT_GRACE_SECONDS

    def test_unknown_lair(self) -> None:
        assert self._manager(timeout=600, started_seconds_ago=0).operative_token_min_ttl_seconds(TASK_ID, "x") is None
