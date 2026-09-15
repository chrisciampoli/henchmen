"""Unit tests for the ``henchmen init`` setup wizard.

The wizard is driven by a ``ScriptedPrompter``; every live check in
``henchmen.cli.checks`` is replaced by a fake so no network is touched.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from henchmen.cli import checks, init
from henchmen.cli.checks import CheckResult, CheckStatus, SlackChannel, SlackScopeError, SlackUnreachableError
from henchmen.cli.envfile import EnvFile
from henchmen.cli.init import (
    SECTIONS,
    InitOptions,
    add_init_arguments,
    options_from_args,
    run_init,
    run_init_cli,
)
from henchmen.cli.prompts import ScriptedPrompter

ANTHROPIC_MODELS = ["claude-haiku-4-5", "claude-opus-5", "claude-sonnet-5"]


class TestTierFallbacks:
    def test_fallbacks_match_the_settings_defaults(self) -> None:
        """The import-failure fallbacks must not drift from Settings (bare Bedrock ids fail on-demand invocation)."""
        from henchmen.config.settings import Settings

        for provider, tiers in init._TIER_FIELDS.items():
            for tier, (field_name, fallback) in tiers.items():
                default = Settings.model_fields[field_name].default
                if default:
                    assert fallback == default, (provider, tier)

    def test_bedrock_fallbacks_are_inference_profiles(self) -> None:
        for _field_name, fallback in init._TIER_FIELDS["aws"].values():
            assert fallback.startswith("us."), fallback


def _ok(name: str, message: str = "ok") -> CheckResult:
    return CheckResult(name, CheckStatus.OK, message)


def _fail(name: str, message: str = "rejected") -> CheckResult:
    return CheckResult(name, CheckStatus.FAIL, message, hint="fix it")


@pytest.fixture
def calls() -> dict[str, list[Any]]:
    return {}


@pytest.fixture(autouse=True)
def fake_checks(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]) -> None:
    """Replace every live check with a recording fake that succeeds."""

    def record(name: str, result: Any) -> Any:
        def fake(*args: Any, **kwargs: Any) -> Any:
            calls.setdefault(name, []).append(args)
            return result

        return fake

    monkeypatch.setattr(checks, "check_anthropic_key", record("anthropic", _ok("Anthropic API key", "valid")))
    monkeypatch.setattr(checks, "list_anthropic_models", record("anthropic_models", list(ANTHROPIC_MODELS)))
    monkeypatch.setattr(checks, "check_openai_key", record("openai", _ok("OpenAI API key")))
    monkeypatch.setattr(
        checks, "list_openai_models", record("openai_models", ["gpt-4.1", "gpt-4.1-mini", "o3", "tts-1"])
    )
    monkeypatch.setattr(checks, "check_ollama", record("ollama", _ok("Ollama")))
    monkeypatch.setattr(checks, "list_ollama_models", record("ollama_models", ["qwen2.5-coder:7b", "llama3.2:latest"]))
    monkeypatch.setattr(checks, "check_vertex", record("vertex", _ok("Vertex AI credentials")))
    monkeypatch.setattr(checks, "check_github_token", record("github", _ok("GitHub token", "authenticated as octocat")))
    monkeypatch.setattr(checks, "check_github_repo", record("github_repo", _ok("GitHub repository")))
    monkeypatch.setattr(
        checks, "check_slack_bot_token", record("slack_bot", _ok("Slack bot token", "@henchmen in Acme"))
    )
    monkeypatch.setattr(checks, "check_slack_app_token", record("slack_app", _ok("Slack app token")))
    monkeypatch.setattr(
        checks,
        "list_slack_channels",
        record(
            "slack_channels",
            [
                SlackChannel("C1", "engineering", False, False),
                SlackChannel("C2", "henchmen", False, False),
                SlackChannel("C3", "secret", True, True),
            ],
        ),
    )
    monkeypatch.setattr(checks, "join_slack_channel", record("slack_join", _ok("Slack channel", "joined #henchmen")))
    monkeypatch.setattr(checks, "check_jira", record("jira", _ok("Jira", "authenticated as Jane")))
    monkeypatch.setattr(init, "_gcloud_project", lambda: "")
    monkeypatch.setattr(init, "_git_config", lambda key: "")


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    return tmp_path / ".env.local"


def _run(answers: list[Any], env_path: Path, **kwargs: Any) -> tuple[int, ScriptedPrompter, EnvFile]:
    prompter = ScriptedPrompter(answers)
    options = InitOptions(env_file=env_path, **kwargs)
    code = run_init(prompter, options)
    return code, prompter, EnvFile.load(env_path)


# ---------------------------------------------------------------------------
# Full runs
# ---------------------------------------------------------------------------


class TestFullRun:
    def test_local_anthropic_end_to_end(self, env_path: Path, calls: dict[str, list[Any]]):
        answers = [
            "1",  # mode: local
            "",  # environment: dev
            "1",  # llm: anthropic
            "sk-ant-new-key-0000",  # api key
            "",  # complex tier -> recommended default
            "",  # light tier
            "",  # reasoning tier
            "",  # chat model -> light
            "ghp_newtoken00000000",  # github token
            "acme/app",  # repo
            "",  # git author name
            "",  # git author email
            "n",  # slack
            "n",  # jira
            "n",  # limits
            "y",  # write
        ]
        code, prompter, env = _run(answers, env_path)

        assert code == 0
        assert env.get("HENCHMEN_PROVIDER") == "local"
        assert env.get("HENCHMEN_ENVIRONMENT") == "dev"
        assert env.get("HENCHMEN_LLM_PROVIDER") == "anthropic"
        assert env.get("HENCHMEN_ANTHROPIC_API_KEY") == "sk-ant-new-key-0000"
        assert env.get("HENCHMEN_ANTHROPIC_MODEL_COMPLEX") == "claude-sonnet-5"
        assert env.get("HENCHMEN_ANTHROPIC_MODEL_LIGHT") == "claude-haiku-4-5"
        assert env.get("HENCHMEN_ANTHROPIC_MODEL_REASONING") == "claude-opus-5"
        assert env.get("HENCHMEN_LLM_CHAT_MODEL") == "claude-haiku-4-5"
        assert env.get("HENCHMEN_GITHUB_TOKEN") == "ghp_newtoken00000000"
        assert env.get("HENCHMEN_GITHUB_DEFAULT_REPO") == "acme/app"
        assert env.get("HENCHMEN_GITHUB_DEFAULT_ORG") == "acme"
        assert env.get("HENCHMEN_GIT_AUTHOR_NAME") == "Henchmen Operative"
        assert "HENCHMEN_SLACK_BOT_TOKEN" not in env.as_dict()
        # Live checks were called with the collected values.
        assert calls["anthropic"] == [("sk-ant-new-key-0000",)]
        assert calls["github"] == [("ghp_newtoken00000000",)]
        assert calls["github_repo"] == [("ghp_newtoken00000000", "acme/app")]
        # Secrets never appear in output.
        joined = "\n".join(prompter.output)
        assert "sk-ant-new-key-0000" not in joined
        assert "ghp_newtoken00000000" not in joined
        assert "****0000" in joined

    def test_ollama_run_uses_live_model_list(self, env_path: Path):
        answers = [
            "1",
            "",  # mode
            "3",  # llm: ollama
            "",  # base url default
            "1",  # complex: qwen2.5-coder:7b (first live model)
            "2",  # light: llama3.2:latest
            "1",  # reasoning
            "",  # chat model default
            "ghp_x",
            "acme/app",
            "",
            "",
            "n",
            "n",
            "n",
            "y",
        ]
        code, _, env = _run(answers, env_path)
        assert code == 0
        assert env.get("HENCHMEN_LLM_PROVIDER") == "local"
        assert env.get("HENCHMEN_LLM_OLLAMA_BASE_URL") == "http://localhost:11434"
        assert (
            env.get("HENCHMEN_LLM_OLLAMA_MODEL_COMPLEX") == "llama3.2:latest"
            or env.get("HENCHMEN_LLM_OLLAMA_MODEL_COMPLEX") == "qwen2.5-coder:7b"
        )
        assert env.get("HENCHMEN_LLM_OLLAMA_MODEL") == env.get("HENCHMEN_LLM_OLLAMA_MODEL_COMPLEX")
        assert env.get("HENCHMEN_LLM_OLLAMA_MODEL_LIGHT") in ("llama3.2:latest", "qwen2.5-coder:7b")

    def test_openai_filters_non_chat_models(self, env_path: Path):
        answers = ["1", "", "2", "sk-openai-000000000", "", "", "", "", "ghp_x", "acme/app", "", "", "n", "n", "n", "y"]
        code, prompter, env = _run(answers, env_path)
        assert code == 0
        assert env.get("HENCHMEN_OPENAI_MODEL_COMPLEX") == "gpt-4.1"
        assert env.get("HENCHMEN_OPENAI_MODEL_REASONING") == "o3"
        assert "tts-1" not in "\n".join(prompter.transcript)

    def test_vertex_run(self, env_path: Path, calls: dict[str, list[Any]]):
        answers = [
            "2",
            "",  # mode gcp, env dev
            "my-proj",
            "",  # project, region
            "4",  # llm: vertex
            "",
            "",
            "",  # tiers
            "",  # chat
            "ghp_x",
            "acme/app",
            "",
            "",
            "n",
            "n",
            "n",
            "y",
        ]
        code, prompter, env = _run(answers, env_path)
        assert code == 0
        assert env.get("HENCHMEN_PROVIDER") == "gcp"
        assert env.get("HENCHMEN_GCP_PROJECT_ID") == "my-proj"
        assert env.get("HENCHMEN_LLM_PROVIDER") == "gcp"
        assert env.get("HENCHMEN_VERTEX_AI_MODEL_COMPLEX") == "gemini-2.5-pro"
        assert env.get("HENCHMEN_VERTEX_AI_MODEL_LIGHT") == "gemini-2.5-flash"
        assert env.get("HENCHMEN_VERTEX_AI_MODEL_REASONING") == "gemini-3.1-pro"
        assert calls["vertex"] == [("my-proj", "us-central1")]
        joined = "\n".join(prompter.output)
        assert "gcloud secrets versions add henchmen-dev-github-token" in joined
        assert "ghp_x" not in joined


# ---------------------------------------------------------------------------
# Existing configuration
# ---------------------------------------------------------------------------


class TestExistingConfig:
    def test_existing_values_are_defaults_and_backup_made(self, env_path: Path, calls: dict[str, list[Any]]):
        env_path.write_text(
            "# mine\nHENCHMEN_PROVIDER=local\nHENCHMEN_LLM_PROVIDER=anthropic\n"
            "HENCHMEN_ANTHROPIC_API_KEY=sk-ant-old-key-9999\nCUSTOM=keep\n",
            encoding="utf-8",
        )
        answers = ["", "", "", "", "", "", "", "", "ghp_x", "acme/app", "", "", "n", "n", "n", "y"]
        code, prompter, env = _run(answers, env_path)
        assert code == 0
        assert env.get("HENCHMEN_ANTHROPIC_API_KEY") == "sk-ant-old-key-9999"
        assert env.get("CUSTOM") == "keep"
        assert calls["anthropic"] == [("sk-ant-old-key-9999",)]
        assert env_path.with_name(".env.local.bak").read_text(encoding="utf-8").startswith("# mine")
        assert "# mine" in env_path.read_text(encoding="utf-8")
        assert any("****9999" in line for line in prompter.transcript + prompter.output)

    def test_yes_accepts_everything_without_prompting(self, env_path: Path, calls: dict[str, list[Any]]):
        env_path.write_text(
            "HENCHMEN_PROVIDER=local\nHENCHMEN_ENVIRONMENT=dev\nHENCHMEN_LLM_PROVIDER=anthropic\n"
            "HENCHMEN_ANTHROPIC_API_KEY=sk-ant-old-key-9999\nHENCHMEN_GITHUB_TOKEN=ghp_old\n"
            "HENCHMEN_GITHUB_DEFAULT_REPO=acme/app\nHENCHMEN_SLACK_BOT_TOKEN=xoxb-old\n"
            "HENCHMEN_SLACK_APP_TOKEN=xapp-old\nHENCHMEN_SLACK_NOTIFICATION_CHANNEL=C2\n",
            encoding="utf-8",
        )
        code, prompter, env = _run([], env_path, yes=True)
        assert code == 0
        assert prompter.transcript == []
        assert env.get("HENCHMEN_ANTHROPIC_MODEL_COMPLEX") == "claude-sonnet-5"
        assert env.get("HENCHMEN_LLM_CHAT_MODEL") == "claude-haiku-4-5"
        assert env.get("HENCHMEN_SLACK_NOTIFICATION_CHANNEL") == "C2"
        assert calls["anthropic"] == [("sk-ant-old-key-9999",)]
        assert calls["slack_bot"] == [("xoxb-old",)]
        assert calls["slack_join"] == [("xoxb-old", "C2")]


# ---------------------------------------------------------------------------
# Slack section
# ---------------------------------------------------------------------------


class TestSlack:
    def _env_with_basics(self, env_path: Path) -> None:
        env_path.write_text(
            "HENCHMEN_PROVIDER=local\nHENCHMEN_LLM_PROVIDER=anthropic\nHENCHMEN_ANTHROPIC_API_KEY=sk-ant-x\n",
            encoding="utf-8",
        )

    def test_lists_channels_and_joins_selected(self, env_path: Path, calls: dict[str, list[Any]]):
        self._env_with_basics(env_path)
        answers = ["y", "xoxb-new-token-1234", "xapp-new-token-5678", "", "2", "y"]
        code, prompter, env = _run(answers, env_path, sections=("slack",))
        assert code == 0
        assert env.get("HENCHMEN_SLACK_BOT_TOKEN") == "xoxb-new-token-1234"
        assert env.get("HENCHMEN_SLACK_APP_TOKEN") == "xapp-new-token-5678"
        assert env.get("HENCHMEN_SLACK_NOTIFICATION_CHANNEL") == "C2"
        assert calls["slack_join"] == [("xoxb-new-token-1234", "C2")]
        menu = "\n".join(prompter.transcript)
        assert "Channel for Henchmen to join" in menu

    def test_already_member_channel_skips_join(self, env_path: Path, calls: dict[str, list[Any]]):
        self._env_with_basics(env_path)
        answers = ["y", "xoxb-new-token-1234", "xapp-new-token-5678", "", "3", "y"]
        code, prompter, env = _run(answers, env_path, sections=("slack",))
        assert code == 0
        assert env.get("HENCHMEN_SLACK_NOTIFICATION_CHANNEL") == "C3"
        assert "slack_join" not in calls
        assert any("already a member" in line for line in prompter.output)

    def test_skip_choice_leaves_channel_unset(self, env_path: Path, calls: dict[str, list[Any]]):
        self._env_with_basics(env_path)
        answers = ["y", "xoxb-new-token-1234", "xapp-new-token-5678", "", "__skip__", "y"]
        code, _, env = _run(answers, env_path, sections=("slack",))
        assert code == 0
        assert "HENCHMEN_SLACK_NOTIFICATION_CHANNEL" not in env.as_dict()
        assert "slack_join" not in calls

    def test_missing_scope_falls_back_to_manual_channel_id(
        self, env_path: Path, monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
    ):
        self._env_with_basics(env_path)

        def boom(token: str, **kw: Any) -> list[SlackChannel]:
            raise SlackScopeError("channels:read")

        monkeypatch.setattr(checks, "list_slack_channels", boom)
        answers = ["y", "xoxb-new-token-1234", "xapp-new-token-5678", "", "C999", "y"]
        code, prompter, env = _run(answers, env_path, sections=("slack",))
        assert code == 0
        assert env.get("HENCHMEN_SLACK_NOTIFICATION_CHANNEL") == "C999"
        assert calls["slack_join"] == [("xoxb-new-token-1234", "C999")]
        assert any("channels:read" in line for line in prompter.output)

    def test_unreachable_slack_falls_back_to_manual_channel_id(
        self, env_path: Path, monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
    ):
        self._env_with_basics(env_path)

        def boom(token: str, **kw: Any) -> list[SlackChannel]:
            raise SlackUnreachableError("connection refused")

        monkeypatch.setattr(checks, "list_slack_channels", boom)
        answers = ["y", "xoxb-new-token-1234", "xapp-new-token-5678", "", "C999", "y"]
        code, prompter, env = _run(answers, env_path, sections=("slack",))
        assert code == 0
        assert env.get("HENCHMEN_SLACK_NOTIFICATION_CHANNEL") == "C999"
        assert calls["slack_join"] == [("xoxb-new-token-1234", "C999")]
        assert any("Slack could not be reached" in line for line in prompter.output)

    def test_declining_slack_writes_nothing_for_slack(self, env_path: Path, calls: dict[str, list[Any]]):
        self._env_with_basics(env_path)
        code, _, env = _run(["n", "y"], env_path, sections=("slack",))
        assert code == 0
        assert "HENCHMEN_SLACK_BOT_TOKEN" not in env.as_dict()
        assert "slack_bot" not in calls

    def test_invalid_bot_token_three_times_then_skip(self, env_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._env_with_basics(env_path)
        monkeypatch.setattr(
            checks, "check_slack_bot_token", lambda value, **kw: _fail("Slack bot token", "invalid_auth")
        )
        answers = ["y", "xoxb-bad-1", "xoxb-bad-2", "xoxb-bad-3", "n", "y"]
        code, prompter, env = _run(answers, env_path, sections=("slack",))
        assert code == 0
        assert "HENCHMEN_SLACK_BOT_TOKEN" not in env.as_dict()
        assert sum("invalid_auth" in line for line in prompter.output) == 3


# ---------------------------------------------------------------------------
# Validation and retry
# ---------------------------------------------------------------------------


class TestRetryAndValidation:
    def test_failed_key_reprompts_then_keep_anyway(self, env_path: Path, monkeypatch: pytest.MonkeyPatch):
        env_path.write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")
        seen: list[str] = []

        def flaky(value: str, **kw: Any) -> CheckResult:
            seen.append(value)
            return _fail("Anthropic API key", "rejected: 401")

        monkeypatch.setattr(checks, "check_anthropic_key", flaky)
        monkeypatch.setattr(checks, "list_anthropic_models", lambda value, **kw: [])
        answers = [
            "1",  # anthropic
            "bad1",
            "bad2",
            "bad3",  # three failures
            "y",  # keep anyway
            "",
            "",
            "",  # tiers (free text, defaults)
            "",  # chat
            "y",  # write
        ]
        code, prompter, env = _run(answers, env_path, sections=("llm",))
        assert code == 0
        assert seen == ["bad1", "bad2", "bad3"]
        assert env.get("HENCHMEN_ANTHROPIC_API_KEY") == "bad3"
        assert env.get("HENCHMEN_ANTHROPIC_MODEL_COMPLEX") == "claude-sonnet-5"
        assert any("Attempt 1 of 3" in line for line in prompter.output)

    def test_repo_slug_validation_reprompts(self, env_path: Path):
        env_path.write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")
        answers = ["ghp_x", "not-a-slug", "acme/app", "", "", "y"]
        code, prompter, env = _run(answers, env_path, sections=("github",))
        assert code == 0
        assert env.get("HENCHMEN_GITHUB_DEFAULT_REPO") == "acme/app"
        assert any("owner/repo" in line for line in prompter.output)

    def test_environment_validation(self, env_path: Path):
        answers = ["1", "production", "prod", "y"]
        code, prompter, env = _run(answers, env_path, sections=("mode",))
        assert code == 0
        assert env.get("HENCHMEN_ENVIRONMENT") == "prod"
        assert any("dev, staging or prod" in line for line in prompter.output)

    def test_limits_section_validates_numbers(self, env_path: Path):
        answers = ["y", "abc", "4.5", "900", "8192", "y"]
        code, _, env = _run(answers, env_path, sections=("limits",))
        assert code == 0
        assert env.get("HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD") == "4.5"
        assert env.get("HENCHMEN_OPERATIVE_WALLCLOCK_CEILING_SECONDS") == "900"
        assert env.get("HENCHMEN_OPERATIVE_MAX_OUTPUT_TOKENS") == "8192"

    def test_jira_section(self, env_path: Path, calls: dict[str, list[Any]]):
        answers = ["y", "https://acme.atlassian.net", "me@acme.com", "jira-token-000000", "proj", "y"]
        code, _, env = _run(answers, env_path, sections=("jira",))
        assert code == 0
        assert env.get("HENCHMEN_JIRA_BASE_URL") == "https://acme.atlassian.net"
        assert env.get("HENCHMEN_JIRA_API_TOKEN") == "jira-token-000000"
        assert env.get("HENCHMEN_JIRA_PROJECT_KEY") == "PROJ"
        assert calls["jira"] == [("https://acme.atlassian.net", "me@acme.com", "jira-token-000000")]


# ---------------------------------------------------------------------------
# Modes: dry-run, abort, decline
# ---------------------------------------------------------------------------


class TestModes:
    def test_dry_run_writes_nothing_but_shows_file(self, env_path: Path):
        answers = ["1", "", "n"]
        code, prompter, _ = _run(answers, env_path, sections=("mode",), dry_run=True)
        assert code == 0
        assert not env_path.exists()
        assert any("HENCHMEN_PROVIDER=local" in line for line in prompter.output)
        assert any("--dry-run" in line for line in prompter.output)

    def test_abort_at_prompt_writes_nothing(self, env_path: Path):
        code, prompter, _ = _run([ScriptedPrompter.ABORT], env_path)
        assert code == init.EXIT_ABORTED
        assert not env_path.exists()
        assert any("Aborted" in line for line in prompter.output)

    def test_declining_write_returns_1(self, env_path: Path):
        code, prompter, _ = _run(["1", "", "n"], env_path, sections=("mode",))
        assert code == 1
        assert not env_path.exists()
        assert any("Nothing written" in line for line in prompter.output)

    def test_summary_masks_secrets(self, env_path: Path):
        env_path.write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")
        answers = ["ghp_supersecret_12345678", "acme/app", "", "", "y"]
        _, prompter, _ = _run(answers, env_path, sections=("github",))
        summary = "\n".join(prompter.output)
        assert "ghp_supersecret_12345678" not in summary
        assert "HENCHMEN_GITHUB_TOKEN" in summary and "****5678" in summary

    def test_section_filter_runs_only_requested_section(self, env_path: Path):
        code, prompter, env = _run(["ghp_x", "acme/app", "", "", "y"], env_path, sections=("github",))
        assert code == 0
        assert env.keys() == [
            "HENCHMEN_GITHUB_TOKEN",
            "HENCHMEN_GITHUB_DEFAULT_REPO",
            "HENCHMEN_GITHUB_DEFAULT_ORG",
            "HENCHMEN_GIT_AUTHOR_NAME",
            "HENCHMEN_GIT_AUTHOR_EMAIL",
        ]
        assert not any("Slack" in line for line in prompter.output)


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_options_from_args_defaults(self):
        parser = argparse.ArgumentParser()
        add_init_arguments(parser)
        options = options_from_args(parser.parse_args([]))
        assert options.env_file == Path(".env.local")
        assert options.sections == SECTIONS
        assert options.yes is False and options.dry_run is False

    def test_options_from_args_flags(self):
        parser = argparse.ArgumentParser()
        add_init_arguments(parser)
        args = parser.parse_args(
            ["--yes", "--dry-run", "--env-file", "x.env", "--section", "llm", "--section", "slack"]
        )
        options = options_from_args(args)
        assert options.yes and options.dry_run
        assert options.env_file == Path("x.env")
        assert options.sections == ("llm", "slack")

    def test_run_init_cli_uses_given_prompter(self, env_path: Path):
        parser = argparse.ArgumentParser()
        add_init_arguments(parser)
        args = parser.parse_args(["--env-file", str(env_path), "--section", "mode"])
        prompter = ScriptedPrompter(["1", "", "y"])
        assert run_init_cli(args, prompter=prompter) == 0
        assert EnvFile.load(env_path).get("HENCHMEN_PROVIDER") == "local"

    def test_init_defaults_to_the_data_dir_config_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
        assert InitOptions().env_file == tmp_path / "henchmen.env"
        assert options_from_args(argparse.Namespace(env_file=None)).env_file == tmp_path / "henchmen.env"
        assert options_from_args(argparse.Namespace(env_file="custom.env")).env_file.name == "custom.env"

    def test_init_without_data_dir_still_writes_env_local(self, monkeypatch):
        monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
        assert options_from_args(argparse.Namespace(env_file=None)).env_file == Path(".env.local")


# ---------------------------------------------------------------------------
# section_llm — AWS Bedrock branch
# ---------------------------------------------------------------------------


def test_bedrock_section_validates_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    regions: list[str] = []

    def fake_check(region: str, *, timeout: float) -> CheckResult:
        regions.append(region)
        return CheckResult("AWS Bedrock", CheckStatus.OK, "reachable in us-east-1 (1 models)")

    monkeypatch.setattr(checks, "check_bedrock", fake_check)
    monkeypatch.setattr(
        checks, "list_bedrock_models", lambda region, *, timeout: ["us.anthropic.claude-sonnet-4-20250514-v1:0"]
    )
    state = init.WizardState(env=EnvFile.load(tmp_path / ".env.local"))
    state.set("HENCHMEN_LLM_PROVIDER", "aws", "LLM")
    prompter = ScriptedPrompter([])

    init.section_llm(prompter, state, InitOptions(env_file=tmp_path / ".env.local", yes=True))

    assert regions == ["us-east-1"]
    assert any("AWS Bedrock" in line for line in prompter.output)
    assert not any("not validated live" in line for line in prompter.output)
