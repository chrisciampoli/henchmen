"""Tests for the Console's single configuration writer (D-P10)."""

import os
import sys
import threading
from pathlib import Path

import pytest

from henchmen.console.config_store import (
    CONFIGURED,
    DISPATCH_API_TOKEN_KEY,
    ConfigStore,
    ConfigStoreError,
    settings_env_names,
)


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(config_file=tmp_path / "henchmen.env", secrets_dir=tmp_path / "secrets")


def test_update_then_get(store: ConfigStore) -> None:
    store.update({"HENCHMEN_LLM_PROVIDER": "anthropic", "HENCHMEN_ANTHROPIC_API_KEY": "sk-ant-1"}, section="LLM")
    assert store.get("HENCHMEN_LLM_PROVIDER") == "anthropic"
    assert store.is_set("HENCHMEN_ANTHROPIC_API_KEY")
    assert not store.is_set("HENCHMEN_OPENAI_API_KEY")
    if sys.platform != "win32":
        assert oct(os.stat(store.config_file).st_mode & 0o777) == "0o600"


def test_update_preserves_unrelated_lines(store: ConfigStore) -> None:
    store.config_file.write_text("# kept\nHENCHMEN_PROVIDER=local\n", encoding="utf-8")
    store.update({"HENCHMEN_GITHUB_DEFAULT_REPO": "acme/app"}, section="GitHub")
    text = store.config_file.read_text(encoding="utf-8")
    assert "# kept" in text
    assert "HENCHMEN_PROVIDER=local" in text
    assert "HENCHMEN_GITHUB_DEFAULT_REPO=acme/app" in text


@pytest.mark.parametrize("key", ["PATH", "HENCHMEN_DATA_DIR", "HENCHMEN_NOT_A_SETTING", "henchmen_provider"])
def test_unknown_keys_are_refused_and_nothing_is_written(store: ConfigStore, key: str) -> None:
    with pytest.raises(ConfigStoreError):
        store.update({"HENCHMEN_PROVIDER": "local", key: "x"}, section="Provider")
    assert not store.config_file.exists()


@pytest.mark.parametrize("value", ["a\nHENCHMEN_ENVIRONMENT=prod", "a\rb", "a\x00b"])
def test_values_with_line_breaks_are_refused(store: ConfigStore, value: str) -> None:
    with pytest.raises(ConfigStoreError, match="HENCHMEN_JIRA_EMAIL"):
        store.update({"HENCHMEN_JIRA_EMAIL": value}, section="Jira")
    assert not store.config_file.exists()


def test_unset_removes_keys(store: ConfigStore) -> None:
    store.update({"HENCHMEN_GITHUB_WEBHOOK_SECRET": "1", "HENCHMEN_GITHUB_DEFAULT_REPO": "acme/app"}, section="GitHub")
    store.unset(["HENCHMEN_GITHUB_WEBHOOK_SECRET"])
    assert store.get("HENCHMEN_GITHUB_WEBHOOK_SECRET") == ""
    assert store.get("HENCHMEN_GITHUB_DEFAULT_REPO") == "acme/app"


def test_unset_on_a_missing_file_does_not_create_it(store: ConfigStore) -> None:
    store.unset(["HENCHMEN_GITHUB_WEBHOOK_SECRET"])
    assert not store.config_file.exists()


def test_masked_hides_secret_values(store: ConfigStore) -> None:
    store.update({"HENCHMEN_SLACK_BOT_TOKEN": "xoxb-1", "HENCHMEN_JIRA_EMAIL": "a@b.co"}, section="Slack")
    assert store.masked(["HENCHMEN_SLACK_BOT_TOKEN", "HENCHMEN_SLACK_APP_TOKEN", "HENCHMEN_JIRA_EMAIL"]) == {
        "HENCHMEN_SLACK_BOT_TOKEN": CONFIGURED,
        "HENCHMEN_SLACK_APP_TOKEN": "",
        "HENCHMEN_JIRA_EMAIL": "a@b.co",
    }


