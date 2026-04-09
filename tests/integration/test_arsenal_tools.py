"""Integration tests for Arsenal tool execution.

These tests exercise real tool handler functions against actual files and git
repositories on disk using the test_workspace fixture.
"""

import os
from pathlib import Path

import pytest

from henchmen.arsenal.registry import ToolRegistry
from henchmen.arsenal.server import ArsenalServer
from henchmen.arsenal.tools.code_edit import (
    file_create,
    file_delete,
    file_edit,
    file_write,
)
from henchmen.arsenal.tools.code_intel import (
    ast_analysis,
    file_read,
    file_search,
    grep_search,
    symbol_lookup,
)
from henchmen.arsenal.tools.git_ops import (
    git_branch_create,
    git_commit,
    git_diff,
    git_log,
    git_status,
)
from henchmen.arsenal.tools.test_runner import run_lint, run_tests
from henchmen.models.scheme import ArsenalRequirement

# ---------------------------------------------------------------------------
# TestCodeIntelTools
# ---------------------------------------------------------------------------


class TestCodeIntelTools:
    @pytest.mark.asyncio
    async def test_file_read_returns_content(self, test_workspace: Path):
        auth_py = str(test_workspace / "src" / "auth.py")
        result = await file_read(auth_py)

        assert "error" not in result
        assert result["path"] == auth_py
        assert "def login" in result["content"]
        assert result["total_lines"] > 0

    @pytest.mark.asyncio
    async def test_file_read_with_line_range(self, test_workspace: Path):
        auth_py = str(test_workspace / "src" / "auth.py")
        # Read lines 1-3 (0-indexed: lines at index 1 through 3 exclusive)
        result = await file_read(auth_py, start_line=1, end_line=3)

        assert "error" not in result
        assert result["start_line"] == 1
        assert result["end_line"] == 3
        # Content should only be 2 lines (indices 1 and 2)
        lines = result["content"].splitlines()
        assert len(lines) <= 2

    @pytest.mark.asyncio
    async def test_file_search_finds_matching_files(self, test_workspace: Path):
        result = await file_search(pattern="*.py", directory=str(test_workspace))

        assert "error" not in result
        matches = result["matches"]
        basenames = [os.path.basename(m) for m in matches]
        assert "auth.py" in basenames
        assert "test_auth.py" in basenames

    @pytest.mark.asyncio
    async def test_grep_search_finds_pattern(self, test_workspace: Path):
        result = await grep_search(
            pattern="def login",
            directory=str(test_workspace),
            file_glob="*.py",
        )

        assert "error" not in result
        assert result["return_code"] == 0
        assert "def login" in result["output"]
        assert "auth.py" in result["output"]

    @pytest.mark.asyncio
    async def test_symbol_lookup_finds_function(self, test_workspace: Path):
        result = await symbol_lookup(symbol="login", directory=str(test_workspace))

        assert "error" not in result
        assert result["count"] >= 1
        found_texts = [m.get("text", "") for m in result["matches"]]
        assert any("def login" in t for t in found_texts)

    @pytest.mark.asyncio
    async def test_ast_analysis_lists_functions(self, test_workspace: Path):
        auth_py = str(test_workspace / "src" / "auth.py")
        result = await ast_analysis(path=auth_py)

        assert "error" not in result
        assert result["path"] == auth_py
        function_names = [f["name"] for f in result["functions"]]
        assert "login" in function_names


# ---------------------------------------------------------------------------
# TestCodeEditTools
# ---------------------------------------------------------------------------


