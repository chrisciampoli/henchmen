"""Tests for the Mastermind lint gate scoping: only the operative's changed files are judged."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.mastermind.scheme_executor.lint_scope import (
    CheckCommand,
    LintScopeError,
    changed_files,
    plan_lint,
    to_shell_script,
)
from henchmen.utils.stack_detector import detect_stack

_HAS_GIT = shutil.which("git") is not None


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


def _write(root: Path, rel: str, content: str = "x = 1\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def branch_clone(tmp_path: Path) -> Path:
    """A clone on branch ``henchmen/t`` that changed two files; ``main`` then moved on separately."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _write(seed, "pyproject.toml", "[project]\nname='x'\n")
    _write(seed, "legacy.py", "import os\n")  # pre-existing violation the operative never touched
    _write(seed, "doomed.py")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(remote))

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    _git(work, "checkout", "-q", "-b", "henchmen/t")
    _write(work, "pkg/new module.py")
    _git(work, "rm", "-q", "doomed.py")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "operative change")
    _git(work, "push", "-q", "origin", "henchmen/t")

    # main moves on after the branch was cut: not the operative's change.
    _write(seed, "upstream.py")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "upstream")
    _git(seed, "push", "-q", str(remote), "main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--branch", "henchmen/t", "--single-branch", str(remote), str(clone))
    return clone


@pytest.mark.skipif(not _HAS_GIT, reason="git is not installed")
class TestChangedFiles:
    @pytest.mark.asyncio
    async def test_lists_only_the_branch_changes(self, branch_clone: Path) -> None:
        files = await changed_files(str(branch_clone), "main")
        assert sorted(files) == ["doomed.py", "pkg/new module.py"]

    @pytest.mark.asyncio
    async def test_missing_base_branch_raises(self, branch_clone: Path) -> None:
        with pytest.raises(LintScopeError, match="git fetch failed"):
            await changed_files(str(branch_clone), "no-such-branch")


