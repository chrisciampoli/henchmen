"""Unit tests for Settings token aliases, operative env passthrough and new tier fields."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from henchmen.config.settings import Settings

_ALIASED = [
    ("github_token", "HENCHMEN_GITHUB_TOKEN", "GITHUB_TOKEN"),
    ("slack_bot_token", "HENCHMEN_SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN"),
    ("slack_app_token", "HENCHMEN_SLACK_APP_TOKEN", "SLACK_APP_TOKEN"),
    ("slack_signing_secret", "HENCHMEN_SLACK_SIGNING_SECRET", "SLACK_SIGNING_SECRET"),
    ("jira_base_url", "HENCHMEN_JIRA_BASE_URL", "JIRA_SERVER"),
    ("jira_email", "HENCHMEN_JIRA_EMAIL", "JIRA_EMAIL"),
    ("jira_api_token", "HENCHMEN_JIRA_API_TOKEN", "JIRA_API_TOKEN"),
]

_RAW_NAMES = [row[2] for row in _ALIASED] + [
    "HENCHMEN_SLACK_BOT_TOKEN_SECRET",
    "HENCHMEN_SLACK_APP_TOKEN_SECRET",
    "HENCHMEN_JIRA_API_TOKEN_SECRET",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a bare environment.

    These tests assert on exact Settings values, so any ``HENCHMEN_*`` or
    aliased variable left behind by another test (or exported in the
    developer's shell) would make them order-dependent.
    """
    for name in [n for n in os.environ if n.startswith("HENCHMEN_")] + _RAW_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HENCHMEN_PROVIDER", "local")


def _settings(**overrides: object) -> Settings:
    """Build Settings from the environment only, ignoring the developer's .env.local."""
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestTokenAliases:
    @pytest.mark.parametrize(("field", "prefixed", "raw"), _ALIASED)
    def test_raw_name_is_accepted(self, monkeypatch: pytest.MonkeyPatch, field: str, prefixed: str, raw: str):
        monkeypatch.setenv(raw, "raw-value")
        assert getattr(_settings(), field) == "raw-value"

    @pytest.mark.parametrize(("field", "prefixed", "raw"), _ALIASED)
    def test_prefixed_name_is_accepted(self, monkeypatch: pytest.MonkeyPatch, field: str, prefixed: str, raw: str):
        monkeypatch.setenv(prefixed, "prefixed-value")
        assert getattr(_settings(), field) == "prefixed-value"

    @pytest.mark.parametrize(("field", "prefixed", "raw"), _ALIASED)
    def test_prefixed_wins_when_both_set(self, monkeypatch: pytest.MonkeyPatch, field: str, prefixed: str, raw: str):
        monkeypatch.setenv(raw, "raw-value")
        monkeypatch.setenv(prefixed, "prefixed-value")
        assert getattr(_settings(), field) == "prefixed-value"

    @pytest.mark.parametrize(
        ("field", "legacy"),
        [
            ("slack_bot_token", "HENCHMEN_SLACK_BOT_TOKEN_SECRET"),
            ("slack_app_token", "HENCHMEN_SLACK_APP_TOKEN_SECRET"),
            ("jira_api_token", "HENCHMEN_JIRA_API_TOKEN_SECRET"),
        ],
    )
    def test_legacy_secret_names_still_work(self, monkeypatch: pytest.MonkeyPatch, field: str, legacy: str):
        monkeypatch.setenv(legacy, "legacy-value")
        assert getattr(_settings(), field) == "legacy-value"

    def test_dotenv_file_honours_aliases(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        env_file = tmp_path / ".env.local"
        env_file.write_text(
            "HENCHMEN_SLACK_BOT_TOKEN=xoxb-from-file\nGITHUB_TOKEN=ghp-from-file\n",
            encoding="utf-8",
        )
        settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]
        assert settings.slack_bot_token == "xoxb-from-file"
        assert settings.github_token == "ghp-from-file"

    def test_keyword_construction_and_model_copy(self):
        settings = _settings(github_token="kw", slack_bot_token="xoxb-kw")
        assert settings.github_token == "kw" and settings.slack_bot_token == "xoxb-kw"
        copied = settings.model_copy(update={"github_token": "copied"})
        assert copied.github_token == "copied"

    def test_removed_secret_fields_are_gone(self):
        for name in ("slack_bot_token_secret", "slack_app_token_secret", "jira_api_token_secret"):
            assert name not in Settings.model_fields


class TestTierDefaults:
    def test_vertex_tiers(self):
        s = _settings()
        assert s.vertex_ai_model_complex == "gemini-2.5-pro"
        assert s.vertex_ai_model_light == "gemini-2.5-flash"
        assert s.vertex_ai_model_reasoning == "gemini-3.1-pro"

    def test_anthropic_defaults_are_current_ids(self):
        s = _settings()
        assert s.anthropic_model_complex == "claude-sonnet-5"
        assert s.anthropic_model_light == "claude-haiku-4-5"
        assert s.anthropic_model_reasoning == "claude-opus-5"
        for value in (s.anthropic_model_complex, s.anthropic_model_light, s.anthropic_model_reasoning):
            assert not value[-8:].isdigit(), "first-party Anthropic IDs must not carry a date suffix"

    def test_ollama_and_bedrock_tier_fields_exist(self):
        s = _settings()
        assert (
            s.llm_ollama_model_complex == "" and s.llm_ollama_model_light == "" and s.llm_ollama_model_reasoning == ""
        )
        assert s.bedrock_model_complex and s.bedrock_model_light and s.bedrock_model_reasoning

    def test_chat_model_and_force_push_defaults(self):
        s = _settings()
        assert s.llm_chat_model == ""
        assert s.allow_force_push is False
        assert s.metrics_auth_token == ""