class TestCodeEditTools:
    @pytest.mark.asyncio
    async def test_file_write_creates_content(self, test_workspace: Path):
        new_file = str(test_workspace / "src" / "utils.py")
        content = "def helper():\n    return True\n"

        result = await file_write(new_file, content)

        assert "error" not in result
        assert result["success"] is True
        assert Path(new_file).exists()
        assert Path(new_file).read_text(encoding="utf-8") == content

    @pytest.mark.asyncio
    async def test_file_edit_replaces_text(self, test_workspace: Path):
        auth_py = str(test_workspace / "src" / "auth.py")
        old_text = '{"token": "abc123", "user": username}'
        new_text = '{"token": "xyz789", "user": username}'

        result = await file_edit(auth_py, old_text, new_text)

        assert "error" not in result
        assert result["success"] is True
        updated = Path(auth_py).read_text(encoding="utf-8")
        assert "xyz789" in updated
        assert "abc123" not in updated

    @pytest.mark.asyncio
    async def test_file_create_new_file(self, test_workspace: Path):
        new_file = str(test_workspace / "src" / "newmodule.py")
        content = "# new module\n"

        result = await file_create(new_file, content)

        assert "error" not in result
        assert result["success"] is True
        assert Path(new_file).exists()
        assert Path(new_file).read_text(encoding="utf-8") == content

    @pytest.mark.asyncio
    async def test_file_create_fails_if_exists(self, test_workspace: Path):
        auth_py = str(test_workspace / "src" / "auth.py")

        result = await file_create(auth_py, "# should fail")

        assert "error" in result
        assert "already exists" in result["error"]

    @pytest.mark.asyncio
    async def test_file_delete_removes_file(self, test_workspace: Path):
        temp_file = test_workspace / "src" / "temp_to_delete.py"
        temp_file.write_text("# temporary\n", encoding="utf-8")
        assert temp_file.exists()

        result = await file_delete(str(temp_file))

        assert "error" not in result
        assert result["success"] is True
        assert not temp_file.exists()


# ---------------------------------------------------------------------------
# TestGitOpsTools
# ---------------------------------------------------------------------------


class TestGitOpsTools:
    """Git tools use the process cwd for git commands, so we chdir to the workspace."""

    @pytest.fixture(autouse=True)
    def chdir_to_workspace(self, test_workspace: Path, monkeypatch):
        """Change cwd to the test workspace for every git test."""
        monkeypatch.chdir(test_workspace)

    @pytest.mark.asyncio
    async def test_git_status_shows_clean_workspace(self, test_workspace: Path):
        result = await git_status()

        assert result["success"] is True
        # A clean repo has no short-format lines
        assert result["stdout"].strip() == ""

    @pytest.mark.asyncio
    async def test_git_branch_create(self, test_workspace: Path):
        result = await git_branch_create(branch_name="feature/test")

        assert result["success"] is True
        assert result["branch_name"] == "feature/test"
        # Verify the branch was actually created and checked out
        import subprocess

        log = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=test_workspace,
            capture_output=True,
            text=True,
        )
        assert log.stdout.strip() == "feature/test"

    @pytest.mark.asyncio
    async def test_git_commit_stages_and_commits(self, test_workspace: Path):
        # Modify a file so there's something to commit
        auth_py = test_workspace / "src" / "auth.py"
        original = auth_py.read_text(encoding="utf-8")
        auth_py.write_text(original + "\n# integration test change\n", encoding="utf-8")

        result = await git_commit(
            message="test commit",
            files=["src/auth.py"],
        )

        assert result["success"] is True
        # Verify commit appears in git log
        log_result = await git_log(max_count=5)
        assert "test commit" in log_result["stdout"]

    @pytest.mark.asyncio
    async def test_git_diff_shows_changes(self, test_workspace: Path):
        # Modify a file without staging it
        auth_py = test_workspace / "src" / "auth.py"
        auth_py.write_text(
            auth_py.read_text(encoding="utf-8") + "\n# diff test change\n",
            encoding="utf-8",
        )

        result = await git_diff()

        assert result["success"] is True
        assert "diff test change" in result["stdout"]

    @pytest.mark.asyncio
    async def test_git_log_shows_history(self, test_workspace: Path):
        result = await git_log(max_count=5)

        assert result["success"] is True
        assert "Initial commit" in result["stdout"]

    @pytest.mark.asyncio
    async def test_git_status_shows_modified_files(self, test_workspace: Path):
        # Modify a tracked file
        auth_py = test_workspace / "src" / "auth.py"
        auth_py.write_text(
            auth_py.read_text(encoding="utf-8") + "\n# status test change\n",
            encoding="utf-8",
        )

        result = await git_status()

        assert result["success"] is True
        # Modified files appear in short status output
        assert "auth.py" in result["stdout"]


# ---------------------------------------------------------------------------
# TestTestRunnerTools
# ---------------------------------------------------------------------------


