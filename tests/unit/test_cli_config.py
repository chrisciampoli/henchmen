"""Unit tests for ``henchmen config`` — effective settings with secrets masked."""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from henchmen.cli import main
from henchmen.cli.config_cmd import is_secret_field, render_settings, run_config_cli
from henchmen.config.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    """Run from an empty directory with no HENCHMEN_ vars leaking in."""
    for key in list(os.environ):
        if key.startswith("HENCHMEN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    yield


class TestSecretDetection:
    @pytest.mark.parametrize(
        "name",
        ["github_token", "openai_api_key", "anthropic_api_key", "slack_signing_secret", "metrics_auth_token"],
    )
    def test_credentials_are_secret(self, name: str) -> None:
        assert is_secret_field(name)

    @pytest.mark.parametrize("name", ["jira_project_key", "operative_max_output_tokens", "provider"])
    def test_plain_config_is_not_secret(self, name: str) -> None:
        assert not is_secret_field(name)

    def test_every_str_credential_field_is_masked(self) -> None:
        """Guard against a new credential field slipping past the name pattern."""
        # Named after a credential but holding none: the expiry timestamp of github_token.
        not_credentials = {"github_token_expires_at"}
        for name, field in Settings.model_fields.items():
            if name in not_credentials:
                continue
            if field.annotation is str and any(m in name for m in ("token", "secret", "api_key", "password")):
                assert is_secret_field(name), name
        assert not any(is_secret_field(name) for name in not_credentials)

    def test_agrees_with_config_store_masked_for_every_settings_field(self, tmp_path) -> None:
        """``henchmen config`` and the Console's ``ConfigStore.masked()`` share one classifier.

        Every field ConfigStore will accept is written with the same probe
        value, then whether ``masked()`` shows it as ``CONFIGURED`` (a secret)
        or echoes it back (not a secret) must match ``is_secret_field``.
        """
        from henchmen.console.config_store import CONFIGURED, ConfigStore, ConfigStoreError

        store = ConfigStore(config_file=tmp_path / "henchmen.env", secrets_dir=tmp_path / "secrets")
        checked = 0
        for name in Settings.model_fields:
            env_key = f"HENCHMEN_{name.upper()}"
            try:
                store.update({env_key: "probe-value"}, section="Probe")
            except ConfigStoreError:
                continue  # never-writable keys are out of scope for this parity check
            masked_as_secret = store.masked([env_key])[env_key] == CONFIGURED
            assert masked_as_secret == is_secret_field(name), name
            checked += 1
        assert checked > 0


class TestRenderSettings:
    def test_masks_secrets_and_shows_config(self) -> None:
        settings = Settings(provider="local", openai_api_key="sk-supersecretvalue1234", github_token="")
        lines = render_settings(settings)
        joined = "\n".join(lines)
        assert "sk-supersecretvalue1234" not in joined
        assert "HENCHMEN_OPENAI_API_KEY=****1234" in lines
        assert "HENCHMEN_GITHUB_TOKEN=(not set)" in lines
        assert "HENCHMEN_PROVIDER=local" in lines
        names = [line.split("=", 1)[0].lower() for line in lines]
        assert names == sorted(names)

    def test_only_set_hides_defaults(self) -> None:
        settings = Settings(provider="local", local_serve_port=9123)
        lines = render_settings(settings, only_set=True)
        assert "HENCHMEN_LOCAL_SERVE_PORT=9123" in lines
        assert not any(line.startswith("HENCHMEN_OPERATIVE_MAX_OUTPUT_TOKENS=") for line in lines)


class TestRunConfigCli:
    def test_reads_env_local(self, tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / ".env.local").write_text(
            "HENCHMEN_PROVIDER=local\nHENCHMEN_LLM_PROVIDER=anthropic\n", encoding="utf-8"
        )
        assert run_config_cli() == 0
        out = capsys.readouterr().out
        assert "HENCHMEN_LLM_PROVIDER=anthropic" in out

    def test_invalid_settings_exit_two_with_hint(self, tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / ".env.local").write_text("HENCHMEN_PROVIDER=gcp\n", encoding="utf-8")
        assert run_config_cli() == 2
        assert "henchmen init" in capsys.readouterr().err

    def test_subcommand_registered(self) -> None:
        with (
            patch("sys.argv", ["henchmen", "config", "--only-set"]),
            patch("henchmen.cli.config_cmd.run_config_cli", return_value=0) as run,
            pytest.raises(SystemExit) as exc,
        ):
            main()
        assert exc.value.code == 0
        assert run.call_args[0][0].only_set is True