class TestProviderValidation:
    def test_unknown_provider_rejected(self):
        with pytest.raises(ValueError, match="HENCHMEN_PROVIDER"):
            _settings(provider="azure")

    def test_unknown_service_override_rejected(self):
        with pytest.raises(ValueError, match="HENCHMEN_DOCUMENT_STORE_PROVIDER"):
            _settings(document_store_provider="mongodb")

    def test_unknown_llm_provider_rejected(self):
        with pytest.raises(ValueError, match="HENCHMEN_LLM_PROVIDER"):
            _settings(llm_provider="mistral")

    @pytest.mark.parametrize("alias", ["ollama", "vertex", "bedrock", "anthropic", "openai", "OLLAMA"])
    def test_documented_llm_aliases_accepted(self, alias: str):
        assert _settings(llm_provider=alias).llm_provider == alias

    def test_every_registry_alias_is_accepted_by_settings(self):
        """Settings and the registry must agree on which provider names exist."""
        from henchmen.providers.tiers import CANONICAL_LLM_PROVIDERS, LLM_PROVIDER_ALIASES

        for name in list(CANONICAL_LLM_PROVIDERS) + list(LLM_PROVIDER_ALIASES):
            _settings(llm_provider=name)

    def test_gcp_requires_project_id(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_PROVIDER", "gcp")
        with pytest.raises(ValueError, match="HENCHMEN_GCP_PROJECT_ID is required"):
            _settings()


class TestValidateForRuntime:
    def test_clean_local_ollama_config_has_no_problems(self):
        assert _settings(llm_provider="ollama").validate_for_runtime() == []

    def test_missing_anthropic_key_reported(self):
        problems = _settings(llm_provider="anthropic").validate_for_runtime()
        assert any("HENCHMEN_ANTHROPIC_API_KEY" in p for p in problems)

    def test_missing_openai_key_reported(self):
        problems = _settings(llm_provider="openai").validate_for_runtime()
        assert any("HENCHMEN_OPENAI_API_KEY" in p for p in problems)

    def test_key_present_is_clean(self):
        assert _settings(llm_provider="anthropic", anthropic_api_key="sk-ant-x").validate_for_runtime() == []

    def test_staging_requires_oidc_audience_and_metrics_token(self):
        problems = _settings(llm_provider="ollama", environment="staging").validate_for_runtime()
        assert any("HENCHMEN_PUBSUB_OIDC_AUDIENCE" in p for p in problems)
        assert any("HENCHMEN_METRICS_AUTH_TOKEN" in p for p in problems)

    def test_staging_clean_when_configured(self):
        problems = _settings(
            llm_provider="ollama",
            environment="staging",
            pubsub_oidc_audience="https://mastermind",
            metrics_auth_token="tok",
        ).validate_for_runtime()
        assert problems == []

    def test_non_positive_limits_reported(self):
        problems = _settings(
            llm_provider="ollama", operative_task_cost_ceiling_usd=0.0, operative_max_output_tokens=0
        ).validate_for_runtime()
        assert any("COST_CEILING" in p for p in problems)
        assert any("MAX_OUTPUT_TOKENS" in p for p in problems)

    def test_empty_tier_model_reported(self):
        problems = _settings(
            llm_provider="anthropic", anthropic_api_key="k", anthropic_model_light=""
        ).validate_for_runtime()
        assert any("default/light" in p for p in problems)


class TestOperativeEnv:
    def test_includes_limits_and_tiers_but_not_secrets_by_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", "ghp-secret")
        env = _settings(llm_provider="anthropic").operative_env()
        assert env["HENCHMEN_PROVIDER"] == "local"
        assert env["HENCHMEN_LLM_PROVIDER"] == "anthropic"
        assert env["HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD"] == "6.0"
        assert env["HENCHMEN_OPERATIVE_WALLCLOCK_CEILING_SECONDS"] == "1800"
        assert env["HENCHMEN_OPERATIVE_MAX_OUTPUT_TOKENS"] == "16384"
        assert env["HENCHMEN_ANTHROPIC_MODEL_COMPLEX"] == "claude-sonnet-5"
        assert env["HENCHMEN_ALLOW_FORCE_PUSH"] == "false"
        assert "HENCHMEN_ANTHROPIC_API_KEY" not in env
        assert "HENCHMEN_GITHUB_TOKEN" not in env

    def test_include_secrets_adds_keys_that_are_set(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_ANTHROPIC_API_KEY", "sk-ant-secret")
        monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", "ghp-secret")
        env = _settings(llm_provider="anthropic").operative_env(include_secrets=True)
        assert env["HENCHMEN_ANTHROPIC_API_KEY"] == "sk-ant-secret"
        assert env["HENCHMEN_GITHUB_TOKEN"] == "ghp-secret"
        assert "HENCHMEN_OPENAI_API_KEY" not in env  # empty values are omitted

    def test_empty_values_omitted(self):
        env = _settings().operative_env()
        assert "HENCHMEN_GCP_PROJECT_ID" not in env
        assert "HENCHMEN_LLM_OLLAMA_MODEL_COMPLEX" not in env

    def test_local_forward_base_defaults_to_serve_port(self):
        s = _settings()
        assert s.local_forward_base == "http://host.docker.internal:8000"
        assert _settings(local_serve_port=9000).local_forward_base == "http://host.docker.internal:9000"
        assert _settings(local_forward_base_url="http://x:1").local_forward_base == "http://x:1"

    def test_lair_service_account_email_default_and_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_PROVIDER", "gcp")
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "proj")
        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "staging")
        s = _settings()
        assert s.lair_service_account_email == "sa-staging-operative@proj.iam.gserviceaccount.com"
        assert _settings(lair_service_account="custom@x.iam").lair_service_account_email == "custom@x.iam"
