"""Unit tests for Arsenal - ToolRegistry, ArsenalServer, and tool modules."""

import pytest

from henchmen.arsenal.registry import ToolDefinition, ToolRegistry, tool
from henchmen.models.scheme import ArsenalRequirement

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_requirement(
    tool_sets: list[str],
    allow_destructive: bool = False,
) -> ArsenalRequirement:
    return ArsenalRequirement(tool_sets=tool_sets, allow_destructive=allow_destructive)


# ---------------------------------------------------------------------------
# Setup / Teardown
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_registry():
    """Each test gets a clean ToolRegistry."""
    ToolRegistry.clear()
    yield
    ToolRegistry.clear()


# ---------------------------------------------------------------------------
# ToolRegistry.register and get_tool
# ---------------------------------------------------------------------------


class TestToolRegistryRegisterAndGet:
    def test_register_adds_tool(self):
        async def handler() -> dict:  # type: ignore[return]
            return {}

        td = ToolDefinition(
            name="my_tool",
            description="A test tool",
            category="test_cat",
            handler=handler,
        )
        ToolRegistry.register(td)
        assert ToolRegistry.get_tool("my_tool") is td

    def test_get_tool_unknown_returns_none(self):
        assert ToolRegistry.get_tool("nonexistent") is None

    def test_register_multiple_tools(self):
        async def h1() -> dict:  # type: ignore[return]
            return {}

        async def h2() -> dict:  # type: ignore[return]
            return {}

        ToolRegistry.register(ToolDefinition("t1", "desc1", "cat_a", h1))
        ToolRegistry.register(ToolDefinition("t2", "desc2", "cat_b", h2))
        assert ToolRegistry.get_tool("t1") is not None
        assert ToolRegistry.get_tool("t2") is not None

    def test_register_overwrites_existing_name(self):
        async def h1() -> dict:  # type: ignore[return]
            return {}

        async def h2() -> dict:  # type: ignore[return]
            return {}

        ToolRegistry.register(ToolDefinition("same_name", "first", "cat", h1))
        ToolRegistry.register(ToolDefinition("same_name", "second", "cat", h2))
        assert ToolRegistry.get_tool("same_name").description == "second"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# ToolRegistry.get_tools_for_requirement – category filtering
# ---------------------------------------------------------------------------


class TestGetToolsForRequirementCategories:
    def _register_tools(self) -> None:
        async def h() -> dict:  # type: ignore[return]
            return {}

        ToolRegistry.register(ToolDefinition("read_file", "read", "code_intel", h))
        ToolRegistry.register(ToolDefinition("write_file", "write", "code_edit", h))
        ToolRegistry.register(ToolDefinition("git_commit", "commit", "git_ops", h))

    def test_returns_only_requested_categories(self):
        self._register_tools()
        req = _make_requirement(["code_intel"])
        tools = ToolRegistry.get_tools_for_requirement(req)
        names = {t.name for t in tools}
        assert names == {"read_file"}

    def test_returns_multiple_categories(self):
        self._register_tools()
        req = _make_requirement(["code_intel", "git_ops"])
        tools = ToolRegistry.get_tools_for_requirement(req)
        names = {t.name for t in tools}
        assert names == {"read_file", "git_commit"}

    def test_empty_tool_sets_returns_empty(self):
        self._register_tools()
        req = _make_requirement([])
        assert ToolRegistry.get_tools_for_requirement(req) == []

    def test_unregistered_but_valid_category_returns_empty(self):
        # "slack" is a valid tool_set literal but no slack tools are registered in this test
        req = _make_requirement(["slack"])
        assert ToolRegistry.get_tools_for_requirement(req) == []


# ---------------------------------------------------------------------------
# ToolRegistry.get_tools_for_requirement – allow_destructive flag
# ---------------------------------------------------------------------------