class TestPlanLint:
    def test_python_lints_only_existing_changed_py_files(self, tmp_path: Path) -> None:
        _write(tmp_path, "pyproject.toml", "")
        _write(tmp_path, "src/a.py")
        _write(tmp_path, "README.md", "")
        plan = plan_lint(detect_stack(tmp_path), tmp_path, ["src/a.py", "README.md", "deleted.py"])
        assert plan.commands == (CheckCommand(argv=("python", "-m", "ruff", "check", "--force-exclude", "./src/a.py")),)
        assert "." not in plan.commands[0].argv

    def test_python_without_changed_py_files_skips(self, tmp_path: Path) -> None:
        _write(tmp_path, "pyproject.toml", "")
        _write(tmp_path, "docs/x.md", "")
        plan = plan_lint(detect_stack(tmp_path), tmp_path, ["docs/x.md"])
        assert plan.commands == ()
        assert "no changed Python files" in plan.skip_reason

    def test_dash_prefixed_path_is_not_a_flag(self, tmp_path: Path) -> None:
        _write(tmp_path, "pyproject.toml", "")
        _write(tmp_path, "-rf.py")
        plan = plan_lint(detect_stack(tmp_path), tmp_path, ["-rf.py"])
        assert plan.commands[0].argv[-1] == "./-rf.py"

    def test_node_monorepo_runs_eslint_per_package(self, tmp_path: Path) -> None:
        _write(tmp_path, "package.json", json.dumps({"devDependencies": {"eslint": "^9"}}))
        _write(tmp_path, "pnpm-lock.yaml", "")
        _write(tmp_path, "apps/api/package.json", json.dumps({"name": "api"}))
        _write(tmp_path, "apps/api/src/a.ts", "")
        _write(tmp_path, "tools/b.js", "")
        plan = plan_lint(detect_stack(tmp_path), tmp_path, ["apps/api/src/a.ts", "tools/b.js", "pnpm-lock.yaml"])
        assert plan.commands == (
            CheckCommand(argv=("pnpm", "exec", "eslint", "./tools/b.js"), cwd="."),
            CheckCommand(argv=("pnpm", "exec", "eslint", "./src/a.ts"), cwd="apps/api"),
        )

    def test_node_without_eslint_anywhere_skips(self, tmp_path: Path) -> None:
        _write(tmp_path, "package.json", json.dumps({"scripts": {"test": "jest"}}))
        _write(tmp_path, "index.js", "")
        plan = plan_lint(detect_stack(tmp_path), tmp_path, ["index.js"])
        assert plan.commands == ()
        assert "no ESLint configured" in plan.skip_reason

    def test_npm_uses_the_installed_eslint_only(self, tmp_path: Path) -> None:
        _write(tmp_path, "package.json", "{}")
        _write(tmp_path, "eslint.config.js", "")
        _write(tmp_path, "index.js", "")
        plan = plan_lint(detect_stack(tmp_path), tmp_path, ["index.js"])
        assert plan.commands == (CheckCommand(argv=("npx", "--no-install", "eslint", "./index.js")),)

    def test_unreadable_package_json_fails_closed(self, tmp_path: Path) -> None:
        _write(tmp_path, "package.json", "{not json")
        _write(tmp_path, "index.js", "")
        with pytest.raises(LintScopeError, match="package.json"):
            plan_lint(detect_stack(tmp_path), tmp_path, ["index.js"])

    def test_go_vets_changed_packages(self, tmp_path: Path) -> None:
        _write(tmp_path, "go.mod", "module x\n")
        _write(tmp_path, "main.go", "")
        _write(tmp_path, "internal/db/db.go", "")
        _write(tmp_path, "internal/db/db_test.go", "")
        plan = plan_lint(detect_stack(tmp_path), tmp_path, ["internal/db/db.go", "internal/db/db_test.go", "main.go"])
        assert plan.commands == (CheckCommand(argv=("go", "vet", ".", "./internal/db")),)

    def test_rust_runs_the_crate_lint_only_when_rust_changed(self, tmp_path: Path) -> None:
        _write(tmp_path, "Cargo.toml", "")
        stack = detect_stack(tmp_path)
        assert plan_lint(stack, tmp_path, ["README.md"]).commands == ()
        assert plan_lint(stack, tmp_path, ["src/lib.rs"]).commands == (CheckCommand(argv=tuple(stack.lint_command)),)


class TestShellScript:
    def test_every_command_runs_and_any_failure_fails(self) -> None:
        script = to_shell_script(
            (CheckCommand(argv=("eslint", "./a b.ts")), CheckCommand(argv=("eslint", "./c.ts"), cwd="apps/x y")),
            "pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile",
        )
        assert script.startswith("{ pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile; } && {")
        assert "( cd . && eslint './a b.ts' ) || rc=$?;" in script
        assert "( cd 'apps/x y' && eslint ./c.ts ) || rc=$?;" in script
        assert script.rstrip("; }").endswith("exit $rc")

    @pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="needs a POSIX bash")
    def test_script_exit_code_reflects_a_failing_command(self, tmp_path: Path) -> None:
        script = to_shell_script((CheckCommand(argv=("false",)), CheckCommand(argv=("true",))), None)
        assert subprocess.run(["bash", "-c", script], cwd=tmp_path, check=False).returncode != 0


