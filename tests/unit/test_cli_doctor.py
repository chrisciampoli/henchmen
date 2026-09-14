"""Unit tests for the ``henchmen doctor`` CLI subcommand.

Every test here is hermetic: ``subprocess.run`` and the live ``checks.*``
probes are patched, so the suite never shells out to a real docker/git nor
touches the network.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from henchmen.cli import doctor
from henchmen.cli.doctor import (
    CheckResult,
    CheckStatus,
    check_docker,
    check_env_file,
    check_git_identity,
    check_github,
    check_jira,
    check_llm_credentials,
    check_model_pricing,
    check_model_tiers,
    check_operative_image,
    check_python_version,
    check_runtime_config,
    check_settings,
    check_slack,
    run_doctor,
)
from henchmen.config.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    """Run every test from an empty directory with no HENCHMEN_ vars leaking in."""
    for key in list(__import__("os").environ):
        if key.startswith("HENCHMEN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    yield


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"provider": "local"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CheckResult helper type (re-exported from henchmen.cli.checks)
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_reexported_from_checks_module(self) -> None:
        from henchmen.cli import checks

        assert CheckResult is checks.CheckResult
        assert CheckStatus is checks.CheckStatus

    def test_ok_status(self) -> None:
        r = CheckResult(name="thing", status=CheckStatus.OK, message="all good")
        assert r.is_ok is True
        assert r.is_failure is False

    def test_fail_status(self) -> None:
        r = CheckResult(name="thing", status=CheckStatus.FAIL, message="bad")
        assert r.is_ok is False
        assert r.is_failure is True

    def test_warn_status(self) -> None:
        r = CheckResult(name="thing", status=CheckStatus.WARN, message="maybe")
        assert r.is_ok is False
        assert r.is_failure is False


# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------


class TestCheckDocker:
    def test_docker_present_and_running(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "Server Version: 24.0.5\n"
            result = check_docker()
        assert result.status == CheckStatus.OK
        assert "24.0" in result.message or "running" in result.message.lower()

    def test_docker_not_running(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "Cannot connect to daemon"
            result = check_docker()
        assert result.status == CheckStatus.FAIL

    def test_docker_not_installed(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("docker")):
            result = check_docker()
        assert result.status == CheckStatus.FAIL
        assert "not found" in result.message.lower()


class TestCheckPythonVersion:
    def test_python_312_passes(self) -> None:
        with patch("sys.version_info", (3, 12, 0, "final", 0)):
            assert check_python_version().status == CheckStatus.OK

    def test_python_311_fails(self) -> None:
        with patch("sys.version_info", (3, 11, 0, "final", 0)):
            assert check_python_version().status == CheckStatus.FAIL


class TestCheckGitIdentity:
    def test_git_identity_configured(self) -> None:
        def fake_run(args, *a, **kw):
            out = "Chris\n" if "user.name" in args else "chris@example.com\n"
            mock = type("M", (), {})()
            mock.returncode = 0
            mock.stdout = out
            return mock

        with patch("subprocess.run", side_effect=fake_run):
            assert check_git_identity().status == CheckStatus.OK

    def test_git_identity_missing(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert check_git_identity().status == CheckStatus.FAIL


class TestCheckEnvFile:
    def test_env_local_present(self, tmp_path, monkeypatch) -> None:
        (tmp_path / ".env.local").write_text("HENCHMEN_GCP_PROJECT_ID=test\n")
        monkeypatch.chdir(tmp_path)
        assert check_env_file().status == CheckStatus.OK

    def test_env_file_missing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert check_env_file().status == CheckStatus.WARN

    def test_check_env_file_uses_the_data_dir(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
        assert check_env_file().status == CheckStatus.WARN
        (tmp_path / "henchmen.env").write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")
        result = check_env_file()
        assert result.status == CheckStatus.OK
        assert str(tmp_path / "henchmen.env") in result.message


# ---------------------------------------------------------------------------
# Settings-driven checks
# ---------------------------------------------------------------------------


class TestCheckSettings:
    def test_reports_validation_failure_with_init_hint(self, tmp_path, monkeypatch) -> None:
        # provider=gcp without a project id is the most common startup failure.
        (tmp_path / ".env.local").write_text("HENCHMEN_PROVIDER=gcp\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = check_settings()
        assert result.status == CheckStatus.FAIL
        assert "henchmen init" in (result.hint or "")

    def test_reads_env_local_rather_than_os_environ(self, tmp_path, monkeypatch) -> None:
        (tmp_path / ".env.local").write_text(
            "HENCHMEN_PROVIDER=local\nHENCHMEN_LLM_PROVIDER=anthropic\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        settings, result = doctor.load_settings()
        assert settings is not None
        assert result.status == CheckStatus.OK
        assert "llm=anthropic" in result.message


class TestCheckModelTiers:
    def test_lists_resolved_models(self) -> None:
        result = check_model_tiers(_settings(llm_provider="anthropic"))
        assert result.status == CheckStatus.OK
        assert "complex=" in result.message and "light=" in result.message

    def test_fails_when_a_tier_has_no_model(self) -> None:
        settings = _settings(llm_provider="openai", openai_model_light="")
        result = check_model_tiers(settings)
        assert result.status == CheckStatus.FAIL
        assert "default/light" in result.message


class TestCheckModelPricing:
    def test_priced_defaults_are_ok(self) -> None:
        result = check_model_pricing(_settings(llm_provider="anthropic"))
        assert result.status == CheckStatus.OK

    def test_unpriced_tier_model_warns(self) -> None:
        """An unpriced model costs $0, so the task cost ceiling can never trip."""
        result = check_model_pricing(_settings(llm_provider="openai", openai_model_complex="gpt-made-up-9"))
        assert result.status == CheckStatus.WARN
        assert "gpt-made-up-9" in result.message
        assert "ceiling" in result.message
        assert result.hint is not None and "PRICE_TABLE" in result.hint

    def test_local_models_are_not_flagged(self) -> None:
        result = check_model_pricing(_settings(llm_provider="local", llm_ollama_model="qwen-unpriced"))
        assert result.status == CheckStatus.OK

    def test_run_doctor_includes_the_pricing_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        with (
            patch("henchmen.cli.doctor.check_docker", return_value=CheckResult("Docker", CheckStatus.OK, "")),
            patch("henchmen.cli.doctor.check_git_identity", return_value=CheckResult("Git", CheckStatus.OK, "")),
            patch("henchmen.cli.doctor.check_operative_image", return_value=CheckResult("Img", CheckStatus.OK, "")),
        ):
            names = [r.name for r in run_doctor(offline=True)]
        assert "Model pricing" in names


class TestCheckLLMCredentials:
    def test_ollama_probed_live(self) -> None:
        ok = CheckResult(name="Ollama", status=CheckStatus.OK, message="reachable")
        with patch("henchmen.cli.checks.check_ollama", return_value=ok) as probe:
            result = check_llm_credentials(_settings(llm_provider="ollama"))
        assert result.status == CheckStatus.OK
        probe.assert_called_once()

    def test_offline_skips_the_probe(self) -> None:
        with patch("henchmen.cli.checks.check_ollama") as probe:
            result = check_llm_credentials(_settings(llm_provider="local"), offline=True)
        assert result.status == CheckStatus.OK
        probe.assert_not_called()

    def test_openai_with_prefixed_key_is_probed(self) -> None:
        ok = CheckResult(name="OpenAI API key", status=CheckStatus.OK, message="valid")
        with patch("henchmen.cli.checks.check_openai_key", return_value=ok) as probe:
            result = check_llm_credentials(_settings(llm_provider="openai", openai_api_key="sk-test"))
        assert result.status == CheckStatus.OK
        probe.assert_called_once_with("sk-test")

    def test_openai_missing_key_fails(self) -> None:
        result = check_llm_credentials(_settings(llm_provider="openai"))
        assert result.status == CheckStatus.FAIL

    def test_bare_openai_api_key_is_not_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The providers only read HENCHMEN_OPENAI_API_KEY — doctor must agree."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-bare")
        result = check_llm_credentials(_settings(llm_provider="openai"))
        assert result.status == CheckStatus.FAIL
        assert "HENCHMEN_OPENAI_API_KEY" in (result.hint or "")

    def test_bare_anthropic_api_key_is_not_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-bare")
        result = check_llm_credentials(_settings(llm_provider="anthropic"))
        assert result.status == CheckStatus.FAIL

    def test_anthropic_missing_key_fails(self) -> None:
        assert check_llm_credentials(_settings(llm_provider="anthropic")).status == CheckStatus.FAIL

    def test_vertex_alias_resolves_to_gcp(self) -> None:
        ok = CheckResult(name="Vertex AI credentials", status=CheckStatus.OK, message="ADC present")
        with patch("henchmen.cli.checks.check_vertex", return_value=ok) as probe:
            result = check_llm_credentials(_settings(provider="local", llm_provider="vertex", gcp_project_id="p"))
        assert result.status == CheckStatus.OK
        probe.assert_called_once_with("p", "us-central1")


class TestCheckIntegrations:
    def test_github_without_token_warns(self) -> None:
        assert check_github(_settings()).status == CheckStatus.WARN

    def test_github_with_default_repo_checks_the_repo(self) -> None:
        ok = CheckResult(name="GitHub repository", status=CheckStatus.OK, message="reachable")
        settings = _settings(github_token="ghp_x", github_default_org="acme", github_default_repo="widgets")
        with patch("henchmen.cli.checks.check_github_repo", return_value=ok) as probe:
            result = check_github(settings)
        assert result.status == CheckStatus.OK
        probe.assert_called_once_with("ghp_x", "acme/widgets")

    def test_slack_not_configured_is_ok(self) -> None:
        assert check_slack(_settings()).status == CheckStatus.OK

    def test_slack_bad_bot_token_fails(self) -> None:
        bad = CheckResult(name="Slack bot token", status=CheckStatus.FAIL, message="rejected")
        with patch("henchmen.cli.checks.check_slack_bot_token", return_value=bad):
            result = check_slack(_settings(slack_bot_token="xoxb-bad"))
        assert result.status == CheckStatus.FAIL

    def test_jira_not_configured_is_ok(self) -> None:
        assert check_jira(_settings()).status == CheckStatus.OK

    def test_jira_configured_is_probed(self) -> None:
        ok = CheckResult(name="Jira", status=CheckStatus.OK, message="authenticated")
        settings = _settings(jira_base_url="https://x.atlassian.net", jira_email="a@b.c", jira_api_token="t")
        with patch("henchmen.cli.checks.check_jira", return_value=ok) as probe:
            result = check_jira(settings)
        assert result.status == CheckStatus.OK
        probe.assert_called_once()


class TestCheckRuntimeConfig:
    def test_reports_settings_problems(self) -> None:
        settings = _settings(llm_provider="anthropic")
        result = check_runtime_config(settings)
        assert result.status == CheckStatus.FAIL
        assert "ANTHROPIC_API_KEY" in result.message

    def test_clean_config_passes(self) -> None:
        settings = _settings(llm_provider="anthropic", anthropic_api_key="sk-ant-x")
        assert check_runtime_config(settings).status == CheckStatus.OK


class TestCheckOperativeImage:
    def test_image_exists(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            assert check_operative_image().status == CheckStatus.OK

    def test_image_missing_but_buildable(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            assert check_operative_image().status == CheckStatus.WARN


# ---------------------------------------------------------------------------
# run_doctor end-to-end (fully stubbed — no docker, no git, no network)
# ---------------------------------------------------------------------------


def _stub_all(monkeypatch: pytest.MonkeyPatch, *, docker_ok: bool = True) -> None:
    ok = lambda name: CheckResult(name=name, status=CheckStatus.OK, message="ok")  # noqa: E731
    monkeypatch.setattr(doctor, "check_python_version", lambda: ok("python"))
    monkeypatch.setattr(
        doctor,
        "check_docker",
        lambda: ok("docker") if docker_ok else CheckResult("Docker", CheckStatus.FAIL, "not installed"),
    )
    monkeypatch.setattr(doctor, "check_git_identity", lambda: ok("git"))
    monkeypatch.setattr(doctor, "check_operative_image", lambda: ok("operative"))


class TestRunDoctor:
    def test_returns_results_without_touching_docker_or_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_all(monkeypatch)
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: pytest.fail("doctor shelled out"))
        results = run_doctor(offline=True)
        assert results and all(isinstance(r, CheckResult) for r in results)

    def test_settings_failure_short_circuits(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_all(monkeypatch)
        (tmp_path / ".env.local").write_text("HENCHMEN_PROVIDER=gcp\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        results = run_doctor(offline=True)
        assert results[-1].name == "Settings"
        assert results[-1].is_failure

    def test_nonzero_on_any_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_all(monkeypatch, docker_ok=False)
        assert any(r.is_failure for r in run_doctor(offline=True))


# ---------------------------------------------------------------------------
# CLI wiring (henchmen doctor)
# ---------------------------------------------------------------------------


class TestDoctorCLIWiring:
    def test_doctor_subcommand_registered(self) -> None:
        from henchmen.cli import main

        with patch("sys.argv", ["henchmen", "doctor"]), patch("henchmen.cli.doctor.run_doctor") as mock_run:
            mock_run.return_value = [CheckResult("Python version", CheckStatus.OK, "3.12")]
            with pytest.raises(SystemExit) as excinfo:
                main()
        assert excinfo.value.code == 0

    def test_doctor_exits_nonzero_on_failure(self) -> None:
        from henchmen.cli import main

        with patch("sys.argv", ["henchmen", "doctor"]), patch("henchmen.cli.doctor.run_doctor") as mock_run:
            mock_run.return_value = [CheckResult("Docker", CheckStatus.FAIL, "Not installed")]
            with pytest.raises(SystemExit) as excinfo:
                main()
        assert excinfo.value.code != 0

    def test_offline_flag_is_forwarded(self) -> None:
        from henchmen.cli import main

        with patch("sys.argv", ["henchmen", "doctor", "--offline"]), patch("henchmen.cli.doctor.run_doctor") as mock:
            mock.return_value = []
            with pytest.raises(SystemExit):
                main()
        mock.assert_called_once_with(offline=True)

    def test_run_doctor_cli_without_args_namespace(self) -> None:
        with patch("henchmen.cli.doctor.run_doctor", return_value=[]) as mock:
            assert doctor.run_doctor_cli(argparse.Namespace()) == 0
        mock.assert_called_once_with(offline=False)


def test_check_operative_image_inspects_the_given_image():
    from unittest.mock import MagicMock, patch

    from henchmen.cli.doctor import CheckStatus, check_operative_image

    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
        result = check_operative_image("ghcr.io/acme/henchmen/operative:0.3.0")
    assert run.call_args.args[0] == ["docker", "image", "inspect", "ghcr.io/acme/henchmen/operative:0.3.0"]
    assert result.status == CheckStatus.OK
    assert "ghcr.io/acme/henchmen/operative:0.3.0" in result.message
