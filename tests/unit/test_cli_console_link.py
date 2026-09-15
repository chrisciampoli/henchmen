"""Tests for `henchmen console-link`."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from henchmen.cli.console_link import run_console_link_cli
from henchmen.console.auth import SETUP_TOKEN_FILE_NAME, SetupTokenStore


def _args(port: int | None = None) -> argparse.Namespace:
    return argparse.Namespace(port=port)


def test_requires_a_data_dir(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
    assert run_console_link_cli(_args()) == 2
    assert "HENCHMEN_DATA_DIR" in capsys.readouterr().err


def test_prints_a_fresh_link_that_invalidates_the_previous_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    (tmp_path / "henchmen.env").write_text(
        "HENCHMEN_PROVIDER=local\nHENCHMEN_LOCAL_SERVE_PORT=9123\n", encoding="utf-8"
    )

    assert run_console_link_cli(_args()) == 0
    first = capsys.readouterr().out.strip()
    assert run_console_link_cli(_args()) == 0
    second = capsys.readouterr().out.strip()

    prefix = "http://127.0.0.1:9123/console/session?setup_token="
    assert first.startswith(prefix) and second.startswith(prefix)
    store = SetupTokenStore(tmp_path / "secrets" / SETUP_TOKEN_FILE_NAME)
    assert store.consume(first.removeprefix(prefix)) is False
    assert store.consume(second.removeprefix(prefix)) is True


def test_port_flag_wins_and_unloadable_settings_fall_back_to_8000(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HENCHMEN_PROVIDER", "gcp")  # no project id: Settings cannot be built
    assert run_console_link_cli(_args(port=8001)) == 0
    assert capsys.readouterr().out.startswith("http://127.0.0.1:8001/")
    assert run_console_link_cli(_args()) == 0
    assert capsys.readouterr().out.startswith("http://127.0.0.1:8000/")


def test_configured_port_fallback_warns_on_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HENCHMEN_PROVIDER", "gcp")  # no project id: Settings cannot be built

    assert run_console_link_cli(_args()) == 0

    captured = capsys.readouterr()
    assert captured.out.startswith("http://127.0.0.1:8000/")
    assert "8000" in captured.err
    assert "--port" in captured.err


def test_unwritable_secrets_exit_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    with patch.object(SetupTokenStore, "rotate", side_effect=PermissionError("denied")):
        assert run_console_link_cli(_args(port=8000)) == 2
    assert "denied" in capsys.readouterr().err


def test_main_routes_the_subcommand() -> None:
    from henchmen.cli import main

    with (
        patch("sys.argv", ["henchmen", "console-link", "--port", "9000"]),
        patch("henchmen.cli.console_link.run_console_link_cli", return_value=0) as run,
        pytest.raises(SystemExit) as exit_info,
    ):
        main()
    assert exit_info.value.code == 0
    assert run.call_args.args[0].port == 9000