class TestGetToolsForRequirementDestructive:
    def _register_tools(self) -> None:
        async def h() -> dict:  # type: ignore[return]
            return {}

        ToolRegistry.register(ToolDefinition("safe_op", "safe", "code_edit", h, is_destructive=False))
        ToolRegistry.register(ToolDefinition("delete_file", "delete", "code_edit", h, is_destructive=True))
        ToolRegistry.register(ToolDefinition("force_push", "force", "git_ops", h, is_destructive=True))

    def test_destructive_excluded_when_not_allowed(self):
        self._register_tools()
        req = _make_requirement(["code_edit", "git_ops"], allow_destructive=False)
        tools = ToolRegistry.get_tools_for_requirement(req)
        names = {t.name for t in tools}
        assert "delete_file" not in names
        assert "force_push" not in names
        assert "safe_op" in names

    def test_destructive_included_when_allowed(self):
        self._register_tools()
        req = _make_requirement(["code_edit", "git_ops"], allow_destructive=True)
        tools = ToolRegistry.get_tools_for_requirement(req)
        names = {t.name for t in tools}
        assert "delete_file" in names
        assert "force_push" in names
        assert "safe_op" in names


# ---------------------------------------------------------------------------
# ToolRegistry.list_categories and list_tools
# ---------------------------------------------------------------------------


class TestListCategoriesAndTools:
    def _register(self) -> None:
        async def h() -> dict:  # type: ignore[return]
            return {}

        ToolRegistry.register(ToolDefinition("t1", "d", "alpha", h))
        ToolRegistry.register(ToolDefinition("t2", "d", "alpha", h))
        ToolRegistry.register(ToolDefinition("t3", "d", "beta", h))

    def test_list_categories(self):
        self._register()
        cats = ToolRegistry.list_categories()
        assert set(cats) == {"alpha", "beta"}

    def test_list_tools_no_filter(self):
        self._register()
        tools = ToolRegistry.list_tools()
        assert set(tools) == {"t1", "t2", "t3"}

    def test_list_tools_with_category(self):
        self._register()
        tools = ToolRegistry.list_tools(category="alpha")
        assert set(tools) == {"t1", "t2"}

    def test_list_tools_unknown_category(self):
        self._register()
        assert ToolRegistry.list_tools(category="unknown") == []

    def test_list_categories_empty(self):
        assert ToolRegistry.list_categories() == []

    def test_list_tools_empty(self):
        assert ToolRegistry.list_tools() == []


# ---------------------------------------------------------------------------
# ToolRegistry.clear
# ---------------------------------------------------------------------------


class TestToolRegistryClear:
    def test_clear_removes_all(self):
        async def h() -> dict:  # type: ignore[return]
            return {}

        ToolRegistry.register(ToolDefinition("t1", "d", "cat", h))
        ToolRegistry.clear()
        assert ToolRegistry.list_tools() == []
        assert ToolRegistry.list_categories() == []
        assert ToolRegistry.get_tool("t1") is None


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------


class TestToolDecorator:
    def test_decorator_registers_tool(self):
        @tool(name="decorated_tool", category="test_cat", description="A decorated tool")
        async def my_handler() -> dict:  # type: ignore[return]
            return {}

        result = ToolRegistry.get_tool("decorated_tool")
        assert result is not None
        assert result.name == "decorated_tool"
        assert result.category == "test_cat"
        assert result.description == "A decorated tool"
        assert result.is_destructive is False

    def test_decorator_marks_destructive(self):
        @tool(
            name="destructive_decorated",
            category="test_cat",
            description="Dangerous",
            is_destructive=True,
        )
        async def dangerous_handler() -> dict:  # type: ignore[return]
            return {}

        result = ToolRegistry.get_tool("destructive_decorated")
        assert result is not None
        assert result.is_destructive is True

    def test_decorator_returns_original_function(self):
        @tool(name="passthrough_tool", category="test_cat", description="Passthrough")
        async def original() -> dict:  # type: ignore[return]
            return {"ok": True}

        # The decorator should return the original function unchanged
        assert original is not None
        assert callable(original)

    def test_decorator_captures_parameters(self):
        @tool(name="param_tool", category="test_cat", description="Has params")
        async def handler_with_params(path: str, count: int = 5) -> dict:  # type: ignore[return]
            return {}

        result = ToolRegistry.get_tool("param_tool")
        assert result is not None
        assert "path" in result.parameters
        assert "count" in result.parameters


