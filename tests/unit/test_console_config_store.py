"""The minimal ConfigStore Plan 2A needs: a generated Dispatch API token (D-P10, amendment A7)."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from henchmen.cli.envfile import EnvFile
from henchmen.config.settings import SEEDED_SECRET_PLACEHOLDER
from henchmen.console.config_store import DISPATCH_API_TOKEN_KEY, ConfigStore


def _store(tmp_path: Path, text: str) -> tuple[ConfigStore, Path]:
    config = tmp_path / "henchmen.env"
    config.write_text(text, encoding="utf-8")
    return ConfigStore(config, tmp_path / "secrets"), config


def test_generates_a_token_when_missing_and_keeps_the_rest(tmp_path: Path) -> None:
    store, config = _store(tmp_path, "# mine\nHENCHMEN_PROVIDER=local\n")
    assert store.ensure_dispatch_api_token() is True
    env = EnvFile.load(config)
    assert len(env.get(DISPATCH_API_TOKEN_KEY)) >= 40
    assert env.get("HENCHMEN_PROVIDER") == "local"
    assert "# mine" in config.read_text(encoding="utf-8")
    # ConfigStore keeps EnvFile's backup, but owner-only (ruling P2): a .bak of
    # the previous content is expected here, not absent.
    backup = config.with_name(config.name + ".bak")
    assert backup.is_file()
    if sys.platform != "win32":
        assert oct(os.stat(config).st_mode & 0o777) == "0o600"
        assert oct(os.stat(backup).st_mode & 0o777) == "0o600"


def test_keeps_an_existing_token(tmp_path: Path) -> None:
    store, config = _store(tmp_path, f"{DISPATCH_API_TOKEN_KEY}=my-own-token\n")
    assert store.ensure_dispatch_api_token() is False
    assert EnvFile.load(config).get(DISPATCH_API_TOKEN_KEY) == "my-own-token"


@pytest.mark.parametrize("value", ["", "   ", SEEDED_SECRET_PLACEHOLDER])
def test_replaces_a_blank_or_placeholder_token(tmp_path: Path, value: str) -> None:
    store, config = _store(tmp_path, f"{DISPATCH_API_TOKEN_KEY}={value}\n")
    assert store.ensure_dispatch_api_token() is True
    token = EnvFile.load(config).get(DISPATCH_API_TOKEN_KEY)
    assert token.strip() and token != SEEDED_SECRET_PLACEHOLDER


def test_pending_token_skips_generation_when_the_environment_already_provides_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The environment (e.g. a Secret Manager mount) outranks the file, so nothing is generated."""
    monkeypatch.setenv("HENCHMEN_DISPATCH_API_TOKEN", "already-configured-by-the-environment")
    store, config = _store(tmp_path, "HENCHMEN_PROVIDER=local\n")
    assert store.pending_dispatch_api_token(env_files=(str(config),)) is None


def test_pending_token_forwards_seeded_env_to_settings_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seeded_env must reach settings_problems unchanged, so apply's D-P8 masking applies here too."""
    import henchmen.console.config_store as config_store_module

    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def _fake_settings_problems(
        env_files: Sequence[str], *, seeded_env: dict[str, str] | None = None, overrides: object = None
    ) -> tuple[SimpleNamespace, list[str]]:
        calls.append((tuple(env_files), dict(seeded_env) if seeded_env else None))
        return SimpleNamespace(dispatch_api_token=""), []

    monkeypatch.setattr(config_store_module, "settings_problems", _fake_settings_problems)
    store, config = _store(tmp_path, "HENCHMEN_PROVIDER=local\n")
    seeded = {"HENCHMEN_PROVIDER": "local"}

    token = store.pending_dispatch_api_token(env_files=(str(config),), seeded_env=seeded)

    assert token is not None and len(token) >= 40
    assert calls == [((str(config),), seeded)]


def test_pending_token_is_not_written_until_write_dispatch_api_token_is_called(tmp_path: Path) -> None:
    """Ruling P6: computing the pending token must not touch the file."""
    store, config = _store(tmp_path, "HENCHMEN_PROVIDER=local\n")
    before = config.read_bytes()
    token = store.pending_dispatch_api_token()
    assert token is not None and len(token) >= 40
    assert config.read_bytes() == before
    assert not config.with_name(config.name + ".bak").exists()

    store.write_dispatch_api_token(token)
    assert EnvFile.load(config).get(DISPATCH_API_TOKEN_KEY) == token


def test_the_generated_token_authenticates_the_desktop_task_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from henchmen.config.settings import get_settings
    from henchmen.dispatch.api_models import dispatch_auth_headers
    from henchmen.dispatch.server import app

    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    store, _ = _store(tmp_path, "HENCHMEN_PROVIDER=local\nHENCHMEN_GITHUB_DEFAULT_REPO=acme/api\n")
    store.ensure_dispatch_api_token()
    get_settings.cache_clear()
    token = get_settings().dispatch_api_token
    with TestClient(app) as client:
        assert client.post("/api/v1/tasks", json={"title": "T"}).status_code == 401
        ok = client.post("/api/v1/tasks", json={"title": "T"}, headers=dispatch_auth_headers(token))
        assert ok.status_code == 200


def test_dispatch_auth_headers() -> None:
    from henchmen.dispatch.api_models import dispatch_auth_headers

    assert dispatch_auth_headers("") == {}
    assert dispatch_auth_headers("  ") == {}
    assert dispatch_auth_headers("abc") == {"Authorization": "Bearer abc"}
