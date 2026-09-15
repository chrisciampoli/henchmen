"""Unit tests for Settings configuration (env var binding, Pub/Sub prefix logic, defaults)."""

import pytest

from henchmen.config.settings import Environment, Settings, get_settings, require_secure_service_url

# ---------------------------------------------------------------------------
# Environment enum
# ---------------------------------------------------------------------------


class TestEnvironmentEnum:
    def test_dev_value(self):
        assert Environment.DEV.value == "dev"

    def test_staging_value(self):
        assert Environment.STAGING.value == "staging"

    def test_prod_value(self):
        assert Environment.PROD.value == "prod"

    def test_string_enum_pattern(self):
        assert isinstance(Environment.DEV, str)
        assert Environment.DEV == "dev"


# ---------------------------------------------------------------------------
# require_secure_service_url -- the one shared https/host validator (ruling C23/N1)
# ---------------------------------------------------------------------------


class TestRequireSecureServiceUrl:
    """``require_secure_github_url`` and ``checks.validate_jira_base_url`` are both thin
    wrappers over this one function, so GitHub and Jira share the identical host rule
    and cannot silently drift into two near-copies."""

    def test_https_is_accepted(self):
        assert require_secure_service_url("https://example.com") == "https://example.com"

    def test_loopback_http_is_accepted(self):
        require_secure_service_url("http://localhost:9000")

    def test_compose_service_host_over_http_is_accepted(self):
        assert require_secure_service_url("http://fakes:9000/jira") == "http://fakes:9000/jira"

    def test_dotted_host_over_http_is_refused(self):
        with pytest.raises(ValueError):
            require_secure_service_url("http://jira.example.com")

    def test_query_string_is_refused(self):
        with pytest.raises(ValueError):
            require_secure_service_url("https://example.com?x=1")

    def test_fragment_is_refused(self):
        with pytest.raises(ValueError):
            require_secure_service_url("https://example.com#frag")

    def test_url_parameters_are_refused(self):
        with pytest.raises(ValueError):
            require_secure_service_url("https://example.com/path;param=1")

    def test_field_name_appears_in_the_error_message(self):
        with pytest.raises(ValueError, match="Jira site URL"):
            require_secure_service_url("not-a-url", field="Jira site URL")

    def test_default_field_label(self):
        with pytest.raises(ValueError, match="URL"):
            require_secure_service_url("not-a-url")


# ---------------------------------------------------------------------------
# Settings creation with HENCHMEN_ prefix
# ---------------------------------------------------------------------------


