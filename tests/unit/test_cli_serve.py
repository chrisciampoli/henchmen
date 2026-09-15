"""Tests for the ``henchmen serve`` combined app: sub-app lifespans really run.

Starlette never runs a mounted app's lifespan, so before this app entered them
explicitly the Slack bot never connected and ``/mastermind/metrics/*`` 404'd.
These tests drive the real sub-app lifespans through a TestClient.
"""

from __future__ import annotations

import json
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


def test_restart_exit_code_is_distinct_from_uvicorns_startup_failure_code() -> None:
    """75 (EX_TEMPFAIL) — must differ from uvicorn's own STARTUP_FAILURE (3), which
    serve_app also returns (for a server whose lifespan never started).
    """
    assert RESTART_EXIT_CODE == 75


class TestServeAppExitCodes:
    """serve_app against a stubbed uvicorn.Server: restart, startup failure, Ctrl+C."""

    def _stub_server(self, *, started: bool) -> MagicMock:
        server = MagicMock()
        server.started = started
        return server

    def test_restart_requested_returns_the_restart_exit_code(self) -> None:
        from henchmen.cli.serve import serve_app

        server = self._stub_server(started=True)
        with patch("henchmen.cli.serve.uvicorn.Server", return_value=server):
            restart = RestartSignal()
            restart.request()
            code = serve_app(MagicMock(), host="127.0.0.1", port=8000, log_level="info", restart=restart)
        assert code == RESTART_EXIT_CODE
        server.run.assert_called_once()

    def test_normal_stop_with_started_true_returns_zero(self) -> None:
        from henchmen.cli.serve import serve_app

        server = self._stub_server(started=True)
        with patch("henchmen.cli.serve.uvicorn.Server", return_value=server):
            code = serve_app(MagicMock(), host="127.0.0.1", port=8000, log_level="info", restart=RestartSignal())
        assert code == 0

    def test_started_false_returns_uvicorns_startup_failure_code(self) -> None:
        from henchmen.cli.serve import serve_app

        server = self._stub_server(started=False)
        with patch("henchmen.cli.serve.uvicorn.Server", return_value=server):
            code = serve_app(MagicMock(), host="127.0.0.1", port=8000, log_level="info", restart=RestartSignal())
        assert code == 3

    def test_restart_requested_wins_over_a_failed_startup(self) -> None:
        from henchmen.cli.serve import serve_app

        server = self._stub_server(started=False)
        with patch("henchmen.cli.serve.uvicorn.Server", return_value=server):
            restart = RestartSignal()
            restart.request()
            code = serve_app(MagicMock(), host="127.0.0.1", port=8000, log_level="info", restart=restart)
        assert code == RESTART_EXIT_CODE

    def test_keyboard_interrupt_is_swallowed_and_returns_zero(self) -> None:
        """Ctrl+C must not end in a traceback, matching uvicorn.run's own behaviour."""
        from henchmen.cli.serve import serve_app

        server = self._stub_server(started=True)
        server.run.side_effect = KeyboardInterrupt
        with patch("henchmen.cli.serve.uvicorn.Server", return_value=server):
            code = serve_app(MagicMock(), host="127.0.0.1", port=8000, log_level="info", restart=RestartSignal())
        assert code == 0

    def test_keyboard_interrupt_after_a_restart_request_still_restarts(self) -> None:
        from henchmen.cli.serve import serve_app

        server = self._stub_server(started=True)
        server.run.side_effect = KeyboardInterrupt
        with patch("henchmen.cli.serve.uvicorn.Server", return_value=server):
            restart = RestartSignal()
            restart.request()
            code = serve_app(MagicMock(), host="127.0.0.1", port=8000, log_level="info", restart=restart)
        assert code == RESTART_EXIT_CODE


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


