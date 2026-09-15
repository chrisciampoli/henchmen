"""Unit tests for Arsenal git tools, the workspace boundary, and the process runner.

These cover the paths that are easy to get wrong and expensive to get wrong:
the argv handed to git, the protected-branch guard, the force-push gate, and
the directory git actually runs in.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from henchmen.arsenal import _workspace
from henchmen.arsenal._process import run_command
from henchmen.arsenal._repo import current_repo_slug, normalize_repo_slug
from henchmen.arsenal._workspace import (
    current_workspace_dir,
    ensure_in_workspace,
    get_workspace_root,
    set_workspace_root,
)
from henchmen.arsenal.tools import git_ops


@pytest.fixture
def workspace(tmp_path: Path):
    """Point the Arsenal workspace boundary at a temporary directory."""
    root = tmp_path / "workspace"
    root.mkdir()
    set_workspace_root(root)
    yield root
    set_workspace_root(None)


@pytest.fixture
def fake_git(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace ``_run_git`` so tests assert argv without spawning git."""
    mock = AsyncMock(return_value={"stdout": "", "stderr": "", "return_code": 0, "success": True})
    monkeypatch.setattr(git_ops, "_run_git", mock)
    return mock


# ---------------------------------------------------------------------------
# ensure_in_workspace
# ---------------------------------------------------------------------------