class TestSettingsEnvPrefix:
    def test_creates_settings_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "my-project")
        monkeypatch.setenv("HENCHMEN_GCP_REGION", "europe-west1")
        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "staging")

        settings = Settings()
        assert settings.gcp_project_id == "my-project"
        assert settings.gcp_region == "europe-west1"
        assert settings.environment == Environment.STAGING

    def test_gcp_project_id_required_when_provider_is_gcp(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_PROVIDER", "gcp")
        monkeypatch.delenv("HENCHMEN_GCP_PROJECT_ID", raising=False)

        with pytest.raises(ValueError, match="HENCHMEN_GCP_PROJECT_ID is required"):
            Settings()

    def test_case_insensitive_env_prefix(self, monkeypatch: pytest.MonkeyPatch):
        """Settings should accept HENCHMEN_ prefix regardless of case (pydantic-settings behavior)."""
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")

        settings = Settings()
        assert settings.gcp_project_id == "test-project"


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


class TestSettingsDefaults:
    def test_gcp_region_defaults_to_us_central1(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")

        settings = Settings()
        assert settings.gcp_region == "us-central1"

    def test_environment_defaults_to_dev(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")

        settings = Settings()
        assert settings.environment == Environment.DEV

    def test_firestore_database_defaults_to_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")

        settings = Settings()
        assert settings.firestore_database == "(default)"

    def test_github_default_org(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_ORG", "")

        settings = Settings()
        assert settings.github_default_org == ""

    def test_github_default_repo(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "")

        settings = Settings()
        assert settings.github_default_repo == ""

    def test_pinecone_index_field_removed(self, monkeypatch: pytest.MonkeyPatch):
        """Deprecated pinecone_index_name field was removed from Settings."""
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")

        settings = Settings()
        assert not hasattr(settings, "pinecone_index_name")

    def test_lair_defaults(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")

        settings = Settings()
        assert settings.lair_default_cpu == "4"
        assert settings.lair_default_memory == "8Gi"
        assert settings.lair_default_timeout == 1800

    def test_vertex_ai_model_complex_has_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")

        settings = Settings()
        # The field has a non-empty default; the exact value is set in the Settings class
        assert settings.vertex_ai_model_complex != ""
        assert isinstance(settings.vertex_ai_model_complex, str)

    def test_vertex_ai_claude_region_field_removed(self, monkeypatch: pytest.MonkeyPatch):
        """Deprecated vertex_ai_claude_region field was removed from Settings."""
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")

        settings = Settings()
        assert not hasattr(settings, "vertex_ai_claude_region")


# ---------------------------------------------------------------------------
# Pub/Sub topic prefix logic (model_post_init)
# ---------------------------------------------------------------------------


class TestSettingsPubSubTopicPrefix:
    def test_dev_topics_get_dev_prefix(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")

        settings = Settings()
        assert settings.pubsub_topic_task_intake == "henchmen-dev-task-intake"
        assert settings.pubsub_topic_operative_complete == "henchmen-dev-operative-complete"
        assert settings.pubsub_topic_forge_request == "henchmen-dev-forge-request"
        assert settings.pubsub_topic_dead_letter == "henchmen-dev-dead-letter"

    def test_staging_topics_get_staging_prefix(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "staging")

        settings = Settings()
        assert settings.pubsub_topic_task_intake == "henchmen-staging-task-intake"
        assert settings.pubsub_topic_operative_complete == "henchmen-staging-operative-complete"

    def test_prod_topics_get_prod_prefix(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "prod")

        settings = Settings()
        assert settings.pubsub_topic_task_intake == "henchmen-prod-task-intake"
        assert settings.pubsub_topic_forge_result == "henchmen-prod-forge-result"

    def test_all_topics_receive_env_prefix(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")

        settings = Settings()
        topic_fields = [
            "pubsub_topic_task_intake",
            "pubsub_topic_operative_complete",
            "pubsub_topic_forge_request",
            "pubsub_topic_forge_result",
            "pubsub_topic_dead_letter",
            "pubsub_topic_embed_request",
            "pubsub_topic_ci_failure",
        ]
        for field_name in topic_fields:
            value = getattr(settings, field_name)
            assert value.startswith("henchmen-dev-"), f"{field_name} should start with 'henchmen-dev-', got '{value}'"

    def test_explicit_topic_overrides_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
        monkeypatch.setenv("HENCHMEN_PUBSUB_TOPIC_TASK_INTAKE", "custom-topic")

        settings = Settings()
        assert settings.pubsub_topic_task_intake == "custom-topic"


# ---------------------------------------------------------------------------
# get_settings singleton
# ---------------------------------------------------------------------------


class TestGetSettingsSingleton:
    def test_returns_settings_instance(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")

        result = get_settings()
        assert isinstance(result, Settings)

    def test_returns_same_instance_on_repeated_calls(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")

        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_cache_clear_allows_new_instance(self, monkeypatch: pytest.MonkeyPatch):
        """This test deliberately exercises ``cache_clear`` itself — keep the
        manual call even though ``_isolate_settings`` normally handles it."""
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")

        s1 = get_settings()
        get_settings.cache_clear()
        s2 = get_settings()
        # After cache_clear, a new instance is created
        assert s1 is not s2


# ---------------------------------------------------------------------------
# Extra fields are ignored (SettingsConfigDict extra="ignore")
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Local-mode portability (gcp_project_id optional when provider != gcp)
# ---------------------------------------------------------------------------


class TestSettingsLocalMode:
    def test_local_mode_no_gcp_project(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        monkeypatch.delenv("HENCHMEN_GCP_PROJECT_ID", raising=False)

        s = Settings()
        assert s.gcp_project_id == ""

    def test_gcp_mode_requires_gcp_project(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_PROVIDER", "gcp")
        monkeypatch.delenv("HENCHMEN_GCP_PROJECT_ID", raising=False)

        with pytest.raises(ValueError, match="HENCHMEN_GCP_PROJECT_ID is required"):
            Settings()


class TestValidateForRuntimeCatchesProviderGcpBypass:
    """``model_post_init`` already refuses provider=gcp with no project id at construction,
    but ``model_copy(update=...)`` -- used by test fixtures (e.g. ``_mock_settings`` in
    test_provider_registry.py) to derive per-test Settings variants -- bypasses it, so
    ``validate_for_runtime`` checks it again."""

    def test_provider_gcp_without_a_project_id_is_a_runtime_problem(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        base = Settings(_env_file=None, provider="local", gcp_project_id="test-project")  # type: ignore[call-arg]
        settings = base.model_copy(update={"provider": "gcp", "gcp_project_id": ""})
        assert any("HENCHMEN_GCP_PROJECT_ID" in p for p in settings.validate_for_runtime())

    def test_provider_gcp_with_a_project_id_has_no_provider_problem(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        base = Settings(_env_file=None, provider="local", gcp_project_id="test-project")  # type: ignore[call-arg]
        settings = base.model_copy(update={"provider": "gcp"})
        assert not any("HENCHMEN_GCP_PROJECT_ID" in p for p in settings.validate_for_runtime())

    def test_provider_and_llm_both_gcp_reports_the_missing_project_id_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nit fix: provider=gcp and llm_provider=gcp (the common case) must report the
        empty project id once, not twice."""
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        base = Settings(_env_file=None, provider="local", gcp_project_id="test-project")  # type: ignore[call-arg]
        settings = base.model_copy(update={"provider": "gcp", "llm_provider": "gcp", "gcp_project_id": ""})
        problems = [p for p in settings.validate_for_runtime() if "HENCHMEN_GCP_PROJECT_ID" in p]
        assert len(problems) == 1


# ---------------------------------------------------------------------------
# Extra fields are ignored (SettingsConfigDict extra="ignore")
# ---------------------------------------------------------------------------


class TestSettingsExtraIgnored:
    def test_extra_env_vars_do_not_raise(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_NONEXISTENT_FIELD", "some-value")

        # Should not raise ValidationError
        settings = Settings()
        assert settings.gcp_project_id == "test-project"


# ---------------------------------------------------------------------------
# Dispatch rate limiting and Jira field mapping
# ---------------------------------------------------------------------------


class TestDispatchRateLimitSettings:
    def test_defaults_match_the_previous_hardcoded_limiter(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.dispatch_rate_limit_requests == 60
        assert settings.dispatch_rate_limit_window_seconds == 60.0
        assert settings.dispatch_trust_forwarded_for is True

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        monkeypatch.setenv("HENCHMEN_DISPATCH_RATE_LIMIT_REQUESTS", "5")
        monkeypatch.setenv("HENCHMEN_DISPATCH_RATE_LIMIT_WINDOW_SECONDS", "0.5")
        monkeypatch.setenv("HENCHMEN_DISPATCH_TRUST_FORWARDED_FOR", "false")

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.dispatch_rate_limit_requests == 5
        assert settings.dispatch_rate_limit_window_seconds == 0.5
        assert settings.dispatch_trust_forwarded_for is False
        # A sub-second window is valid and must not be truncated to 0 by the check.
        assert not any("DISPATCH_RATE_LIMIT" in p for p in settings.validate_for_runtime())

    @pytest.mark.parametrize(
        ("field", "env_name"),
        [
            ("dispatch_rate_limit_requests", "HENCHMEN_DISPATCH_RATE_LIMIT_REQUESTS"),
            ("dispatch_rate_limit_window_seconds", "HENCHMEN_DISPATCH_RATE_LIMIT_WINDOW_SECONDS"),
        ],
    )
    def test_non_positive_values_are_reported(self, monkeypatch: pytest.MonkeyPatch, field: str, env_name: str):
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")

        settings = Settings(_env_file=None, **{field: 0})  # type: ignore[call-arg, arg-type]
        assert any(env_name in problem for problem in settings.validate_for_runtime())


class TestJiraFieldIdSettings:
    def test_default_to_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.jira_repo_field == ""
        assert settings.jira_branch_field == ""

    def test_env_sets_the_custom_field_ids(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        monkeypatch.setenv("HENCHMEN_JIRA_REPO_FIELD", "customfield_10042")
        monkeypatch.setenv("HENCHMEN_JIRA_BRANCH_FIELD", "customfield_10043")

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.jira_repo_field == "customfield_10042"
        assert settings.jira_branch_field == "customfield_10043"

    def test_descriptions_say_field_id_not_display_name(self):
        for name in ("jira_repo_field", "jira_branch_field"):
            description = Settings.model_fields[name].description or ""
            assert "field ID" in description
            assert "display name" in description


# ---------------------------------------------------------------------------
# Provider, CI, Forge, Dossier, eval and local-mode settings
# ---------------------------------------------------------------------------


def _local_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg, arg-type]


class TestDispatchApiTokenSetting:
    def test_defaults_to_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("DISPATCH_API_TOKEN", raising=False)
        assert _local_settings(monkeypatch).dispatch_api_token == ""

    @pytest.mark.parametrize("env_name", ["HENCHMEN_DISPATCH_API_TOKEN", "DISPATCH_API_TOKEN"])
    def test_accepts_prefixed_and_secret_mount_names(self, monkeypatch: pytest.MonkeyPatch, env_name: str):
        monkeypatch.delenv("DISPATCH_API_TOKEN", raising=False)
        monkeypatch.setenv(env_name, "tok")
        assert _local_settings(monkeypatch).dispatch_api_token == "tok"

    def test_is_masked_by_henchmen_config(self):
        from henchmen.cli.config_cmd import is_secret_field

        assert is_secret_field("dispatch_api_token")


class TestOptionalProviderSettings:
    @pytest.mark.parametrize(
        ("field", "default"),
        [
            ("ci_builder_image", "python:3.12"),
            ("ci_github_token_secret", ""),
            ("local_sqlite_path", ""),
            ("local_storage_dir", ""),
            ("dead_letter_subscription", ""),
            ("aws_ecs_execution_role_arn", ""),
            ("eval_db_path", ""),
            ("dossier_semantic_rerank", True),
            ("forge_ci_timeout_seconds", 540),
        ],
    )
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch, field: str, default: object):
        # tests/conftest.py points these at per-test files; the defaults are what is under test here.
        for name in ("HENCHMEN_LOCAL_SQLITE_PATH", "HENCHMEN_LOCAL_STORAGE_DIR", "HENCHMEN_EVAL_DB_PATH"):
            monkeypatch.delenv(name, raising=False)
        assert getattr(_local_settings(monkeypatch), field) == default

    @pytest.mark.parametrize(
        ("env_name", "field", "raw", "expected"),
        [
            ("HENCHMEN_CI_BUILDER_IMAGE", "ci_builder_image", "node:24", "node:24"),
            ("HENCHMEN_CI_GITHUB_TOKEN_SECRET", "ci_github_token_secret", "github-token", "github-token"),
            ("HENCHMEN_LOCAL_SQLITE_PATH", "local_sqlite_path", "/tmp/h.db", "/tmp/h.db"),
            ("HENCHMEN_LOCAL_STORAGE_DIR", "local_storage_dir", "/tmp/store", "/tmp/store"),
            ("HENCHMEN_DEAD_LETTER_SUBSCRIPTION", "dead_letter_subscription", "dl-sub", "dl-sub"),
            (
                "HENCHMEN_AWS_ECS_EXECUTION_ROLE_ARN",
                "aws_ecs_execution_role_arn",
                "arn:aws:iam::1:role/exec",
                "arn:aws:iam::1:role/exec",
            ),
            ("HENCHMEN_EVAL_DB_PATH", "eval_db_path", "/tmp/evals.db", "/tmp/evals.db"),
            ("HENCHMEN_DOSSIER_SEMANTIC_RERANK", "dossier_semantic_rerank", "false", False),
            ("HENCHMEN_FORGE_CI_TIMEOUT_SECONDS", "forge_ci_timeout_seconds", "300", 300),
        ],
    )
    def test_env_overrides(
        self, monkeypatch: pytest.MonkeyPatch, env_name: str, field: str, raw: str, expected: object
    ):
        monkeypatch.setenv(env_name, raw)
        assert getattr(_local_settings(monkeypatch), field) == expected

    @pytest.mark.parametrize("value", [29, 581, 600])
    def test_forge_ci_timeout_must_finish_before_the_pubsub_ack_deadline(
        self, monkeypatch: pytest.MonkeyPatch, value: int
    ):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="forge_ci_timeout_seconds"):
            _local_settings(monkeypatch, forge_ci_timeout_seconds=value)

    @pytest.mark.parametrize("value", [30, 580])
    def test_forge_ci_timeout_bounds_are_inclusive(self, monkeypatch: pytest.MonkeyPatch, value: int):
        assert _local_settings(monkeypatch, forge_ci_timeout_seconds=value).forge_ci_timeout_seconds == value


class TestBedrockDefaults:
    def test_tier_defaults_are_us_cross_region_inference_profiles(self, monkeypatch: pytest.MonkeyPatch):
        settings = _local_settings(monkeypatch)
        assert settings.bedrock_model_complex == "us.anthropic.claude-sonnet-4-20250514-v1:0"
        assert settings.bedrock_model_light == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert settings.bedrock_model_reasoning == "us.anthropic.claude-sonnet-4-20250514-v1:0"

    def test_inference_profile_defaults_are_priced(self, monkeypatch: pytest.MonkeyPatch):
        from henchmen.providers.pricing import lookup_price

        settings = _local_settings(monkeypatch)
        for model in (settings.bedrock_model_complex, settings.bedrock_model_light, settings.bedrock_model_reasoning):
            assert lookup_price(model) is not None, model


class TestRemovedSettings:
    @pytest.mark.parametrize(
        "field",
        ["vertex_ai_grounding_enabled", "vertex_ai_context_cache_enabled", "vertex_ai_context_cache_min_tokens"],
    )
    def test_field_is_gone_and_not_forwarded(self, monkeypatch: pytest.MonkeyPatch, field: str):
        settings = _local_settings(monkeypatch)
        assert field not in Settings.model_fields
        assert f"HENCHMEN_{field.upper()}" not in settings.operative_env()


# ---------------------------------------------------------------------------
# Every Settings field has a reader
# ---------------------------------------------------------------------------

# Fields forwarded to operatives whose consumer has not been wired yet. Each
# entry is a known gap, not a place to park new dead config: remove it as soon
# as a component reads the field (or the field is deleted).
#
# jira_intake_label: written by the Console's Jira step; the Phase 3 Jira poller reads it (A10).
_FIELDS_AWAITING_A_READER: frozenset[str] = frozenset({"jira_intake_label"})


def _unread_settings_fields() -> set[str]:
    """Settings fields that no module under src/henchmen (other than settings.py) mentions.

    Console step routers (``console/steps``) only write settings, so they do not
    count as readers.
    """
    from pathlib import Path

    import henchmen

    package_root = Path(henchmen.__file__).parent
    settings_file = package_root / "config" / "settings.py"
    steps_dir = package_root / "console" / "steps"
    corpus = "\n".join(
        path.read_text(encoding="utf-8")
        for path in package_root.rglob("*.py")
        if path != settings_file and steps_dir not in path.parents
    )
    return {name for name in Settings.model_fields if name not in corpus and f"HENCHMEN_{name.upper()}" not in corpus}


class TestEverySettingIsRead:
    def test_every_field_is_referenced_outside_settings(self):
        """Dead Settings fields let docs tell operators to set knobs that do nothing."""
        unread = sorted(_unread_settings_fields() - _FIELDS_AWAITING_A_READER)
        assert unread == [], f"Settings fields with no reader in src/henchmen: {unread}"

    def test_allowlist_has_no_stale_entries(self):
        """An allowlisted field that gained a reader (or was deleted) must leave the allowlist."""
        assert set(Settings.model_fields) >= _FIELDS_AWAITING_A_READER
        assert _unread_settings_fields() >= _FIELDS_AWAITING_A_READER

    def test_step_routers_only_write_settings(self):
        """Guard for the ``console/steps`` corpus exclusion above (ruling M-9).

        Excluding every step router from the settings-reader corpus would
        silently hide a step that genuinely reads a setting (through
        ``get_settings()`` or by building ``Settings(...)`` directly) instead
        of going through ``ConfigStore``. Only ``ai_provider.py``'s cost
        estimate does that today, to build a throwaway ``Settings`` for the
        chosen provider/models -- never the process-wide cached settings.
        """
        from pathlib import Path

        import henchmen

        package_root = Path(henchmen.__file__).parent
        steps_dir = package_root / "console" / "steps"
        allowed = {steps_dir / "ai_provider.py"}
        offenders = sorted(
            str(path.relative_to(package_root))
            for path in steps_dir.glob("*.py")
            if path not in allowed
            for pattern in ("get_settings(", "Settings(")
            if pattern in path.read_text(encoding="utf-8")
        )
        assert offenders == [], f"Step routers must read config through ConfigStore, not Settings: {offenders}"


class TestSeededSecretPlaceholder:
    """Terraform's published placeholder must never count as a configured secret."""

    def test_every_seeded_terraform_secret_maps_to_a_guarded_field(self) -> None:
        import re
        from pathlib import Path

        from henchmen.config.settings import _SEEDED_SECRET_FIELDS

        tf = (Path(__file__).resolve().parents[2] / "terraform/modules/secrets/main.tf").read_text(encoding="utf-8")
        seeded = {
            name.replace("-", "_")
            for name in re.findall(r'secret_id\s*=\s*"henchmen-\$\{var\.environment\}-([a-z-]+)"', tf)
        }
        assert seeded, "no seeded secrets found in the secrets module"
        assert seeded <= set(_SEEDED_SECRET_FIELDS), f"unguarded seeded secrets: {seeded - set(_SEEDED_SECRET_FIELDS)}"

    @pytest.mark.parametrize(
        "env_name",
        [
            "HENCHMEN_METRICS_AUTH_TOKEN",
            "HENCHMEN_DISPATCH_API_TOKEN",
            "HENCHMEN_SLACK_SIGNING_SECRET",
            "HENCHMEN_GITHUB_WEBHOOK_SECRET",
            "HENCHMEN_JIRA_WEBHOOK_SECRET",
            "GITHUB_TOKEN",
        ],
    )
    def test_placeholder_is_read_as_empty(self, monkeypatch: pytest.MonkeyPatch, env_name: str) -> None:
        from henchmen.config.settings import SEEDED_SECRET_PLACEHOLDER, Settings

        monkeypatch.setenv(env_name, SEEDED_SECRET_PLACEHOLDER)
        field = env_name.removeprefix("HENCHMEN_").lower()
        assert getattr(Settings(_env_file=None), field) == ""

    def test_real_values_are_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from henchmen.config.settings import Settings

        monkeypatch.setenv("HENCHMEN_METRICS_AUTH_TOKEN", "a-real-token")
        assert Settings(_env_file=None).metrics_auth_token == "a-real-token"

    @pytest.mark.asyncio
    async def test_metrics_endpoint_rejects_the_placeholder_in_prod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi import HTTPException

        from henchmen.config.settings import SEEDED_SECRET_PLACEHOLDER, Settings
        from henchmen.observability.api import build_metrics_auth_dependency

        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "prod")
        monkeypatch.setenv("HENCHMEN_METRICS_AUTH_TOKEN", SEEDED_SECRET_PLACEHOLDER)
        dependency = build_metrics_auth_dependency(Settings(_env_file=None))
        with pytest.raises(HTTPException) as excinfo:
            await dependency(authorization=f"Bearer {SEEDED_SECRET_PLACEHOLDER}")
        assert excinfo.value.status_code == 401