def test_serve_without_data_dir_runs_the_services(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from unittest.mock import patch

    from henchmen.cli import _serve

    # Ruling R3: _serve writes HENCHMEN_LOCAL_SERVE_PORT to os.environ directly;
    # pre-registering it with monkeypatch guarantees teardown restores/clears it.
    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
    # Without a data dir, get_settings() reads .env.local/.env from the working
    # directory — chdir into an empty tmp_path so this never reads the repo's own.
    monkeypatch.chdir(tmp_path)
    with (
        patch("henchmen.cli.serve.build_serve_app", return_value=MagicMock()) as build,
        patch("henchmen.cli.serve.serve_app", return_value=0) as run,
        pytest.raises(SystemExit) as exit_info,
    ):
        _serve(_serve_args())
    assert exit_info.value.code == 0
    assert build.call_args.kwargs["console"] is None
    assert build.call_args.kwargs["desktop"] is None
    assert run.call_args.kwargs["port"] == 8123


def test_serve_with_incomplete_setup_serves_only_the_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from unittest.mock import patch

    from henchmen.cli import _serve

    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HENCHMEN_CONSOLE_SETUP_TOKEN", "g" * 43)
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
    assert f"console/session?setup_token={'g' * 43}" in capsys.readouterr().out


def test_serve_with_an_unreadable_setup_token_exits_two_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ruling B4 fix round, item 4: a broken token read must not surface as a traceback."""
    from unittest.mock import patch

    from henchmen.cli import _serve

    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HENCHMEN_CONSOLE_SETUP_TOKEN", "g" * 43)
    with (
        patch.object(ConsoleAuth, "setup_token", new_callable=lambda: property(lambda self: "")),
        pytest.raises(SystemExit) as exit_info,
    ):
        _serve(_serve_args())
    assert exit_info.value.code == 2
    assert "sign-in token" in capsys.readouterr().err


def test_serve_with_completed_setup_mounts_the_console_in_run_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from unittest.mock import patch

    from henchmen.cli import _serve
    from henchmen.console.state import SetupState, SetupStep

    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    # _serve_args() defaults to port 8123 (below), and the default container hostname is
    # "henchmen"; a forward base and Docker network matching both keeps this test in run
    # mode instead of the needs-attention mode ruling P3 (Task 11) now enters for the
    # unconfigured default (and, without a network, for the container hostname alone).
    (tmp_path / "henchmen.env").write_text(
        "HENCHMEN_PROVIDER=local\nHENCHMEN_LOCAL_FORWARD_BASE_URL=http://henchmen:8123\n"
        "HENCHMEN_LOCAL_DOCKER_NETWORK=henchmen\n",
        encoding="utf-8",
    )
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
    desktop = build.call_args.kwargs["desktop"]
    assert desktop.allowed_hostnames == frozenset({"127.0.0.1", "localhost", "::1", "henchmen"})


def test_run_mode_prints_the_port_settings_actually_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The printed URL must match the port build_serve_app/serve_app are bound to, which
    comes from Settings (and so can come from <data dir>/henchmen.env) — not the
    pre-Settings bootstrap fallback used only to bind the setup-mode-only Console.
    """
    from unittest.mock import patch

    from henchmen.cli import _serve
    from henchmen.console.state import SetupState, SetupStep

    # Ruling R3 pre-registers HENCHMEN_LOCAL_SERVE_PORT only to undo _serve's direct
    # os.environ write when args.port is not None; here port=None below means _serve
    # never writes it, and the env var must stay absent so the port comes only from
    # henchmen.env — so R3's pre-registration does not apply to this test.
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HENCHMEN_LOCAL_SERVE_PORT", raising=False)
    # A forward base and Docker network matching the resolved port and the default
    # container hostname keep this in run mode instead of ruling P3's needs-attention gate.
    (tmp_path / "henchmen.env").write_text(
        "HENCHMEN_PROVIDER=local\nHENCHMEN_LOCAL_SERVE_PORT=9999\n"
        "HENCHMEN_LOCAL_FORWARD_BASE_URL=http://henchmen:9999\nHENCHMEN_LOCAL_DOCKER_NETWORK=henchmen\n",
        encoding="utf-8",
    )
    store = SetupStateStore(tmp_path / "setup-state.json")
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB], completed=True))
    with (
        patch("henchmen.cli.serve.build_serve_app", return_value=MagicMock()),
        patch("henchmen.cli.serve.serve_app", return_value=0) as run,
        pytest.raises(SystemExit),
    ):
        # No --port and no HENCHMEN_LOCAL_SERVE_PORT in the environment: the only
        # source of the port is <data dir>/henchmen.env, read through Settings.
        _serve(_serve_args(port=None))
    assert run.call_args.kwargs["port"] == 9999
    assert "http://127.0.0.1:9999/console/session" in capsys.readouterr().out


