"""Tests for the data-directory layout and dotenv resolution."""

from pathlib import Path

import pytest

from henchmen.config import paths
from henchmen.config.settings import get_settings


def test_without_data_dir_settings_read_the_repo_dotenv_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    assert paths.data_dir() is None
    assert paths.env_files() == (".env.local", ".env")
    assert paths.config_file() == Path(".env.local")
    assert paths.setup_state_file() is None
    assert paths.secrets_dir() is None


def test_with_data_dir_every_file_lives_inside_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    assert paths.data_dir() == tmp_path
    assert paths.env_files() == (str(tmp_path / "henchmen.env"),)
    assert paths.config_file() == tmp_path / "henchmen.env"
    assert paths.setup_state_file() == tmp_path / "setup-state.json"
    assert paths.secrets_dir() == tmp_path / "secrets"


def test_blank_data_dir_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, "   ")
    assert paths.data_dir() is None


def test_get_settings_reads_the_data_dir_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    (tmp_path / "henchmen.env").write_text("HENCHMEN_LOCAL_SERVE_PORT=8123\n", encoding="utf-8")
    get_settings.cache_clear()
    try:
        assert get_settings().local_serve_port == 8123
    finally:
        get_settings.cache_clear()