class TestTestRunnerTools:
    @pytest.mark.asyncio
    async def test_run_lint_on_clean_code(self, test_workspace: Path):
        result = await run_lint(path=str(test_workspace))

        # ruff may or may not find issues, but it must execute without crashing
        assert "error" not in result
        assert "command" in result
        assert "stdout" in result
        assert "return_code" in result

    @pytest.mark.asyncio
    async def test_run_tests_executes_pytest(self, test_workspace: Path):
        test_dir = str(test_workspace / "tests")
        result = await run_tests(test_path=test_dir)

        # pytest must execute; it may fail due to import path issues in the
        # temporary workspace, but it should not crash the tool itself
        assert "error" not in result
        assert "command" in result
        assert "stdout" in result
        assert "return_code" in result
        # pytest output always contains "passed" or "failed" or "error" or "no tests"
        combined = result["stdout"] + result["stderr"]
        assert any(keyword in combined for keyword in ("passed", "failed", "error", "no tests ran", "collected"))


# ---------------------------------------------------------------------------
# TestToolRegistryIntegration
# ---------------------------------------------------------------------------


class TestToolRegistryIntegration:
    @pytest.fixture(autouse=True)
    def reload_tool_modules(self):
        """Clear the registry, then re-import all tool modules to re-register."""
        ToolRegistry.clear()
        # Re-importing the modules triggers the @tool decorators
        import importlib

        import henchmen.arsenal.tools.code_edit as ce_mod
        import henchmen.arsenal.tools.code_intel as ci_mod
        import henchmen.arsenal.tools.git_ops as go_mod
        import henchmen.arsenal.tools.github as gh_mod
        import henchmen.arsenal.tools.jira as ji_mod
        import henchmen.arsenal.tools.slack as sl_mod
        import henchmen.arsenal.tools.test_runner as tr_mod

        for mod in (ci_mod, ce_mod, go_mod, tr_mod, gh_mod, sl_mod, ji_mod):
            importlib.reload(mod)

        yield

        # Clean up after each test
        ToolRegistry.clear()

    def test_code_intel_requirement_returns_only_code_intel_tools(self):
        req = ArsenalRequirement(tool_sets=["code_intel"])
        tools = ToolRegistry.get_tools_for_requirement(req)

        tool_names = {t.name for t in tools}
        assert tool_names  # non-empty
        for t in tools:
            assert t.category == "code_intel", f"Tool {t.name!r} has category {t.category!r}, expected 'code_intel'"
        assert "file_read" in tool_names
        assert "file_search" in tool_names
        assert "grep_search" in tool_names
        assert "symbol_lookup" in tool_names
        assert "ast_analysis" in tool_names

    def test_multiple_categories_combined(self):
        req = ArsenalRequirement(tool_sets=["code_intel", "code_edit"])
        tools = ToolRegistry.get_tools_for_requirement(req)

        categories = {t.category for t in tools}
        assert "code_intel" in categories
        assert "code_edit" in categories
        # Must not include unrelated categories
        assert "git_ops" not in categories

    def test_destructive_tools_excluded_by_default(self):
        req = ArsenalRequirement(tool_sets=["code_edit"], allow_destructive=False)
        tools = ToolRegistry.get_tools_for_requirement(req)

        tool_names = {t.name for t in tools}
        assert "file_delete" not in tool_names
        # Non-destructive edit tools should still be present
        assert "file_write" in tool_names
        assert "file_edit" in tool_names
        assert "file_create" in tool_names

    def test_destructive_tools_included_when_allowed(self):
        req = ArsenalRequirement(tool_sets=["code_edit"], allow_destructive=True)
        tools = ToolRegistry.get_tools_for_requirement(req)

        tool_names = {t.name for t in tools}
        assert "file_delete" in tool_names

    def test_arsenal_server_registers_filtered_tools(self):
        req = ArsenalRequirement(tool_sets=["code_intel"], allow_destructive=False)
        server = ArsenalServer(requirement=req)

        # The MCP server should have exactly the code_intel tools registered.
        # FastMCP stores tools in _tool_manager._tools (dict keyed by name).
        registered = set(server.mcp._tool_manager._tools.keys())
        assert registered  # non-empty
        # All registered tools must be code_intel tools
        expected_tool_names = {t.name for t in ToolRegistry.get_tools_for_requirement(req)}
        assert registered == expected_tool_names