def test_setup_mode_with_a_non_integer_port_env_exits_with_a_readable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from henchmen.cli import _serve

    # port=None below means _serve never writes HENCHMEN_LOCAL_SERVE_PORT itself, so
    # this monkeypatch.setenv is the only write and R3's pre-registration concern
    # (an untracked direct os.environ write) does not apply.
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "abc")
    with pytest.raises(SystemExit) as exit_info:
        _serve(_serve_args(port=None))
    assert exit_info.value.code == 2
    assert "HENCHMEN_LOCAL_SERVE_PORT" in capsys.readouterr().err


def test_corrupt_setup_state_file_exits_with_a_readable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from henchmen.cli import _serve

    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    state_file = tmp_path / "setup-state.json"
    state_file.write_text("not valid json", encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        _serve(_serve_args())
    assert exit_info.value.code == 2
    assert state_file.name in capsys.readouterr().err


def test_console_auth_load_permission_error_exits_with_a_readable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from unittest.mock import patch

    from henchmen.cli import _serve

    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    with (
        patch.object(ConsoleAuth, "load", side_effect=PermissionError("denied")),
        pytest.raises(SystemExit) as exit_info,
    ):
        _serve(_serve_args())
    assert exit_info.value.code == 2
    assert "secrets" in capsys.readouterr().err.lower()


def test_serve_logging_redacts_the_setup_token_from_uvicorn_access_lines(capsys: pytest.CaptureFixture[str]) -> None:
    """The sign-in URL is printed on purpose, but uvicorn must not write the token into its access log."""
    import uvicorn
    from fastapi import FastAPI

    from henchmen.cli.serve import configure_serve_logging

    original_factory = logging.getLogRecordFactory()
    try:
        configure_serve_logging("info")
        # serve_app builds exactly this Config, which installs uvicorn's own logging config.
        uvicorn.Config(FastAPI(), host="127.0.0.1", port=8000, log_level="info")
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "GET", "/console/session?setup_token=abc123", "1.1", 303
        )
    finally:
        logging.setLogRecordFactory(original_factory)
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "abc123" not in output
    assert "Logging error" not in output, "uvicorn's AccessFormatter needs the record's arguments intact"
    assert "GET /console/session?setup_token=***REDACTED*** HTTP/1.1" in output


def test_serve_installs_the_redacting_log_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from unittest.mock import patch

    from henchmen.cli import _serve

    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    with (
        patch("henchmen.cli.serve.configure_serve_logging") as configure,
        patch("henchmen.cli.serve.serve_app", return_value=0),
        pytest.raises(SystemExit),
    ):
        _serve(_serve_args())
    configure.assert_called_once_with("info")


def test_unreadable_setup_state_file_exits_with_a_readable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from unittest.mock import patch

    from henchmen.cli import _serve

    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    with (
        patch.object(SetupStateStore, "load", side_effect=PermissionError("denied")),
        pytest.raises(SystemExit) as exit_info,
    ):
        _serve(_serve_args())
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("ERROR:")
    assert "denied" in err
    assert "setup-state.json" in err


@pytest.mark.parametrize("data_dir_install", [True, False])
def test_invalid_settings_hint_names_the_file_setup_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], data_dir_install: bool
) -> None:
    from henchmen.cli import _build_settings_or_exit

    monkeypatch.chdir(tmp_path)
    if data_dir_install:
        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    else:
        monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "not-a-port")
    with pytest.raises(SystemExit) as exit_info:
        _build_settings_or_exit()
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    expected = str(tmp_path / "henchmen.env") if data_dir_install else ".env.local"
    assert f"to (re)write {expected}." in err


