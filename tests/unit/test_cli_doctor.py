"""Unit tests for the ``henchmen doctor`` CLI subcommand."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from henchmen.cli.doctor import (
    CheckResult,
    CheckStatus,
    check_docker,
    check_env_file,
    check_git_identity,
    check_llm_credentials,
    check_operative_image,
    check_python_version,
    run_doctor,
)

# ---------------------------------------------------------------------------
# CheckResult helper type
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_ok_status(self):
        r = CheckResult(name="thing", status=CheckStatus.OK, message="all good")
        assert r.status == CheckStatus.OK
        assert r.is_ok is True
        assert r.is_failure is False

    def test_fail_status(self):
        r = CheckResult(name="thing", status=CheckStatus.FAIL, message="bad")
        assert r.is_ok is False
        assert r.is_failure is True

    def test_warn_status(self):
        r = CheckResult(name="thing", status=CheckStatus.WARN, message="maybe")
        assert r.is_ok is False
        assert r.is_failure is False


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


class TestCheckDocker:
    def test_docker_present_and_running(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "Server Version: 24.0.5\n"
            result = check_docker()
        assert result.status == CheckStatus.OK
        assert "24.0" in result.message or "running" in result.message.lower()

    def test_docker_not_running(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "Cannot connect to daemon"
            result = check_docker()
        assert result.status == CheckStatus.FAIL

    def test_docker_not_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("docker")):
            result = check_docker()
        assert result.status == CheckStatus.FAIL
        assert "not installed" in result.message.lower() or "not found" in result.message.lower()


class TestCheckPythonVersion:
    def test_python_312_passes(self):
        with patch("sys.version_info", (3, 12, 0, "final", 0)):
            result = check_python_version()
        assert result.status == CheckStatus.OK

    def test_python_313_passes(self):
        with patch("sys.version_info", (3, 13, 2, "final", 0)):
            result = check_python_version()
        assert result.status == CheckStatus.OK

    def test_python_311_fails(self):
        with patch("sys.version_info", (3, 11, 0, "final", 0)):
            result = check_python_version()
        assert result.status == CheckStatus.FAIL


class TestCheckGitIdentity:
    def test_git_identity_configured(self):
        def fake_run(args, *a, **kw):
            out = "Chris\n" if "user.name" in args else "chris@example.com\n"
            mock = type("M", (), {})()
            mock.returncode = 0
            mock.stdout = out
            return mock

        with patch("subprocess.run", side_effect=fake_run):
            result = check_git_identity()
        assert result.status == CheckStatus.OK

    def test_git_identity_missing(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            result = check_git_identity()
        assert result.status == CheckStatus.FAIL


class TestCheckEnvFile:
    def test_env_local_present(self, tmp_path, monkeypatch):
        (tmp_path / ".env.local").write_text("HENCHMEN_GCP_PROJECT_ID=test\n")
        monkeypatch.chdir(tmp_path)
        result = check_env_file()
        assert result.status == CheckStatus.OK

    def test_env_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = check_env_file()
        assert result.status == CheckStatus.WARN  # non-fatal

    def test_env_example_present_as_fallback(self, tmp_path, monkeypatch):
        (tmp_path / ".env.example").write_text("HENCHMEN_GCP_PROJECT_ID=\n")
        monkeypatch.chdir(tmp_path)
        result = check_env_file()
        assert result.status == CheckStatus.WARN


class TestCheckLLMCredentials:
    def test_ollama_provider_ok(self, monkeypatch):
        monkeypatch.setenv("HENCHMEN_LLM_PROVIDER", "ollama")
        # Ollama doesn't need API keys.
        result = check_llm_credentials()
        assert result.status == CheckStatus.OK

    def test_openai_provider_with_key(self, monkeypatch):
        monkeypatch.setenv("HENCHMEN_LLM_PROVIDER", "openai")
        monkeypatch.setenv("HENCHMEN_OPENAI_API_KEY", "sk-test")
        result = check_llm_credentials()
        assert result.status == CheckStatus.OK

    def test_openai_provider_missing_key(self, monkeypatch):
        monkeypatch.setenv("HENCHMEN_LLM_PROVIDER", "openai")
        monkeypatch.delenv("HENCHMEN_OPENAI_API_KEY", raising=False)
        result = check_llm_credentials()
        assert result.status == CheckStatus.FAIL

    def test_anthropic_provider_missing_key(self, monkeypatch):
        monkeypatch.setenv("HENCHMEN_LLM_PROVIDER", "anthropic")
        monkeypatch.delenv("HENCHMEN_ANTHROPIC_API_KEY", raising=False)
        result = check_llm_credentials()
        assert result.status == CheckStatus.FAIL


class TestCheckOperativeImage:
    def test_image_exists(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = check_operative_image()
        assert result.status == CheckStatus.OK

    def test_image_missing_but_buildable(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            result = check_operative_image()
        # Missing image is a warning, not a failure — it can be built on demand.
        assert result.status == CheckStatus.WARN


# ---------------------------------------------------------------------------
# run_doctor end-to-end
# ---------------------------------------------------------------------------


class TestRunDoctor:
    def test_run_doctor_returns_results_list(self):
        results = run_doctor()
        assert isinstance(results, list)
        assert len(results) > 0
        for r in results:
            assert isinstance(r, CheckResult)

    def test_run_doctor_returns_nonzero_on_any_failure(self):
        with patch("henchmen.cli.doctor.check_docker") as mock_check:
            mock_check.return_value = CheckResult(name="Docker", status=CheckStatus.FAIL, message="Not installed")
            results = run_doctor()
        assert any(r.is_failure for r in results)

    def test_run_doctor_all_ok_when_stubbed(self):
        ok = lambda name: CheckResult(name=name, status=CheckStatus.OK, message="ok")  # noqa: E731
        with (
            patch("henchmen.cli.doctor.check_python_version", return_value=ok("python")),
            patch("henchmen.cli.doctor.check_docker", return_value=ok("docker")),
            patch("henchmen.cli.doctor.check_git_identity", return_value=ok("git")),
            patch("henchmen.cli.doctor.check_env_file", return_value=ok("env")),
            patch("henchmen.cli.doctor.check_llm_credentials", return_value=ok("llm")),
            patch("henchmen.cli.doctor.check_operative_image", return_value=ok("operative")),
        ):
            results = run_doctor()
        assert all(r.is_ok for r in results)


# ---------------------------------------------------------------------------
# CLI wiring (henchmen doctor)
# ---------------------------------------------------------------------------


class TestDoctorCLIWiring:
    def test_doctor_subcommand_registered(self):
        from henchmen.cli import main

        with patch("sys.argv", ["henchmen", "doctor"]), patch("henchmen.cli.doctor.run_doctor") as mock_run:
            mock_run.return_value = [
                CheckResult(name="Python version", status=CheckStatus.OK, message="3.12"),
            ]
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code == 0

    def test_doctor_exits_nonzero_on_failure(self):
        from henchmen.cli import main

        with patch("sys.argv", ["henchmen", "doctor"]), patch("henchmen.cli.doctor.run_doctor") as mock_run:
            mock_run.return_value = [
                CheckResult(name="Docker", status=CheckStatus.FAIL, message="Not installed"),
            ]
            with pytest.raises(SystemExit) as excinfo:
                main()
            assert excinfo.value.code != 0