def test_write_secret_file_lands_in_the_secrets_dir(store: ConfigStore) -> None:
    path = store.write_secret_file("github-app.pem", b"-----BEGIN PRIVATE KEY-----\n")
    assert path == store.secrets_dir / "github-app.pem"
    assert path.read_bytes() == b"-----BEGIN PRIVATE KEY-----\n"
    if sys.platform != "win32":
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("name", ["", ".", "..", "../escape.pem", "nested/key.pem", "nested\\key.pem"])
def test_write_secret_file_refuses_paths(store: ConfigStore, name: str) -> None:
    with pytest.raises(ConfigStoreError):
        store.write_secret_file(name, b"x")


def test_write_secret_file_creates_an_owner_only_secrets_dir(tmp_path: Path) -> None:
    """A fresh data directory has no ``secrets/`` yet (ruling I-3): it must be created, not fail."""
    store = ConfigStore(config_file=tmp_path / "henchmen.env", secrets_dir=tmp_path / "secrets")
    assert not store.secrets_dir.exists()
    store.write_secret_file("key.pem", b"x")
    assert store.secrets_dir.is_dir()
    if sys.platform != "win32":
        assert oct(os.stat(store.secrets_dir).st_mode & 0o777) == "0o700"


def test_concurrent_updates_do_not_lose_keys(store: ConfigStore) -> None:
    keys = [
        "HENCHMEN_GITHUB_WEBHOOK_SECRET",
        "HENCHMEN_GITHUB_DEFAULT_REPO",
        "HENCHMEN_GITHUB_DEFAULT_ORG",
        "HENCHMEN_JIRA_EMAIL",
        "HENCHMEN_JIRA_BASE_URL",
        "HENCHMEN_SLACK_NOTIFICATION_CHANNEL",
        "HENCHMEN_LLM_PROVIDER",
        "HENCHMEN_AWS_REGION",
    ]
    threads = [
        threading.Thread(target=store.update, args=({key: f"value-{index}"},), kwargs={"section": "S"})
        for index, key in enumerate(keys)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(store.get(key) == f"value-{index}" for index, key in enumerate(keys))


def test_two_instances_over_the_same_file_share_the_lock(tmp_path: Path) -> None:
    """Ruling C16: the lock is keyed by the resolved config path, not the instance.

    Apply builds its own ``ConfigStore`` per request and a step router uses a
    different instance again; a second instance updating unrelated keys, and
    the same second instance writing the Dispatch API token, must not lose the
    first instance's concurrent writes.
    """
    config_file = tmp_path / "henchmen.env"
    secrets_dir = tmp_path / "secrets"
    store_a = ConfigStore(config_file=config_file, secrets_dir=secrets_dir)
    store_b = ConfigStore(config_file=config_file, secrets_dir=secrets_dir)
    assert store_a._lock is store_b._lock  # noqa: SLF001 -- exactly what C16 requires

    def _update(store: ConfigStore, key: str, value: str) -> None:
        store.update({key: value}, section="S")

    threads = [
        threading.Thread(target=_update, args=(store_a, "HENCHMEN_GITHUB_DEFAULT_REPO", "acme/from-a")),
        threading.Thread(target=_update, args=(store_b, "HENCHMEN_GITHUB_DEFAULT_ORG", "acme")),
        threading.Thread(target=store_a.write_dispatch_api_token, args=("token-from-a",)),
        threading.Thread(target=store_b.write_dispatch_api_token, args=("token-from-a",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert store_a.get("HENCHMEN_GITHUB_DEFAULT_REPO") == "acme/from-a"
    assert store_b.get("HENCHMEN_GITHUB_DEFAULT_ORG") == "acme"
    assert store_a.get(DISPATCH_API_TOKEN_KEY) == "token-from-a"


def test_settings_env_names_cover_every_field() -> None:
    names = settings_env_names()
    assert "HENCHMEN_GITHUB_TOKEN" in names
    assert "HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD" in names
    assert "HENCHMEN_DATA_DIR" not in names


def test_console_app_exposes_its_stores(tmp_path: Path) -> None:
    from tests.unit.console_harness import build_console_app, make_harness

    harness = make_harness(tmp_path / "given")
    assert harness.app.state.config_store is harness.config_store
    assert harness.app.state.setup_store is harness.setup_store

    app, setup_store, _ = build_console_app(tmp_path / "default")
    assert app.state.setup_store is setup_store
    assert app.state.config_store.config_file == tmp_path / "default" / "henchmen.env"
    assert app.state.config_store.secrets_dir == tmp_path / "default" / "secrets"