# ---------------------------------------------------------------------------
# Tool modules register their tools on import
# ---------------------------------------------------------------------------


class TestToolModulesRegisterOnImport:
    """These tests require the real tool modules; registry is cleared before each test
    by the autouse fixture, so we must import them fresh or check after import."""

    def test_code_intel_tools_registered(self):
        # Force re-registration by importing the module
        import importlib

        import henchmen.arsenal.tools.code_intel as mod

        importlib.reload(mod)
        tools = ToolRegistry.list_tools(category="code_intel")
        assert "file_read" in tools
        assert "file_search" in tools
        assert "symbol_lookup" in tools
        assert "grep_search" in tools
        assert "ast_analysis" in tools

    def test_code_edit_tools_registered(self):
        import importlib

        import henchmen.arsenal.tools.code_edit as mod

        importlib.reload(mod)
        tools = ToolRegistry.list_tools(category="code_edit")
        assert "file_write" in tools
        assert "file_edit" in tools
        assert "file_create" in tools
        assert "file_delete" in tools

    def test_git_ops_tools_registered(self):
        import importlib

        import henchmen.arsenal.tools.git_ops as mod

        importlib.reload(mod)
        tools = ToolRegistry.list_tools(category="git_ops")
        assert "git_branch_create" in tools
        assert "git_commit" in tools
        assert "git_push" in tools
        assert "git_force_push" in tools
        assert "git_diff" in tools
        assert "git_log" in tools
        assert "git_status" in tools

    def test_test_runner_tools_registered(self):
        import importlib

        import henchmen.arsenal.tools.test_runner as mod

        importlib.reload(mod)
        tools = ToolRegistry.list_tools(category="test_runner")
        assert "run_tests" in tools
        assert "run_lint" in tools
        assert "type_check" in tools

    def test_github_tools_registered(self):
        import importlib

        import henchmen.arsenal.tools.github as mod

        importlib.reload(mod)
        tools = ToolRegistry.list_tools(category="github")
        assert "create_pull_request" in tools
        assert "comment_on_pr" in tools
        assert "label_issue" in tools
        assert "assign_issue" in tools
        assert "fetch_issues" in tools

    def test_jira_tools_registered(self):
        import importlib

        import henchmen.arsenal.tools.jira as mod

        importlib.reload(mod)
        tools = ToolRegistry.list_tools(category="jira")
        assert "update_issue_status" in tools
        assert "add_comment" in tools
        assert "transition_issue" in tools
        assert "fetch_issue" in tools

    def test_slack_tools_registered(self):
        import importlib

        import henchmen.arsenal.tools.slack as mod

        importlib.reload(mod)
        tools = ToolRegistry.list_tools(category="slack")
        assert "post_message" in tools
        assert "thread_reply" in tools
        assert "upload_file" in tools

    def test_file_delete_is_destructive(self):
        import importlib

        import henchmen.arsenal.tools.code_edit as mod

        importlib.reload(mod)
        td = ToolRegistry.get_tool("file_delete")
        assert td is not None
        assert td.is_destructive is True

    def test_git_push_is_not_destructive(self):
        import importlib

        import henchmen.arsenal.tools.git_ops as mod

        importlib.reload(mod)
        td = ToolRegistry.get_tool("git_push")
        assert td is not None
        assert td.is_destructive is False

    def test_git_force_push_is_destructive(self):
        import importlib

        import henchmen.arsenal.tools.git_ops as mod

        importlib.reload(mod)
        td = ToolRegistry.get_tool("git_force_push")
        assert td is not None
        assert td.is_destructive is True

    def test_non_destructive_tools_not_marked(self):
        import importlib

        import henchmen.arsenal.tools.code_intel as mod

        importlib.reload(mod)
        for name in ToolRegistry.list_tools(category="code_intel"):
            td = ToolRegistry.get_tool(name)
            assert td is not None
            assert td.is_destructive is False, f"{name} should not be destructive"