class TestDesktopHostAllowlist:
    """D-P2: in desktop mode the whole combined app refuses foreign Host names (DNS rebinding)."""

    @staticmethod
    def _client(host: str) -> TestClient:
        from henchmen.cli.serve import DesktopRuntime
        from henchmen.config.settings import get_settings
        from henchmen.console.auth import desktop_allowed_hostnames

        desktop = DesktopRuntime(allowed_hostnames=desktop_allowed_hostnames("henchmen"))
        app = build_serve_app(get_settings(), 8000, desktop=desktop)
        # A bracketed IPv6 literal (e.g. "[::1]:8000") in base_url itself trips
        # starlette's TestClient netloc parser (it splits on ":" without bracket
        # awareness), so the Host under test is sent as an explicit header on a
        # plain loopback base_url instead of folded into the connection URL —
        # exercising the same scope["headers"] the middleware actually reads.
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        client.headers["host"] = host
        return client

    @pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "henchmen:8000"])
    def test_allowed_hosts_reach_every_service(self, serve_env: Path, host: str) -> None:
        client = self._client(host)
        for path in ("/dispatch/health", "/mastermind/health", "/forge/health"):
            assert client.get(path).status_code == 200

    @pytest.mark.parametrize("host", ["evil.example:8000", "127.0.0.1.evil.example:8000", "henchmen.evil.example"])
    def test_other_hosts_are_refused_on_every_service(self, serve_env: Path, host: str) -> None:
        client = self._client(host)
        for path in ("/dispatch/health", "/mastermind/health", "/forge/health"):
            assert client.get(path).status_code == 403
        assert client.post("/dispatch/api/v1/tasks", json={"title": "T"}).status_code == 403

    def test_health_answers_any_host(self, serve_env: Path) -> None:
        assert self._client("evil.example:8000").get("/health").status_code == 200

    def test_without_a_desktop_runtime_hosts_are_not_checked(self, serve_env: Path) -> None:
        from henchmen.config.settings import get_settings

        app = build_serve_app(get_settings(), 8000)
        assert TestClient(app, base_url="http://evil.example:8000").get("/mastermind/health").status_code == 200


