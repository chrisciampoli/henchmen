"""Unit tests for ``henchmen.forge.ci_runner``.

The CI runner is the only live Forge code path and was previously untested,
which is how a shallow-clone ``git diff HEAD~1`` could silently pass every
silent-failure scan in production. These tests exercise the real thing against
a real (tiny) shallow git clone.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from henchmen.forge.ci_runner import (
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    CIRunner,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required for CI runner tests")

_BASE_LINT_DEBT = "import sys\n"  # F401: pre-existing debt the PR never touches
_CLEAN_MODULE = "def add(a: int, b: int) -> int:\n    return a + b\n"
_SILENT_FAILURE_MODULE = "def risky(handler):\n    try:\n        handler()\n    except Exception:\n        pass\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_pr_clone(tmp_path: Path, added_files: dict[str, str]) -> Path:
    """Build an origin repo with a ``main`` base and a feature branch, then shallow-clone it.

    The clone mirrors what Forge does in production: ``--depth`` + a single
    branch, so ``HEAD~1`` and the base branch are both absent until fetched.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.email", "forge@test.local")
    _git(origin, "config", "user.name", "Forge Test")
    (origin / "legacy.py").write_text(_BASE_LINT_DEBT, encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "base")

    _git(origin, "checkout", "-b", "feature")
    for name, content in added_files.items():
        path = origin / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-m", "pr")
    _git(origin, "checkout", "main")

    workspace = tmp_path / "workspace"
    subprocess.run(
        [
            "git",
            "clone",
            "--depth=1",
            "--branch",
            "feature",
            "--single-branch",
            origin.as_uri(),
            str(workspace),
        ],
        check=True,
        capture_output=True,
    )
    return workspace


def _check(result: dict, name: str) -> dict:
    matches = [c for c in result["checks"] if c["name"] == name]
    assert matches, f"no {name} check in {[c['name'] for c in result['checks']]}"
    return matches[0]


# ---------------------------------------------------------------------------
# Silent-failure scan (forge#0: shallow clone must not fail open)
# ---------------------------------------------------------------------------


class TestSilentFailureScan:
    @pytest.mark.asyncio
    async def test_scan_detects_pattern_in_shallow_clone(self, tmp_path):
        """The scan must fetch the base and see the PR diff even in a depth=1 clone."""
        workspace = _make_pr_clone(tmp_path, {"added.py": _SILENT_FAILURE_MODULE})

        result = await CIRunner().run(str(workspace), base_ref="main")

        scan = _check(result, "silent_failure_scan")
        assert scan["status"] == STATUS_FAILED, scan
        assert scan["critical_count"] >= 1
        assert result["passed"] is False

    @pytest.mark.asyncio
    async def test_scan_passes_on_clean_pr(self, tmp_path):
        workspace = _make_pr_clone(tmp_path, {"added.py": _CLEAN_MODULE})

        result = await CIRunner().run(str(workspace), base_ref="main")

        assert _check(result, "silent_failure_scan")["status"] == STATUS_PASSED
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_missing_base_ref_fails_closed(self, tmp_path):
        """No base ref means no trustworthy diff — the scan must fail, not pass."""
        workspace = _make_pr_clone(tmp_path, {"added.py": _CLEAN_MODULE})

        result = await CIRunner().run(str(workspace), base_ref=None)

        scan = _check(result, "silent_failure_scan")
        assert scan["status"] == STATUS_FAILED
        assert "base ref" in scan["error"].lower()
        assert result["passed"] is False

    @pytest.mark.asyncio
    async def test_unknown_base_ref_fails_closed(self, tmp_path):
        workspace = _make_pr_clone(tmp_path, {"added.py": _CLEAN_MODULE})

        result = await CIRunner().run(str(workspace), base_ref="no-such-branch")

        assert _check(result, "silent_failure_scan")["status"] == STATUS_FAILED
        assert result["passed"] is False

    @pytest.mark.asyncio
    async def test_unsafe_base_ref_is_rejected(self, tmp_path):
        """A branch name that could be read as an option never reaches git."""
        workspace = _make_pr_clone(tmp_path, {"added.py": _CLEAN_MODULE})

        result = await CIRunner().run(str(workspace), base_ref="--upload-pack=touch pwned")

        scan = _check(result, "silent_failure_scan")
        assert scan["status"] == STATUS_FAILED
        assert "unsafe" in scan["error"].lower()


