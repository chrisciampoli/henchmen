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