def test_desktop_run_mode_serves_attention_when_the_forward_host_is_not_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Ruling P3: the default local_forward_base (host.docker.internal) would be silently
    refused by the allowlist -- every operative would fail with an opaque 403 -- so _serve logs
    it as a WARNING (Task 4 behaviour, kept) and now serves the needs-attention Console with the
    fix instead of starting services that could never complete a task (Task 11 supersedes the
    Task 4 warn-and-continue behaviour).
    """
    from unittest.mock import patch

    from henchmen.cli import _serve
    from henchmen.console.state import SetupState, SetupStep

    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    (tmp_path / "henchmen.env").write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")
    store = SetupStateStore(tmp_path / "setup-state.json")
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB], completed=True))
    with (
        patch("henchmen.cli.serve.build_serve_app", return_value=MagicMock()) as build_services,
        patch("henchmen.cli.serve.serve_app", return_value=0) as run,
        caplog.at_level(logging.WARNING, logger="henchmen"),
        pytest.raises(SystemExit) as exit_info,
    ):
        _serve(_serve_args())
    assert exit_info.value.code == 0
    build_services.assert_not_called()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "expected a WARNING about the unreachable forward host"
    assert "host.docker.internal" in caplog.text
    # _serve_args() defaults to port 8123, which _serve writes to HENCHMEN_LOCAL_SERVE_PORT
    # (overriding the pre-registered "8000") before Settings resolves local_serve_port.
    assert "HENCHMEN_LOCAL_FORWARD_BASE_URL=http://henchmen:8123" in caplog.text
    status = TestClient(run.call_args.args[0], base_url=_LOCAL).get("/console/api/status").json()
    assert status["mode"] == "attention"
    assert any("host.docker.internal" in p for p in status["problems"])


def test_desktop_runtime_authenticates_the_shared_broker(serve_env: Path) -> None:
    import henchmen.providers.local.memory as memory
    from henchmen.cli.serve import DesktopRuntime
    from henchmen.config.settings import get_settings

    desktop = DesktopRuntime(allowed_hostnames=frozenset({"localhost"}), internal_push_token="p" * 43)
    build_serve_app(get_settings(), 8000, desktop=desktop)
    broker = memory.get_shared_broker()
    assert broker is not None and broker._forward_token == "p" * 43

    build_serve_app(get_settings(), 8000)
    assert memory.get_shared_broker()._forward_token is None  # type: ignore[union-attr]


def test_run_mode_loads_the_internal_push_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from henchmen.cli import _serve
    from henchmen.config.internal_auth import load_internal_auth
    from henchmen.console.state import SetupState, SetupStep

    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    # A forward base and Docker network matching _serve_args()'s default port (8123) and the
    # default container hostname keep this in run mode instead of ruling P3's attention gate.
    (tmp_path / "henchmen.env").write_text(
        "HENCHMEN_PROVIDER=local\nHENCHMEN_LOCAL_FORWARD_BASE_URL=http://henchmen:8123\n"
        "HENCHMEN_LOCAL_DOCKER_NETWORK=henchmen\n",
        encoding="utf-8",
    )
    SetupStateStore(tmp_path / "setup-state.json").save(
        SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB], completed=True)
    )
    with (
        patch("henchmen.cli.serve.build_serve_app", return_value=MagicMock()) as build,
        patch("henchmen.cli.serve.serve_app", return_value=0),
        pytest.raises(SystemExit),
    ):
        _serve(_serve_args())
    desktop = build.call_args.kwargs["desktop"]
    assert desktop.internal_push_token == load_internal_auth(tmp_path / "secrets").push_token


# ---------------------------------------------------------------------------
# Task 11: service health, the attention app and serve_app's own exit codes
# ---------------------------------------------------------------------------


def test_attention_app_serves_degraded_health_and_the_console(tmp_path: Path) -> None:
    from henchmen.cli.serve import build_attention_app

    client = TestClient(build_attention_app(_console_app(tmp_path, ConsoleMode.ATTENTION)), base_url=_LOCAL)
    assert client.get("/health").json() == {"status": "degraded", "mode": "attention"}
    assert client.get("/console/api/status").json()["mode"] == "attention"


def test_attention_app_exposes_no_service_routes(tmp_path: Path) -> None:
    """Ruling 2: the attention app serves /health plus the Console, and no service routes."""
    from henchmen.cli.serve import build_attention_app

    client = TestClient(build_attention_app(_console_app(tmp_path, ConsoleMode.ATTENTION)), base_url=_LOCAL)
    for path in ("/dispatch/health", "/mastermind/health", "/forge/health"):
        assert client.get(path).status_code == 404


def test_service_health_tracks_a_normal_start_and_stop(serve_env: Path) -> None:
    from henchmen.config.settings import get_settings
    from henchmen.console.services import ServiceHealth

    health = ServiceHealth()
    app = build_serve_app(get_settings(), 8000, health=health)
    with TestClient(app):  # type: ignore[arg-type]
        assert health.snapshot() == {"dispatch": "running", "mastermind": "running", "forge": "running"}
    assert health.snapshot() == {"dispatch": "off", "mastermind": "off", "forge": "off"}
    assert health.startup_error is None


def test_a_service_startup_failure_is_recorded_and_re_raised(serve_env: Path) -> None:
    import henchmen.dispatch.slack_bot as slack_bot
    from henchmen.config.settings import get_settings
    from henchmen.console.services import ServiceHealth

    health = ServiceHealth()
    app = build_serve_app(get_settings(), 8000, health=health)
    with (
        patch.object(slack_bot, "start_socket_mode", side_effect=RuntimeError("socket mode exploded")),
        pytest.raises(RuntimeError, match="socket mode exploded"),
        TestClient(app),  # type: ignore[arg-type]
    ):
        pass
    assert isinstance(health.startup_error, RuntimeError)
    assert health.snapshot() == {"dispatch": "failed", "mastermind": "off", "forge": "off"}


def test_a_second_sub_apps_failure_still_closes_the_store_and_releases_the_broker(
    serve_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Ruling 7: dispatch's lifespan is entered first; when mastermind's then fails,
    AsyncExitStack unwinds dispatch's already-entered context manager (its own __aexit__
    runs, so it is not left dangling), and -- regardless of what that unwind itself does --
    the shared store the combined app owns is closed and the broker singleton is released
    before any needs-attention fallback."""
    import sqlite3

    import henchmen.mastermind.server as mastermind_server
    import henchmen.providers.local.memory as memory
    from henchmen.config.settings import get_settings
    from henchmen.console.services import ServiceHealth
    from henchmen.providers.local.sqlite import SQLiteDocumentStore

    health = ServiceHealth()
    app = build_serve_app(get_settings(), 8000, health=health)
    # Set on app.state before any lifespan runs (build_serve_app seeds it synchronously),
    # so this reference is valid even though mastermind's own lifespan never reaches RUNNING.
    store = mastermind_server.app.state.document_store
    assert isinstance(store, SQLiteDocumentStore)
    with (
        patch.object(mastermind_server, "get_agent", side_effect=RuntimeError("mastermind boom")),
        caplog.at_level(logging.INFO),
        pytest.raises(RuntimeError, match="mastermind boom"),
        TestClient(app),  # type: ignore[arg-type]
    ):
        pass
    assert "[dispatch] Service started" in caplog.text, "dispatch must have started before mastermind failed"
    assert health.snapshot() == {"dispatch": "off", "mastermind": "failed", "forge": "off"}
    assert memory.get_shared_broker() is None
    with pytest.raises(sqlite3.ProgrammingError):
        store._conn.execute("SELECT 1")