# ---------------------------------------------------------------------------
# Lint (forge#13: changed files only)
# ---------------------------------------------------------------------------


class TestLint:
    @pytest.mark.asyncio
    async def test_lint_ignores_pre_existing_debt_in_untouched_files(self, tmp_path):
        workspace = _make_pr_clone(tmp_path, {"added.py": _CLEAN_MODULE})

        result = await CIRunner().run(str(workspace), base_ref="main")

        lint = _check(result, "lint")
        assert lint["status"] == STATUS_PASSED, lint
        assert "legacy.py" not in lint["output"]

    @pytest.mark.asyncio
    async def test_lint_fails_on_changed_file(self, tmp_path):
        workspace = _make_pr_clone(tmp_path, {"added.py": "import json\n"})

        result = await CIRunner().run(str(workspace), base_ref="main")

        lint = _check(result, "lint")
        assert lint["status"] == STATUS_FAILED
        assert "added.py" in lint["output"]

    @pytest.mark.asyncio
    async def test_lint_passes_when_no_python_files_changed(self, tmp_path):
        workspace = _make_pr_clone(tmp_path, {"README.md": "# hello\n"})

        result = await CIRunner().run(str(workspace), base_ref="main")

        lint = _check(result, "lint")
        assert lint["status"] == STATUS_PASSED
        assert "No Python files changed" in lint["output"]

    @pytest.mark.asyncio
    async def test_lint_skipped_when_ruff_unavailable(self, tmp_path, monkeypatch):
        workspace = _make_pr_clone(tmp_path, {"added.py": _CLEAN_MODULE})
        monkeypatch.setattr(CIRunner, "_module_available", staticmethod(lambda name: name != "ruff"))

        result = await CIRunner().run(str(workspace), base_ref="main")

        lint = _check(result, "lint")
        assert lint["status"] == STATUS_SKIPPED
        assert "ruff is not installed" in lint["error"]
        assert "lint" in result["skipped"]
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# Tests check (forge#1 / forge#4: skipped is not passed)
# ---------------------------------------------------------------------------


class TestTestsCheck:
    @pytest.mark.asyncio
    async def test_node_project_without_test_script_is_skipped_not_passed(self, tmp_path):
        workspace = _make_pr_clone(
            tmp_path,
            {"package.json": '{"name": "app", "scripts": {"build": "tsc"}}'},
        )

        result = await CIRunner().run(str(workspace), base_ref="main")

        tests = _check(result, "tests")
        assert tests["status"] == STATUS_SKIPPED
        assert tests["passed"] is False
        assert "tests" in result["skipped"]
        assert "SKIP: tests" in result["summary"]
        # An unverified PR must not be reported as a CI pass overall either.
        assert result["passed"] is False
        assert result["incomplete"] is True

    @pytest.mark.asyncio
    async def test_node_project_without_npm_is_skipped(self, tmp_path, monkeypatch):
        workspace = _make_pr_clone(
            tmp_path,
            {"package.json": '{"name": "app", "scripts": {"test": "jest"}}'},
        )
        monkeypatch.setattr("henchmen.forge.ci_runner.shutil.which", lambda name: None)

        result = await CIRunner().run(str(workspace), base_ref="main")

        tests = _check(result, "tests")
        assert tests["status"] == STATUS_SKIPPED
        assert "npm is not available" in tests["error"]

    @pytest.mark.asyncio
    async def test_no_tests_directory_yields_no_tests_check(self, tmp_path):
        workspace = _make_pr_clone(tmp_path, {"added.py": _CLEAN_MODULE})

        result = await CIRunner().run(str(workspace), base_ref="main")

        assert [c["name"] for c in result["checks"]] == ["lint", "silent_failure_scan"]

    @pytest.mark.asyncio
    async def test_missing_pytest_is_reported_as_skipped_not_a_test_failure(self, tmp_path, monkeypatch):
        workspace = _make_pr_clone(tmp_path, {"tests/test_x.py": "def test_x():\n    assert True\n"})
        monkeypatch.setattr(CIRunner, "_module_available", staticmethod(lambda name: name != "pytest"))

        result = await CIRunner().run(str(workspace), base_ref="main")

        tests = _check(result, "tests")
        assert tests["status"] == STATUS_SKIPPED
        assert "pytest is not installed" in tests["error"]

    @pytest.mark.asyncio
    async def test_python_tests_run_and_fail_closed(self, tmp_path):
        workspace = _make_pr_clone(tmp_path, {"tests/test_x.py": "def test_x():\n    assert False\n"})

        result = await CIRunner().run(str(workspace), base_ref="main")

        tests = _check(result, "tests")
        assert tests["status"] == STATUS_FAILED
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# Subprocess safety (forge#11 + security sweep)
# ---------------------------------------------------------------------------