class TestRunLintGate:
    """The handler fails closed when the diff is unavailable and lints only changed files otherwise."""

    @staticmethod
    def _task() -> MagicMock:
        task = MagicMock()
        task.context.repo = "acme/widgets"
        task.context.branch = "main"
        task.branch_name = "henchmen/t"
        task.id = "t-1"
        return task

    @staticmethod
    def _settings(provider: str = "gcp") -> MagicMock:
        settings = MagicMock()
        settings.github_token = "ghp_secret"
        settings.provider = provider
        return settings

    @pytest.mark.asyncio
    async def test_uncomputable_diff_fails_closed(self, tmp_path: Path) -> None:
        # Clone/detect/scope now live in ci_gate.plan_gate (shared with the local
        # gate container, per Task 8's anti-duplication ruling), so that is what
        # gets patched; handlers._run_on_host is still where the cloud path runs
        # the scoped commands.
        from henchmen.mastermind.scheme_executor import ci_gate, handlers

        _write(tmp_path, "pyproject.toml", "")
        with (
            patch("henchmen.config.settings.get_settings", return_value=self._settings()),
            patch.object(handlers.tempfile, "mkdtemp", return_value=str(tmp_path)),
            patch.object(handlers.shutil, "rmtree"),
            patch.object(ci_gate, "clone_repo", new=AsyncMock()),
            patch.object(
                ci_gate, "changed_files", new=AsyncMock(side_effect=LintScopeError("git fetch failed: ghp_secret"))
            ),
            patch.object(handlers, "_run_on_host", new=AsyncMock()) as run,
        ):
            result = await handlers._run_ci_check(MagicMock(), self._task(), "lint")
        assert result["condition"] == "fail"
        assert "could not determine the files changed" in result["message"]
        assert "ghp_secret" not in result["message"]
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_relevant_changes_passes_without_running_the_linter(self, tmp_path: Path) -> None:
        from henchmen.mastermind.scheme_executor import ci_gate, handlers

        _write(tmp_path, "pyproject.toml", "")
        _write(tmp_path, "README.md", "")
        with (
            patch("henchmen.config.settings.get_settings", return_value=self._settings()),
            patch.object(handlers.tempfile, "mkdtemp", return_value=str(tmp_path)),
            patch.object(handlers.shutil, "rmtree"),
            patch.object(ci_gate, "clone_repo", new=AsyncMock()),
            patch.object(ci_gate, "changed_files", new=AsyncMock(return_value=["README.md"])),
            patch.object(handlers, "_run_on_host", new=AsyncMock()) as run,
        ):
            result = await handlers._run_ci_check(MagicMock(), self._task(), "lint")
        assert result["condition"] == "pass"
        assert "no changed Python files" in result["message"]
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_host_lint_receives_only_changed_files(self, tmp_path: Path) -> None:
        from henchmen.mastermind.scheme_executor import ci_gate, handlers

        _write(tmp_path, "pyproject.toml", "")
        _write(tmp_path, "src/changed.py")
        _write(tmp_path, "legacy.py", "import os\n")
        with (
            patch("henchmen.config.settings.get_settings", return_value=self._settings()),
            patch.object(handlers.tempfile, "mkdtemp", return_value=str(tmp_path)),
            patch.object(handlers.shutil, "rmtree"),
            patch.object(ci_gate, "clone_repo", new=AsyncMock()),
            patch.object(ci_gate, "changed_files", new=AsyncMock(return_value=["src/changed.py"])),
            patch.object(handlers, "_run_on_host", new=AsyncMock(return_value={"returncode": 1, "output": "E"})) as run,
        ):
            result = await handlers._run_ci_check(MagicMock(), self._task(), "lint")
        assert result["condition"] == "fail"
        (commands,) = run.await_args.args[2:]
        expected = ("python", "-m", "ruff", "check", "--force-exclude", "./src/changed.py")
        assert commands == (CheckCommand(argv=expected),)

    @pytest.mark.asyncio
    async def test_tests_check_does_not_need_a_diff(self, tmp_path: Path) -> None:
        from henchmen.mastermind.scheme_executor import ci_gate, handlers

        _write(tmp_path, "pyproject.toml", "")
        with (
            patch("henchmen.config.settings.get_settings", return_value=self._settings()),
            patch.object(handlers.tempfile, "mkdtemp", return_value=str(tmp_path)),
            patch.object(handlers.shutil, "rmtree"),
            patch.object(ci_gate, "clone_repo", new=AsyncMock()),
            patch.object(ci_gate, "changed_files", new=AsyncMock(side_effect=AssertionError("diffed"))),
            patch.object(handlers, "_run_on_host", new=AsyncMock(return_value={"returncode": 0, "output": ""})) as run,
        ):
            result = await handlers._run_ci_check(MagicMock(), self._task(), "tests")
        assert result["condition"] == "pass"
        (commands,) = run.await_args.args[2:]
        assert commands[0].argv[:3] == ("python", "-m", "pytest")