def test_a_failed_lifespan_does_not_leak_the_broker_or_the_store(serve_env: Path) -> None:
    """Ruling: no resource leak into a same-process fallback -- a failed startup must not
    leave its broker as the process-wide singleton, and a fresh build_serve_app afterwards
    must get a working broker and store, never a closed/abandoned one."""
    import henchmen.dispatch.slack_bot as slack_bot
    import henchmen.providers.local.memory as memory
    from henchmen.config.settings import get_settings
    from henchmen.console.services import ServiceHealth

    health = ServiceHealth()
    app = build_serve_app(get_settings(), 8000, health=health)
    with (
        patch.object(slack_bot, "start_socket_mode", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError),
        TestClient(app),  # type: ignore[arg-type]
    ):
        pass
    assert memory.get_shared_broker() is None

    health2 = ServiceHealth()
    app2 = build_serve_app(get_settings(), 8000, health=health2)
    with TestClient(app2):  # type: ignore[arg-type]
        assert health2.snapshot() == {"dispatch": "running", "mastermind": "running", "forge": "running"}
    assert memory.get_shared_broker() is None


class TestServeAppSystemExit:
    @pytest.mark.parametrize("code", [1, 3])
    def test_uvicorn_system_exit_is_returned_not_raised(self, code: int) -> None:
        from henchmen.cli.serve import serve_app

        server = MagicMock()
        server.started = False
        server.run.side_effect = SystemExit(code)
        with patch("henchmen.cli.serve.uvicorn.Server", return_value=server):
            assert (
                serve_app(MagicMock(), host="127.0.0.1", port=8000, log_level="info", restart=RestartSignal()) == code
            )


def _completed_setup(data_dir: Path, config: str) -> None:
    from henchmen.console.state import SetupState, SetupStep

    (data_dir / "henchmen.env").write_text(config, encoding="utf-8")
    SetupStateStore(data_dir / "setup-state.json").save(
        SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB], completed=True)
    )