class TestSubprocessSafety:
    @pytest.mark.asyncio
    async def test_command_timeout_is_reported_not_hung(self, tmp_path):
        runner = CIRunner(timeout_seconds=1)

        rc, out, err = await runner._run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            str(tmp_path),
        )

        assert rc != 0
        assert "timed out" in err.lower()

    @pytest.mark.asyncio
    async def test_total_budget_bounds_the_whole_run(self, tmp_path):
        """The run must end inside the Pub/Sub ack deadline, not per-command budgets summed."""
        runner = CIRunner(timeout_seconds=60, total_budget_seconds=0)
        runner._deadline = 0.0  # already exhausted

        rc, _out, err = await runner._run_command([sys.executable, "-c", "print('never')"], str(tmp_path))

        assert rc != 0
        assert "budget" in err.lower()

    def test_default_budget_fits_inside_pubsub_ack_deadline(self):
        from henchmen.forge.ci_runner import DEFAULT_CI_TIMEOUT_SECONDS

        assert DEFAULT_CI_TIMEOUT_SECONDS < 600

    @pytest.mark.asyncio
    async def test_missing_executable_is_reported(self, tmp_path):
        rc, _out, err = await CIRunner()._run_command(["henchmen-not-a-real-binary"], str(tmp_path))

        assert rc != 0
        assert "could not run" in err.lower()

    def test_child_env_strips_henchmen_secrets(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecret")
        monkeypatch.setenv("HENCHMEN_ANTHROPIC_API_KEY", "sk-secret")
        monkeypatch.setenv("SOME_PASSWORD", "hunter2")
        monkeypatch.setenv("HARMLESS_SETTING", "keep-me")

        env = CIRunner()._child_env()

        assert "GITHUB_TOKEN" not in env
        assert "HENCHMEN_ANTHROPIC_API_KEY" not in env
        assert "SOME_PASSWORD" not in env
        assert env["HARMLESS_SETTING"] == "keep-me"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        # PATH must survive or no subprocess could be found at all.
        assert any(key.upper() == "PATH" for key in env)

    def test_sanitize_redacts_token_and_credential_urls(self):
        runner = CIRunner(redact=["ghp_supersecret"])

        text = runner._sanitize("fatal: could not read https://x-access-token:ghp_supersecret@github.com/acme/repo.git")

        assert "ghp_supersecret" not in text
        assert "***" in text

    @pytest.mark.asyncio
    async def test_git_failure_output_never_leaks_the_token(self, tmp_path):
        """A failed fetch prints the remote URL — the embedded token must be redacted."""
        workspace = _make_pr_clone(tmp_path, {"added.py": _CLEAN_MODULE})
        subprocess.run(
            [
                "git",
                "remote",
                "set-url",
                "origin",
                "https://x-access-token:ghp_leakme@127.0.0.1:1/acme/repo.git",
            ],
            cwd=str(workspace),
            check=True,
            capture_output=True,
        )

        result = await CIRunner(redact=["ghp_leakme"], timeout_seconds=30).run(str(workspace), base_ref="main")

        blob = str(result)
        assert "ghp_leakme" not in blob
        assert result["passed"] is False


def test_a_non_utf8_package_json_reports_tests_as_skipped(tmp_path) -> None:
    """M1: an undecodable manifest is "no test script" (skipped), never an exception out of the run."""
    import asyncio

    from henchmen.forge.ci_runner import STATUS_SKIPPED, CIRunner

    (tmp_path / "package.json").write_bytes(b'{"scripts": {"test": "\xff\xfe"}}')
    check = asyncio.run(CIRunner(timeout_seconds=5)._run_node_tests(str(tmp_path)))
    assert check["status"] == STATUS_SKIPPED
    assert "no `test` script" in check["error"]
