"""Unit tests for Arsenal code_edit, code_intel, test_runner and integration tools.

Each test pins a behaviour that once failed silently: a tool that reported
success while corrupting a file, searched nothing, ran the wrong toolchain, or
read outside the workspace.
"""

import inspect
import shutil
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from henchmen.arsenal._workspace import set_workspace_root
from henchmen.arsenal.registry import ToolRegistry
from henchmen.arsenal.tools import code_edit, code_intel, jira, slack, test_runner

_HAS_GREP = shutil.which("grep") is not None


@pytest.fixture
def workspace(tmp_path: Path):
    """Point the Arsenal workspace boundary at a temporary directory."""
    root = tmp_path / "workspace"
    root.mkdir()
    set_workspace_root(root)
    yield root
    set_workspace_root(None)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A file that lives next to, but outside, the workspace."""
    target = tmp_path / "secret.txt"
    target.write_text("top secret\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Registry contract
# ---------------------------------------------------------------------------


class TestRegistryHandlersAreAsync:
    def test_every_registered_handler_is_a_coroutine_function(self):
        """The agent loop awaits every handler; a sync handler fails every call."""
        import importlib

        import henchmen.arsenal.tools as tools_pkg

        # Other tests register throwaway sync handlers; start from the real set.
        ToolRegistry.clear()
        for module_name in tools_pkg.__all__:
            importlib.reload(importlib.import_module(f"henchmen.arsenal.tools.{module_name}"))
        names = ToolRegistry.list_tools()
        assert names, "no tools registered"
        for name in names:
            tool_def = ToolRegistry.get_tool(name)
            assert tool_def is not None
            assert inspect.iscoroutinefunction(tool_def.handler), f"{name} handler must be async"


# ---------------------------------------------------------------------------
# code_edit
# ---------------------------------------------------------------------------


class TestFileEdit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("old_text", ["", "   ", "\n\n"])
    async def test_empty_old_text_rejected_and_file_untouched(self, workspace: Path, old_text: str):
        target = workspace / "app.py"
        target.write_text("print('hi')\n", encoding="utf-8")

        result = await code_edit.file_edit(str(target), old_text, "INJECTED\n")

        assert "error" in result
        assert target.read_text(encoding="utf-8") == "print('hi')\n"

    @pytest.mark.asyncio
    async def test_exact_replacement(self, workspace: Path):
        target = workspace / "app.py"
        target.write_text("a = 1\nb = 2\n", encoding="utf-8")

        result = await code_edit.file_edit(str(target), "b = 2", "b = 3")

        assert result["success"] is True
        assert target.read_text(encoding="utf-8") == "a = 1\nb = 3\n"

    @pytest.mark.asyncio
    async def test_normalized_match_only_rewrites_matched_span(self, workspace: Path):
        target = workspace / "doc.py"
        original = "x = 'a — b'\ndef f():   \n    return 1\n"
        target.write_text(original, encoding="utf-8")

        result = await code_edit.file_edit(str(target), "def f():\n    return 1", "def f():\n    return 2")

        assert result["success"] is True
        # The em dash on the unrelated first line is preserved.
        assert target.read_text(encoding="utf-8") == "x = 'a — b'\ndef f():\n    return 2\n"

    @pytest.mark.asyncio
    async def test_outside_workspace_denied(self, workspace: Path, outside: Path):
        result = await code_edit.file_edit(str(outside), "top", "bottom")
        assert "access denied" in result["error"]
        assert outside.read_text(encoding="utf-8") == "top secret\n"


class TestFileInsertAtLine:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("line_number", [0, -3, 5])
    async def test_out_of_range_rejected(self, workspace: Path, line_number: int):
        target = workspace / "f.txt"
        target.write_text("one\ntwo\nthree\n", encoding="utf-8")

        result = await code_edit.file_insert_at_line(str(target), line_number, "new")

        assert "out of range 1..4" in result["error"]
        assert target.read_text(encoding="utf-8") == "one\ntwo\nthree\n"

    @pytest.mark.asyncio
    async def test_append_after_last_line_allowed(self, workspace: Path):
        target = workspace / "f.txt"
        target.write_text("one\ntwo\n", encoding="utf-8")

        result = await code_edit.file_insert_at_line(str(target), 3, "three")

        assert result == {"path": str(target), "success": True, "inserted_at_line": 3}
        assert target.read_text(encoding="utf-8") == "one\ntwo\nthree\n"


# ---------------------------------------------------------------------------
# code_intel
# ---------------------------------------------------------------------------


class TestReadToolsEnforceWorkspace:
    @pytest.mark.asyncio
    async def test_file_read_outside_denied(self, workspace: Path, outside: Path):
        result = await code_intel.file_read(str(outside))
        assert "access denied" in result["error"]
        assert "content" not in result

    @pytest.mark.asyncio
    async def test_file_read_traversal_denied(self, workspace: Path, outside: Path):
        result = await code_intel.file_read("../secret.txt")
        assert "access denied" in result["error"]

    @pytest.mark.asyncio
    async def test_file_search_outside_denied(self, workspace: Path, tmp_path: Path):
        result = await code_intel.file_search("*.txt", directory=str(tmp_path))
        assert "access denied" in result["error"]

    @pytest.mark.asyncio
    async def test_ast_analysis_outside_denied(self, workspace: Path, outside: Path):
        result = await code_intel.ast_analysis(str(outside))
        assert "access denied" in result["error"]

    @pytest.mark.asyncio
    async def test_grep_search_outside_denied(self, workspace: Path, tmp_path: Path):
        result = await code_intel.grep_search("secret", directory=str(tmp_path))
        assert "access denied" in result["error"]


class TestFileSearch:
    @pytest.mark.asyncio
    async def test_skips_dependency_and_build_directories(self, workspace: Path):
        (workspace / "src").mkdir()
        (workspace / "src" / "index.js").write_text("", encoding="utf-8")
        for skipped in ("node_modules/pkg", ".git/objects", "dist"):
            (workspace / skipped).mkdir(parents=True)
            (workspace / skipped / "bundle.js").write_text("", encoding="utf-8")

        result = await code_intel.file_search("*.js", directory=str(workspace))

        assert result["count"] == 1
        assert result["matches"][0].endswith("index.js")
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_results_are_capped(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(code_intel, "_MAX_SEARCH_RESULTS", 3)
        for i in range(10):
            (workspace / f"f{i}.py").write_text("", encoding="utf-8")

        result = await code_intel.file_search("*.py", directory=str(workspace))

        assert result["count"] == 3
        assert result["truncated"] is True


@pytest.mark.skipif(not _HAS_GREP, reason="grep is not installed")
class TestGrepSearch:
    @pytest.mark.asyncio
    async def test_dash_prefixed_pattern_is_searched_not_parsed_as_option(self, workspace: Path):
        (workspace / "cli.py").write_text("parser.add_argument('--dry-run')\n", encoding="utf-8")

        result = await code_intel.grep_search("--dry-run", directory=str(workspace), context_lines=0)

        assert "error" not in result
        assert result["return_code"] == 0
        assert "--dry-run" in result["output"]

    @pytest.mark.asyncio
    async def test_invalid_regex_surfaces_an_error(self, workspace: Path):
        (workspace / "a.py").write_text("x\n", encoding="utf-8")

        result = await code_intel.grep_search("[unclosed", directory=str(workspace))

        assert result["return_code"] >= 2
        assert result["error"]


@pytest.mark.skipif(not _HAS_GREP, reason="grep is not installed")
class TestSymbolLookup:
    @pytest.mark.asyncio
    async def test_finds_typescript_definitions(self, workspace: Path):
        (workspace / "api.ts").write_text(
            "export function handleLogin(req) {}\nexport const handleLoginV2 = () => {}\n", encoding="utf-8"
        )

        result = await code_intel.symbol_lookup("handleLogin", directory=str(workspace))

        assert result["count"] == 1
        assert "function handleLogin" in result["matches"][0]["text"]

    @pytest.mark.asyncio
    async def test_finds_python_definitions(self, workspace: Path):
        (workspace / "m.py").write_text("class Widget:\n    pass\n", encoding="utf-8")

        result = await code_intel.symbol_lookup("Widget", directory=str(workspace))

        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_non_identifier_symbol_rejected(self, workspace: Path):
        result = await code_intel.symbol_lookup("--help", directory=str(workspace))
        assert "plain identifier" in result["error"]


# ---------------------------------------------------------------------------
# test_runner
# ---------------------------------------------------------------------------


class TestTestRunnerProjectDetection:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ["run_tests", "run_lint", "type_check"])
    async def test_unknown_project_fails_closed_without_running_anything(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, tool_name: str
    ):
        (workspace / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
        runner = AsyncMock()
        monkeypatch.setattr(test_runner, "run_command", runner)

        result = await getattr(test_runner, tool_name)(working_dir=str(workspace))

        assert result["success"] is False
        assert result["project_type"] == "unknown"
        assert "unsupported project type" in result["error"]
        runner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_working_dir_outside_workspace_denied(self, workspace: Path, tmp_path: Path):
        result = await test_runner.run_tests(working_dir=str(tmp_path))
        assert "access denied" in result["error"]

    @pytest.mark.asyncio
    async def test_python_tests_run_with_ci_env(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        (workspace / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        runner = AsyncMock(return_value={"stdout": "", "stderr": "", "return_code": 0, "success": True})
        monkeypatch.setattr(test_runner, "run_command", runner)

        result = await test_runner.run_tests(working_dir=str(workspace))

        assert result["project_type"] == "python"
        args = runner.await_args.args
        assert args[:3] == ("python", "-m", "pytest")
        assert runner.await_args.kwargs["env"]["CI"] == "1"

    @pytest.mark.asyncio
    async def test_typescript_type_check_honours_path(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        (workspace / "package.json").write_text("{}", encoding="utf-8")
        runner = AsyncMock(return_value={"stdout": "", "stderr": "", "return_code": 0, "success": True})
        monkeypatch.setattr(test_runner, "run_command", runner)

        result = await test_runner.type_check(path="packages/api", working_dir=str(workspace))

        assert result["command"] == "npx tsc --noEmit -p packages/api"


class TestAffectedPackages:
    @pytest.mark.asyncio
    async def test_diffs_against_detected_default_branch(self, monkeypatch: pytest.MonkeyPatch):
        import henchmen.operative.git_helpers as git_helpers

        monkeypatch.setattr(git_helpers, "detect_base_ref", AsyncMock(return_value="origin/master"))
        runner = AsyncMock(
            return_value={
                "stdout": "apps/api/src/a.ts\npackages/ui/b.tsx\n",
                "stderr": "",
                "return_code": 0,
                "success": True,
            }
        )
        monkeypatch.setattr(test_runner, "run_command", runner)

        packages = await test_runner._get_affected_packages("/repo")

        assert runner.await_args.args == ("git", "diff", "--name-only", "origin/master")
        assert packages == ["./apps/api", "./packages/ui"]


# ---------------------------------------------------------------------------
# Integration tools (slack, jira)
# ---------------------------------------------------------------------------


class TestSlackUploadFile:
    @pytest.mark.asyncio
    async def test_passes_single_channel_not_channels_list(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        target = workspace / "report.txt"
        target.write_text("ok\n", encoding="utf-8")
        client = MagicMock()
        client.files_upload_v2.return_value = {"file": {"id": "F1", "name": "report.txt", "permalink": "p"}}
        monkeypatch.setattr(slack, "_get_slack_client", lambda: client)

        result = await slack.upload_file("C0123", str(target), title="Report")

        assert result["success"] is True
        kwargs = client.files_upload_v2.call_args.kwargs
        assert kwargs["channel"] == "C0123"
        assert "channels" not in kwargs
        assert kwargs["file"] == str(target.resolve())

    @pytest.mark.asyncio
    async def test_file_outside_workspace_denied(self, workspace: Path, outside: Path, monkeypatch: pytest.MonkeyPatch):
        client = MagicMock()
        monkeypatch.setattr(slack, "_get_slack_client", lambda: client)

        result = await slack.upload_file("C0123", str(outside))

        assert "access denied" in result["error"]
        client.files_upload_v2.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_token_is_a_clear_error(self, monkeypatch: pytest.MonkeyPatch):
        fake_sdk = ModuleType("slack_sdk")
        fake_sdk.WebClient = MagicMock()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "slack_sdk", fake_sdk)

        result = await slack.post_message("C0123", "hello")

        assert "No Slack bot token configured" in result["error"]


class TestJiraTransition:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("wanted", ["Start Progress", "in progress"])
    async def test_matches_transition_or_destination_status(self, monkeypatch: pytest.MonkeyPatch, wanted: str):
        client = MagicMock()
        client.transitions.return_value = [
            {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
            {"id": "21", "name": "Start Progress", "to": {"name": "In Progress"}},
        ]
        monkeypatch.setattr(jira, "_get_jira_client", lambda: client)

        result = await jira.transition_issue("HEN-1", wanted)

        assert result["success"] is True
        client.transition_issue.assert_called_once_with("HEN-1", "21")

    @pytest.mark.asyncio
    async def test_unknown_transition_lists_available(self, monkeypatch: pytest.MonkeyPatch):
        client = MagicMock()
        client.transitions.return_value = [{"id": "11", "name": "To Do", "to": {"name": "To Do"}}]
        monkeypatch.setattr(jira, "_get_jira_client", lambda: client)

        result = await jira.transition_issue("HEN-1", "Done")

        assert "not found" in result["error"]
        client.transition_issue.assert_not_called()