# A forward base and Docker network matching the default container hostname and
# _serve_args()'s default port (8123), so tests that are not about ruling P3 exercise
# service/bind failures instead of needs-attention mode for the unconfigured default
# forward base or the container-hostname-without-a-network case.
_FORWARD_OK = "HENCHMEN_LOCAL_FORWARD_BASE_URL=http://henchmen:8123\nHENCHMEN_LOCAL_DOCKER_NETWORK=henchmen\n"


class TestNeedsAttention:
    """D-P5: a completed setup that cannot start serves the Console instead of exiting."""

    @pytest.fixture(autouse=True)
    def _data_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "8000")
        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))

    @staticmethod
    def _status(app: object) -> dict[str, object]:
        client = TestClient(app, base_url=_LOCAL)  # type: ignore[arg-type]
        assert client.get("/health").json() == {"status": "degraded", "mode": "attention"}
        return client.get("/console/api/status").json()

    def test_settings_that_do_not_build_serve_the_attention_console(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from henchmen.cli import _serve

        _completed_setup(tmp_path, "HENCHMEN_PROVIDER=local\nHENCHMEN_OPERATIVE_TASK_COST_CEILING_USD=abc\n")
        with (
            patch("henchmen.cli.serve.build_serve_app") as build_services,
            patch("henchmen.cli.serve.serve_app", return_value=0) as run,
            pytest.raises(SystemExit) as exit_info,
        ):
            _serve(_serve_args())
        assert exit_info.value.code == 0
        build_services.assert_not_called()
        status = self._status(run.call_args.args[0])
        assert status["mode"] == "attention"
        assert any("operative_task_cost_ceiling_usd" in p for p in status["problems"])  # type: ignore[attr-defined]
        assert run.call_args.kwargs["port"] == 8123
        assert "Henchmen needs attention" in capsys.readouterr().out

    def test_runtime_problems_serve_the_attention_console(self, tmp_path: Path) -> None:
        from henchmen.cli import _serve

        _completed_setup(tmp_path, "HENCHMEN_PROVIDER=local\nHENCHMEN_LLM_PROVIDER=anthropic\n")
        with (
            patch("henchmen.cli.serve.build_serve_app") as build_services,
            patch("henchmen.cli.serve.serve_app", return_value=0) as run,
            pytest.raises(SystemExit),
        ):
            _serve(_serve_args())
        build_services.assert_not_called()
        problems = self._status(run.call_args.args[0])["problems"]
        assert any("HENCHMEN_ANTHROPIC_API_KEY" in p for p in problems)  # type: ignore[attr-defined]

    def test_a_service_startup_failure_falls_back_without_exiting(self, tmp_path: Path) -> None:
        from henchmen.cli import _serve
        from henchmen.cli.serve import STARTUP_FAILURE

        _completed_setup(tmp_path, "HENCHMEN_PROVIDER=local\n" + _FORWARD_OK)
        leaked = "ghp_" + "z" * 36

        def failing_build(settings, port, *, console, desktop, health):
            health.record_startup_failure("dispatch", RuntimeError(f"Slack refused {leaked}"))
            return MagicMock()

        with (
            patch("henchmen.cli.serve.build_serve_app", side_effect=failing_build),
            patch("henchmen.cli.serve.serve_app", side_effect=[STARTUP_FAILURE, 0]) as run,
            pytest.raises(SystemExit) as exit_info,
        ):
            _serve(_serve_args())
        assert exit_info.value.code == 0
        status = self._status(run.call_args_list[1].args[0])
        assert any("A Henchmen service failed to start" in p for p in status["problems"])  # type: ignore[attr-defined]
        assert leaked not in json.dumps(status)

    def test_build_serve_app_raising_falls_back_to_attention_and_releases_the_broker(self, tmp_path: Path) -> None:
        """Ruling 3: an exception raised while *building* the combined app (a sub-app import, or
        registry.get_document_store()/get_container_orchestrator()) must also fall back to
        attention mode, and must not leave the shared-broker singleton pointing at whatever
        build_serve_app had already set before it failed."""
        import henchmen.providers.local.memory as memory
        from henchmen.cli import _serve

        _completed_setup(tmp_path, "HENCHMEN_PROVIDER=local\n" + _FORWARD_OK)
        with (
            patch("henchmen.cli.serve.build_serve_app", side_effect=RuntimeError("disk full")),
            patch("henchmen.cli.serve.serve_app", return_value=0) as run,
            pytest.raises(SystemExit) as exit_info,
        ):
            _serve(_serve_args())
        assert exit_info.value.code == 0
        status = self._status(run.call_args.args[0])
        assert any("A Henchmen service failed to start" in p for p in status["problems"])  # type: ignore[attr-defined]
        assert memory.get_shared_broker() is None

    def test_attention_port_falls_back_to_the_config_file_when_settings_and_the_env_var_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ruling 4: --port, then HENCHMEN_LOCAL_SERVE_PORT, then the port saved in the config
        file, then 8000 -- this exercises the third, file-based fallback."""
        from henchmen.cli import _serve

        monkeypatch.delenv("HENCHMEN_LOCAL_SERVE_PORT", raising=False)
        _completed_setup(
            tmp_path,
            "HENCHMEN_PROVIDER=local\nHENCHMEN_LOCAL_SERVE_PORT=9321\nHENCHMEN_OPERATIVE_TASK_COST_CEILING_USD=abc\n",
        )
        with (
            patch("henchmen.cli.serve.serve_app", return_value=0) as run,
            pytest.raises(SystemExit),
        ):
            _serve(_serve_args(port=None))
        assert run.call_args.kwargs["port"] == 9321

    def test_a_bind_failure_is_not_attention_mode(self, tmp_path: Path) -> None:
        from henchmen.cli import _serve

        _completed_setup(tmp_path, "HENCHMEN_PROVIDER=local\n" + _FORWARD_OK)
        with (
            patch("henchmen.cli.serve.build_serve_app", return_value=MagicMock()),
            patch("henchmen.cli.serve.serve_app", return_value=1) as run,
            pytest.raises(SystemExit) as exit_info,
        ):
            _serve(_serve_args())
        assert exit_info.value.code == 1
        assert run.call_count == 1

    @pytest.mark.parametrize(("attention_code", "exit_code"), [(RESTART_EXIT_CODE, 75), (1, 3), (3, 3)])
    def test_attention_mode_restarts_on_apply_and_exits_3_when_it_cannot_start(
        self, tmp_path: Path, attention_code: int, exit_code: int
    ) -> None:
        from henchmen.cli import _serve

        _completed_setup(tmp_path, "HENCHMEN_PROVIDER=local\nHENCHMEN_LLM_PROVIDER=anthropic\n")
        with (
            patch("henchmen.cli.serve.serve_app", return_value=attention_code),
            pytest.raises(SystemExit) as exit_info,
        ):
            _serve(_serve_args())
        assert exit_info.value.code == exit_code

    def test_run_mode_hints_at_console_link_when_no_token_is_available(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Deferred Task 3 nit: no usable sign-in token must never mean printing nothing."""
        from henchmen.cli import _serve

        _completed_setup(tmp_path, "HENCHMEN_PROVIDER=local\n" + _FORWARD_OK)
        with (
            patch.object(ConsoleAuth, "setup_token", new_callable=lambda: property(lambda self: "")),
            patch("henchmen.cli.serve.build_serve_app", return_value=MagicMock()),
            patch("henchmen.cli.serve.serve_app", return_value=0),
            pytest.raises(SystemExit),
        ):
            _serve(_serve_args())
        assert "henchmen console-link" in capsys.readouterr().out

    def test_attention_mode_hints_at_console_link_when_no_token_is_available(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from henchmen.cli import _serve

        _completed_setup(tmp_path, "HENCHMEN_PROVIDER=local\nHENCHMEN_LLM_PROVIDER=anthropic\n")
        with (
            patch.object(ConsoleAuth, "setup_token", new_callable=lambda: property(lambda self: "")),
            patch("henchmen.cli.serve.serve_app", return_value=0),
            pytest.raises(SystemExit),
        ):
            _serve(_serve_args())
        assert "henchmen console-link" in capsys.readouterr().out
