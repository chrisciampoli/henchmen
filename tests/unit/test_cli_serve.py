"""Tests for the ``henchmen serve`` combined app: sub-app lifespans really run.

Starlette never runs a mounted app's lifespan, so before this app entered them
explicitly the Slack bot never connected and ``/mastermind/metrics/*`` 404'd.
These tests drive the real sub-app lifespans through a TestClient.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def serve_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Hermetic local settings, and every global the combined app touches restored afterwards."""
    for key in [k for k in os.environ if k.startswith("HENCHMEN_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
    monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
    monkeypatch.setenv("HENCHMEN_LOCAL_SQLITE_PATH", str(tmp_path / "serve.db"))

    import henchmen.mastermind.server as mastermind_server
    import henchmen.providers.local.memory as memory
    from henchmen.config.settings import get_settings
    from henchmen.dispatch.server import app as dispatch_app
    from henchmen.forge.server import app as forge_app

    mastermind_app = mastermind_server.app
    sub_apps = (dispatch_app, mastermind_app, forge_app)
    saved_state = [dict(sub.state._state) for sub in sub_apps]
    saved_routes = [list(sub.router.routes) for sub in sub_apps]
    saved_agent = mastermind_server._agent
    saved_broker = memory.get_shared_broker()
    get_settings.cache_clear()
    mastermind_server._agent = None
    try:
        yield tmp_path
    finally:
        get_settings.cache_clear()
        mastermind_server._agent = saved_agent
        memory.set_shared_broker(saved_broker)  # type: ignore[arg-type]
        for sub, state, routes in zip(sub_apps, saved_state, saved_routes, strict=True):
            sub.state._state.clear()
            sub.state._state.update(state)
            sub.router.routes[:] = routes


def _build() -> object:
    from henchmen.cli.serve import build_serve_app
    from henchmen.config.settings import get_settings

    return build_serve_app(get_settings(), port=8765)


class TestSubAppLifespans:
    def test_dispatch_startup_runs_the_real_slack_bot_starter(
        self, serve_env: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No Slack tokens: the real ``start_socket_mode`` runs and logs why it is disabled."""
        app = _build()
        with caplog.at_level(logging.INFO), TestClient(app) as client:  # type: ignore[arg-type]
            assert client.get("/health").status_code == 200
        assert "Slack Socket Mode disabled" in caplog.text
        assert "[dispatch] Service started" in caplog.text
        assert "[mastermind] Service started" in caplog.text
        assert "[forge] Service started" in caplog.text

    def test_socket_mode_handler_started_and_closed(self, serve_env: Path) -> None:
        import henchmen.dispatch.slack_bot as slack_bot

        handler = MagicMock()
        app = _build()
        with (
            patch.object(slack_bot, "start_socket_mode", return_value=handler) as start,
            TestClient(app),  # type: ignore[arg-type]
        ):
            start.assert_called_once()
            handler.close.assert_not_called()
        handler.close.assert_called_once()

    def test_mastermind_metrics_routes_resolve(self, serve_env: Path) -> None:
        app = _build()
        with TestClient(app) as client:  # type: ignore[arg-type]
            response = client.get("/mastermind/metrics/summary")
            assert response.status_code == 200, response.text
            assert isinstance(response.json(), dict)
            assert client.get("/mastermind/metrics/tasks").status_code == 200


class TestSharedProviders:
    def test_one_document_store_shared_and_closed_on_shutdown(self, serve_env: Path) -> None:
        import sqlite3

        from henchmen.dispatch.server import app as dispatch_app
        from henchmen.forge.server import app as forge_app
        from henchmen.mastermind.server import app as mastermind_app
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        app = _build()
        with TestClient(app):  # type: ignore[arg-type]
            store = mastermind_app.state.document_store
            assert isinstance(store, SQLiteDocumentStore)
            # Forge's lifespan builds its own store; the combined app replaces it with the shared one.
            assert forge_app.state.document_store is store
            assert dispatch_app.state.message_broker is mastermind_app.state.message_broker
            assert forge_app.state.message_broker is mastermind_app.state.message_broker
        with pytest.raises(sqlite3.ProgrammingError):
            store._conn.execute("SELECT 1")

    def test_broker_drained_before_sub_app_shutdown(self, serve_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """In-flight forwards finish while the sub-apps can still serve them."""
        import henchmen.mastermind.server as mastermind_server

        order: list[str] = []
        real_close = mastermind_server._close_providers

        async def _close() -> None:
            order.append("mastermind-shutdown")
            await real_close()

        monkeypatch.setattr(mastermind_server, "_close_providers", _close)
        app = _build()
        with TestClient(app):  # type: ignore[arg-type]
            broker = mastermind_server.app.state.message_broker

            async def _drain() -> None:
                order.append("drain")

            broker.drain = _drain
        assert order == ["drain", "mastermind-shutdown"]


class TestSignalOwnership:
    @pytest.mark.asyncio
    async def test_sub_app_signal_handlers_are_not_installed(self) -> None:
        """A sub-app's SIGTERM handler would replace uvicorn's, so SIGTERM would no longer stop the server."""
        import asyncio
        import signal

        from henchmen.cli.serve import _uvicorn_owns_signals

        loop = asyncio.get_running_loop()
        installed: list[int] = []
        with _uvicorn_owns_signals(loop):
            loop.add_signal_handler(signal.SIGTERM, lambda: installed.append(1))
        assert "add_signal_handler" not in vars(loop)
        assert installed == []


# ---------------------------------------------------------------------------
# Setup mode, Console mount and restart-to-apply
# ---------------------------------------------------------------------------

from henchmen.cli.serve import (  # noqa: E402
    RESTART_EXIT_CODE,
    RestartSignal,
    build_serve_app,
    build_setup_app,
    console_url,
)
from henchmen.console.app import ConsoleMode, create_console_app  # noqa: E402
from henchmen.console.auth import ConsoleAuth  # noqa: E402
from henchmen.console.state import SetupStateStore  # noqa: E402

_LOCAL = "http://127.0.0.1:8000"


def _console_app(tmp_path: Path, mode: ConsoleMode = ConsoleMode.SETUP):
    return create_console_app(
        mode=mode,
        store=SetupStateStore(tmp_path / "setup-state.json"),
        auth=ConsoleAuth(setup_token="tok", signing_key=b"k" * 32),
        config_file=tmp_path / "henchmen.env",
        on_apply=lambda: None,
    )


def test_setup_app_serves_health_and_the_console(tmp_path: Path) -> None:
    client = TestClient(build_setup_app(_console_app(tmp_path)), base_url=_LOCAL)
    assert client.get("/health").json() == {"status": "ok", "mode": "setup"}
    assert client.get("/console/api/status").json()["mode"] == "setup"
    assert client.get("/").status_code == 200


def test_setup_app_does_not_expose_the_services(tmp_path: Path) -> None:
    client = TestClient(build_setup_app(_console_app(tmp_path)), base_url=_LOCAL)
    response = client.post("/dispatch/api/v1/tasks", json={}, headers={"origin": _LOCAL})
    assert response.status_code in {401, 404, 405}


def test_health_does_not_require_a_loopback_host(tmp_path: Path) -> None:
    """The launcher and operatives reach /health by container name."""
    client = TestClient(build_setup_app(_console_app(tmp_path)), base_url="http://henchmen:8000")
    assert client.get("/health").status_code == 200


def test_restart_signal_stops_the_attached_server() -> None:
    signal_ = RestartSignal()
    server = MagicMock()
    signal_.attach(server)
    assert signal_.requested is False
    signal_.request()
    assert signal_.requested is True
    assert server.should_exit is True


def test_restart_requested_before_attach_is_remembered() -> None:
    signal_ = RestartSignal()
    signal_.request()
    server = MagicMock()
    signal_.attach(server)
    assert server.should_exit is True


def test_restart_exit_code_is_three() -> None:
    assert RESTART_EXIT_CODE == 3


def test_console_url_carries_the_setup_token() -> None:
    assert console_url(8123, "abc") == "http://127.0.0.1:8123/console/session?setup_token=abc"


def test_serve_app_mounts_the_console_after_the_services(serve_env: Path) -> None:
    """Ruling R2: build_serve_app uses the serve_env fixture, not mock_settings alone —
    it builds a real document store, so the test must be hermetic the same way the
    pre-existing lifespan tests are.
    """
    from henchmen.config.settings import get_settings

    app = build_serve_app(get_settings(), 8000, console=_console_app(serve_env, ConsoleMode.RUN))
    route_paths = [getattr(route, "path", "") for route in app.routes]
    # Starlette reports a Mount("/") as path "".
    assert route_paths.index("") > route_paths.index("/health"), "the Console mount must come last"
    # No `with`: lifespans are not entered, so this checks routing only.
    client = TestClient(app, base_url=_LOCAL)
    assert client.get("/health").json()["mode"] == "local"
    assert client.get("/console/api/status").json()["mode"] == "run"


def test_serve_app_without_console_is_unchanged(serve_env: Path) -> None:
    from henchmen.config.settings import get_settings

    app = build_serve_app(get_settings(), 8000)
    assert "" not in [getattr(route, "path", "") for route in app.routes]


def _serve_args(port: int | None = 8123):
    import argparse

    return argparse.Namespace(host="127.0.0.1", port=port, log_level="info")


def test_serve_without_data_dir_runs_the_services(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch

    from henchmen.cli import _serve

    # Ruling R3: _serve writes HENCHMEN_LOCAL_SERVE_PORT to os.environ directly;
    # pre-registering it with monkeypatch guarantees teardown restores/clears it.
    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
    with (
        patch("henchmen.cli.serve.build_serve_app", return_value=MagicMock()) as build,
        patch("henchmen.cli.serve.serve_app", return_value=0) as run,
        pytest.raises(SystemExit) as exit_info,
    ):
        _serve(_serve_args())
    assert exit_info.value.code == 0
    assert build.call_args.kwargs["console"] is None
    assert run.call_args.kwargs["port"] == 8123


def test_serve_with_incomplete_setup_serves_only_the_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from unittest.mock import patch

    from henchmen.cli import _serve

    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HENCHMEN_CONSOLE_SETUP_TOKEN", "given-token")
    with (
        patch("henchmen.cli.serve.build_serve_app") as build_services,
        patch("henchmen.cli.serve.serve_app", return_value=RESTART_EXIT_CODE) as run,
        pytest.raises(SystemExit) as exit_info,
    ):
        _serve(_serve_args())
    assert exit_info.value.code == RESTART_EXIT_CODE
    build_services.assert_not_called()
    served = run.call_args.args[0]
    assert TestClient(served, base_url=_LOCAL).get("/health").json()["mode"] == "setup"
    assert "console/session?setup_token=given-token" in capsys.readouterr().out


def test_serve_with_completed_setup_mounts_the_console_in_run_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from unittest.mock import patch

    from henchmen.cli import _serve
    from henchmen.console.state import SetupState, SetupStep

    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    (tmp_path / "henchmen.env").write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")
    store = SetupStateStore(tmp_path / "setup-state.json")
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB], completed=True))
    with (
        patch("henchmen.cli.serve.build_serve_app", return_value=MagicMock()) as build,
        patch("henchmen.cli.serve.serve_app", return_value=0),
        pytest.raises(SystemExit),
    ):
        _serve(_serve_args())
    console = build.call_args.kwargs["console"]
    assert console is not None
    assert TestClient(console, base_url=_LOCAL).get("/console/api/status").json()["mode"] == "run"