class TestEnsureInWorkspace:
    def test_relative_path_resolves_against_root(self, workspace: Path):
        assert ensure_in_workspace("src/app.py") == os.path.join(os.path.realpath(workspace), "src", "app.py")

    def test_absolute_path_inside_root_allowed(self, workspace: Path):
        target = workspace / "src" / "app.py"
        assert ensure_in_workspace(str(target)) == os.path.realpath(str(target))

    def test_root_itself_is_allowed(self, workspace: Path):
        assert ensure_in_workspace(str(workspace)) == os.path.realpath(str(workspace))

    def test_parent_traversal_rejected(self, workspace: Path):
        with pytest.raises(PermissionError):
            ensure_in_workspace("../../etc/passwd")

    def test_absolute_outside_root_rejected(self, workspace: Path, tmp_path: Path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        with pytest.raises(PermissionError):
            ensure_in_workspace(str(outside))

    def test_sibling_prefix_directory_rejected(self, tmp_path: Path):
        """'/workspaceX' must not pass a check for '/workspace'."""
        root = tmp_path / "workspace"
        root.mkdir()
        sibling = tmp_path / "workspaceX"
        sibling.mkdir()
        set_workspace_root(root)
        try:
            with pytest.raises(PermissionError):
                ensure_in_workspace(str(sibling / "leak.txt"))
        finally:
            set_workspace_root(None)

    def test_tilde_is_expanded_then_rejected(self, workspace: Path):
        with pytest.raises(PermissionError):
            ensure_in_workspace("~/secrets.txt")

    def test_empty_path_rejected(self, workspace: Path):
        with pytest.raises(PermissionError):
            ensure_in_workspace("")

    def test_root_reads_workspace_dir_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        set_workspace_root(None)
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        try:
            assert get_workspace_root() == os.path.realpath(str(tmp_path))
        finally:
            set_workspace_root(None)


class TestCurrentWorkspaceDir:
    def test_returns_cwd_when_inside_workspace(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        clone = workspace / "task-123"
        clone.mkdir()
        monkeypatch.chdir(clone)
        assert current_workspace_dir() == os.path.realpath(str(clone))

    def test_falls_back_to_root_when_cwd_outside(self, workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.chdir(outside)
        assert current_workspace_dir() == os.path.realpath(str(workspace))


# ---------------------------------------------------------------------------
# protected-branch parsing
# ---------------------------------------------------------------------------


class TestProtectedBranchParsing:
    @pytest.mark.parametrize(
        "value",
        [
            "main",
            "master",
            "develop",
            "trunk",
            "release/1.2",
            "origin/main",
            "refs/heads/main",
            "MAIN",
            "henchmen/x:main",
            "HEAD:main",
            "+henchmen/x:refs/heads/main",
        ],
    )
    def test_protected_values(self, value: str):
        assert git_ops._branch_is_protected(value) is True

    @pytest.mark.parametrize("value", ["henchmen/task-1", "feature/login", "fix-123"])
    def test_unprotected_values(self, value: str):
        assert git_ops._branch_is_protected(value) is False

    def test_none_is_protected(self):
        assert git_ops._branch_is_protected(None) is True

    @pytest.mark.parametrize(
        "value",
        ["--mirror", "--force", "-f", "+henchmen/x:main", "a:b:c", "henchmen x", " henchmen/x"],
    )
    def test_push_target_rejects_unsafe_values(self, value: str):
        assert git_ops._push_target_error(value) is not None

    def test_push_target_accepts_task_branch(self):
        assert git_ops._push_target_error("henchmen/task-1") is None


# ---------------------------------------------------------------------------
# git_push
# ---------------------------------------------------------------------------


class TestGitPush:
    @pytest.mark.asyncio
    async def test_default_push_sets_upstream_for_head(self, fake_git: AsyncMock):
        result = await git_ops.git_push()
        assert result["success"] is True
        assert fake_git.await_args.args == ("push", "--set-upstream", "origin", "HEAD")

    @pytest.mark.asyncio
    async def test_explicit_branch_sets_upstream(self, fake_git: AsyncMock):
        await git_ops.git_push(branch="henchmen/task-1")
        assert fake_git.await_args.args == ("push", "--set-upstream", "origin", "henchmen/task-1")

    @pytest.mark.parametrize("branch", ["main", "master", "refs/heads/main", "origin/main", "release/2.0"])
    @pytest.mark.asyncio
    async def test_refuses_protected_branches(self, fake_git: AsyncMock, branch: str):
        result = await git_ops.git_push(branch=branch)
        assert result["success"] is False
        assert "protected branch" in result["error"]
        fake_git.assert_not_awaited()

    @pytest.mark.parametrize("branch", ["henchmen/x:main", "HEAD:main", "+henchmen/x:main", "--force", "--mirror"])
    @pytest.mark.asyncio
    async def test_refuses_refspecs_and_options(self, fake_git: AsyncMock, branch: str):
        result = await git_ops.git_push(branch=branch)
        assert result["success"] is False
        fake_git.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_workflow_refusal_tells_the_agent_what_to_do(self, fake_git: AsyncMock):
        fake_git.return_value = {
            "stdout": "",
            "stderr": "refusing to allow a GitHub App to create or update workflow `.github/workflows/ci.yml` "
            "without `workflows` permission",
            "return_code": 1,
            "success": False,
        }
        result = await git_ops.git_push(branch="henchmen/task-1")
        assert result["success"] is False
        assert ".github/workflows" in result["error"]
        assert "Undo" in result["error"]


# ---------------------------------------------------------------------------
# git_force_push
# ---------------------------------------------------------------------------


class TestGitForcePush:
    @pytest.mark.asyncio
    async def test_disabled_by_default(self, fake_git: AsyncMock):
        result = await git_ops.git_force_push(branch="henchmen/task-1")
        assert result["success"] is False
        assert "HENCHMEN_ALLOW_FORCE_PUSH" in result["error"]
        fake_git.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enabled_via_settings(self, fake_git: AsyncMock, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_ALLOW_FORCE_PUSH", "true")
        result = await git_ops.git_force_push(branch="henchmen/task-1")
        assert result["success"] is True
        assert fake_git.await_args.args == ("push", "--force-with-lease", "origin", "henchmen/task-1")

    @pytest.mark.asyncio
    async def test_workflow_refusal_tells_the_agent_what_to_do(
        self, fake_git: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("HENCHMEN_ALLOW_FORCE_PUSH", "true")
        fake_git.return_value = {
            "stdout": "",
            "stderr": "refusing to allow a GitHub App to create or update workflow `.github/workflows/ci.yml` "
            "without `workflows` permission",
            "return_code": 1,
            "success": False,
        }
        result = await git_ops.git_force_push(branch="henchmen/task-1")
        assert result["success"] is False
        assert ".github/workflows" in result["error"]
        assert "Undo" in result["error"]

    @pytest.mark.asyncio
    async def test_refuses_implicit_head_when_enabled(self, fake_git: AsyncMock, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_ALLOW_FORCE_PUSH", "true")
        result = await git_ops.git_force_push()
        assert result["success"] is False
        fake_git.assert_not_awaited()

    @pytest.mark.parametrize(
        "branch",
        ["main", "refs/heads/main", "origin/main", "henchmen/x:main", "+henchmen/x:main", "--mirror"],
    )
    @pytest.mark.asyncio
    async def test_refuses_protected_targets_when_enabled(
        self, fake_git: AsyncMock, monkeypatch: pytest.MonkeyPatch, branch: str
    ):
        monkeypatch.setenv("HENCHMEN_ALLOW_FORCE_PUSH", "true")
        result = await git_ops.git_force_push(branch=branch)
        assert result["success"] is False
        fake_git.assert_not_awaited()


# ---------------------------------------------------------------------------
# git_branch_create
# ---------------------------------------------------------------------------


class TestGitBranchCreate:
    @pytest.mark.asyncio
    async def test_rejects_option_like_branch_name(self, fake_git: AsyncMock):
        result = await git_ops.git_branch_create(branch_name="--orphan")
        assert result["success"] is False
        fake_git.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_branch_from_explicit_base(self, fake_git: AsyncMock):
        result = await git_ops.git_branch_create(branch_name="henchmen/task-1", base_branch="main")
        assert result["branch_name"] == "henchmen/task-1"
        assert fake_git.await_args.args == ("checkout", "-b", "henchmen/task-1", "origin/main")

    @pytest.mark.asyncio
    async def test_default_base_is_the_repository_default_branch(
        self, fake_git: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ):
        import henchmen.operative.git_helpers as git_helpers

        monkeypatch.setattr(git_helpers, "detect_base_branch", AsyncMock(return_value="master"))

        await git_ops.git_branch_create(branch_name="henchmen/task-1")

        assert fake_git.await_args.args == ("checkout", "-b", "henchmen/task-1", "origin/master")

    @pytest.mark.asyncio
    async def test_rejects_option_like_base_branch(self, fake_git: AsyncMock):
        result = await git_ops.git_branch_create(branch_name="henchmen/task-1", base_branch="--mirror")
        assert result["success"] is False
        fake_git.assert_not_awaited()


# ---------------------------------------------------------------------------
# git_commit
# ---------------------------------------------------------------------------


class TestGitCommitWorkingDir:
    @pytest.mark.asyncio
    async def test_defaults_to_cwd_not_workspace_root(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, fake_git: AsyncMock
    ):
        """The clone lives at <root>/<task id>; the root itself has no .git."""
        clone = workspace / "task-123"
        clone.mkdir()
        monkeypatch.chdir(clone)

        await git_ops.git_commit(message="fix: thing")

        for call in fake_git.await_args_list:
            assert call.kwargs["working_dir"] == os.path.realpath(str(clone))

    @pytest.mark.asyncio
    async def test_falls_back_to_root_when_cwd_outside_workspace(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, fake_git: AsyncMock, tmp_path: Path
    ):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.chdir(outside)

        await git_ops.git_commit(message="fix: thing")

        assert fake_git.await_args_list[0].kwargs["working_dir"] == os.path.realpath(str(workspace))

    @pytest.mark.asyncio
    async def test_explicit_working_dir_is_validated(self, workspace: Path, fake_git: AsyncMock, tmp_path: Path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        result = await git_ops.git_commit(message="m", working_dir=str(outside))
        assert result["success"] is False
        assert "access denied" in result["error"]
        fake_git.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_message_rejected(self, workspace: Path, fake_git: AsyncMock):
        result = await git_ops.git_commit(message="   ")
        assert result["success"] is False
        fake_git.assert_not_awaited()


class TestGitCommitStaging:
    @pytest.mark.asyncio
    async def test_staged_file_outside_workspace_rejected(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, fake_git: AsyncMock
    ):
        monkeypatch.chdir(workspace)
        result = await git_ops.git_commit(message="m", files=["../../etc/passwd"])
        assert result["success"] is False
        assert "outside workspace" in result["error"]
        fake_git.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_silent_add_all_fallback(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, fake_git: AsyncMock
    ):
        """A bad path must surface, not quietly stage the whole worktree."""
        monkeypatch.chdir(workspace)
        fake_git.return_value = {
            "stdout": "",
            "stderr": "fatal: pathspec 'typo.py' did not match any files",
            "return_code": 128,
            "success": False,
        }
        result = await git_ops.git_commit(message="m", files=["typo.py"])

        assert result["success"] is False
        assert fake_git.await_count == 1
        assert fake_git.await_args.args[:2] == ("add", "--")

    @pytest.mark.asyncio
    async def test_no_files_stages_everything(self, workspace: Path, monkeypatch: pytest.MonkeyPatch, fake_git):
        monkeypatch.chdir(workspace)
        await git_ops.git_commit(message="m")
        assert fake_git.await_args_list[0].args == ("add", "-A")
        assert fake_git.await_args_list[1].args == ("commit", "-m", "m")


# ---------------------------------------------------------------------------
# repo slug helper
# ---------------------------------------------------------------------------


class TestRepoSlug:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("https://github.com/acme/widgets", "acme/widgets"),
            ("https://github.com/acme/widgets.git", "acme/widgets"),
            ("git@github.com:acme/widgets.git", "acme/widgets"),
            ("acme/widgets", "acme/widgets"),
            ("", ""),
            ("https://github.com/acme", ""),
        ],
    )
    def test_normalize(self, value: str, expected: str):
        assert normalize_repo_slug(value) == expected

    def test_prefers_repo_url_contract_variable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("REPO_URL", "https://github.com/acme/widgets.git")
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "fallback/repo")
        assert current_repo_slug() == "acme/widgets"

    def test_falls_back_to_settings(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("REPO_URL", raising=False)
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "fallback/repo")
        assert current_repo_slug() == "fallback/repo"


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------


class TestRunCommand:
    @pytest.mark.asyncio
    async def test_captures_output(self):
        result = await run_command("python", "-c", "print('hello')")
        assert result["success"] is True
        assert "hello" in result["stdout"]

    @pytest.mark.asyncio
    async def test_times_out_and_fails_closed(self):
        result = await run_command("python", "-c", "import time; time.sleep(30)", timeout_seconds=1.0)
        assert result["success"] is False
        assert result["timed_out"] is True
        assert "timed out" in result["error"]

    @pytest.mark.asyncio
    async def test_decodes_invalid_utf8_without_raising(self):
        result = await run_command(
            "python",
            "-c",
            "import sys; sys.stdout.buffer.write(b'caf\\xe9\\n')",
        )
        assert result["success"] is True
        assert "caf" in result["stdout"]

    @pytest.mark.asyncio
    async def test_missing_binary_returns_error(self):
        result = await run_command("henchmen-not-a-real-binary-xyz")
        assert result["success"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# _run_git against a real repository
# ---------------------------------------------------------------------------


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="git missing")
class TestGitCommitAgainstRealRepo:
    @pytest.mark.asyncio
    async def test_commit_runs_in_the_clone_not_the_root(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        """Regression: git_commit used to run in the workspace root, which has no .git."""
        clone = workspace / "task-123"
        clone.mkdir()
        for args in (
            ["git", "init"],
            ["git", "checkout", "-b", "henchmen/task-123"],
            ["git", "config", "user.email", "dev@example.com"],
            ["git", "config", "user.name", "Dev"],
        ):
            subprocess.run(args, cwd=clone, capture_output=True)
        (clone / "a.txt").write_text("hello\n", encoding="utf-8")
        monkeypatch.chdir(clone)

        result = await git_ops.git_commit(message="feat: add a.txt", files=["a.txt"])

        assert result["success"] is True, result
        log = subprocess.run(["git", "log", "--oneline"], cwd=clone, capture_output=True, text=True)
        assert "feat: add a.txt" in log.stdout


def test_module_exposes_no_mcp_server():
    """ArsenalServer was deleted; Arsenal runs in-process inside the Operative."""
    import henchmen.arsenal as arsenal_pkg

    assert not hasattr(arsenal_pkg, "ArsenalServer")
    assert _workspace.ensure_in_workspace is ensure_in_workspace