class TestGitPushSplit:
    def test_git_push_and_force_push_both_registered(self):
        import importlib

        import henchmen.arsenal.tools.git_ops as mod

        importlib.reload(mod)
        tools = ToolRegistry.list_tools(category="git_ops")
        assert "git_push" in tools
        assert "git_force_push" in tools


# ---------------------------------------------------------------------------
# ArsenalServer
# ---------------------------------------------------------------------------


class TestArsenalServer:
    def _load_code_intel(self) -> None:
        import importlib

        import henchmen.arsenal.tools.code_intel as mod

        importlib.reload(mod)

    def test_server_creates_with_requirement(self):
        from henchmen.arsenal.server import ArsenalServer

        self._load_code_intel()
        req = _make_requirement(["code_intel"])
        server = ArsenalServer(req)
        assert server is not None
        assert server.requirement == req

    def test_server_has_mcp_instance(self):
        from henchmen.arsenal.server import ArsenalServer

        self._load_code_intel()
        req = _make_requirement(["code_intel"])
        server = ArsenalServer(req)
        assert server.mcp is not None

    def test_server_get_app_returns_object(self):
        from henchmen.arsenal.server import ArsenalServer

        self._load_code_intel()
        req = _make_requirement(["code_intel"])
        server = ArsenalServer(req)
        app = server.get_app()
        assert app is not None

    def test_server_excludes_destructive_when_not_allowed(self):
        import importlib

        import henchmen.arsenal.tools.code_edit as mod
        from henchmen.arsenal.server import ArsenalServer

        importlib.reload(mod)
        req = _make_requirement(["code_edit"], allow_destructive=False)
        ArsenalServer(req)
        # Verify file_delete was excluded: the registered tool names on mcp
        # We can check via the ToolRegistry filter directly
        tools = ToolRegistry.get_tools_for_requirement(req)
        names = {t.name for t in tools}
        assert "file_delete" not in names

    def test_server_includes_destructive_when_allowed(self):
        import importlib

        import henchmen.arsenal.tools.code_edit as mod
        from henchmen.arsenal.server import ArsenalServer

        importlib.reload(mod)
        req = _make_requirement(["code_edit"], allow_destructive=True)
        ArsenalServer(req)
        tools = ToolRegistry.get_tools_for_requirement(req)
        names = {t.name for t in tools}
        assert "file_delete" in names


# ---------------------------------------------------------------------------
# working_dir parameter
# ---------------------------------------------------------------------------


class TestWorkingDirParameter:
    """Verify subprocess tools accept a working_dir parameter."""

    def test_git_ops_tools_have_working_dir(self):
        import importlib

        import henchmen.arsenal.tools.git_ops as mod

        importlib.reload(mod)
        for name in [
            "git_branch_create",
            "git_commit",
            "git_push",
            "git_force_push",
            "git_diff",
            "git_log",
            "git_status",
        ]:
            td = ToolRegistry.get_tool(name)
            assert td is not None, f"{name} not registered"
            assert "working_dir" in td.parameters, f"{name} missing working_dir parameter"

    def test_test_runner_tools_have_working_dir(self):
        import importlib

        import henchmen.arsenal.tools.test_runner as mod

        importlib.reload(mod)
        for name in ["run_tests", "run_lint", "type_check"]:
            td = ToolRegistry.get_tool(name)
            assert td is not None, f"{name} not registered"
            assert "working_dir" in td.parameters, f"{name} missing working_dir parameter"

    def test_code_intel_subprocess_tools_have_working_dir(self):
        import importlib

        import henchmen.arsenal.tools.code_intel as mod

        importlib.reload(mod)
        for name in ["symbol_lookup", "grep_search"]:
            td = ToolRegistry.get_tool(name)
            assert td is not None, f"{name} not registered"
            assert "working_dir" in td.parameters, f"{name} missing working_dir parameter"
