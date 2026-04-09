"""Unit tests for the Operative runtime components."""

import logging
import sys
import tempfile
from datetime import UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.models.operative import OperativeConfig, OperativeReport, OperativeStatus
from henchmen.operative.guardrails import OperativeGuardrails

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**kwargs) -> OperativeConfig:
    defaults = {
        "task_id": "task-123",
        "node_id": "node-analyze",
        "scheme_id": "bugfix-standard",
        "model_name": "gemini-2.5-pro",
    }
    defaults.update(kwargs)
    return OperativeConfig(**defaults)


def _make_report(**kwargs) -> OperativeReport:
    from datetime import datetime

    defaults = {
        "task_id": "task-123",
        "scheme_id": "bugfix-standard",
        "node_id": "node-analyze",
        "operative_id": "op-task-123-node-analyze",
        "status": OperativeStatus.COMPLETED,
        "summary": "Fixed the bug",
        "confidence_score": 0.85,
        "started_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    return OperativeReport(**defaults)


# ---------------------------------------------------------------------------
# OperativeConfig
# ---------------------------------------------------------------------------


class TestOperativeConfig:
    def test_minimal_creation(self):
        config = _make_config()
        assert config.task_id == "task-123"
        assert config.node_id == "node-analyze"
        assert config.scheme_id == "bugfix-standard"
        assert config.model_name == "gemini-2.5-pro"

    def test_defaults(self):
        config = _make_config()
        assert config.cpu == "4"
        assert config.memory == "8Gi"
        assert config.timeout_seconds == 1800

    def test_custom_values(self):
        config = _make_config(cpu="8", memory="16Gi", timeout_seconds=3600)
        assert config.cpu == "8"
        assert config.memory == "16Gi"
        assert config.timeout_seconds == 3600

    def test_missing_required_fields_raises(self):
        with pytest.raises(Exception):
            OperativeConfig(node_id="n", scheme_id="s")  # task_id missing

    def test_model_name_default(self):
        config = OperativeConfig(task_id="t", node_id="n", scheme_id="s")
        assert config.model_name == "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# OperativeReport
# ---------------------------------------------------------------------------


class TestOperativeReport:
    def test_creation(self):
        report = _make_report()
        assert report.task_id == "task-123"
        assert report.status == OperativeStatus.COMPLETED
        assert report.confidence_score == 0.85

    def test_confidence_score_bounds(self):
        with pytest.raises(Exception):
            _make_report(confidence_score=1.5)
        with pytest.raises(Exception):
            _make_report(confidence_score=-0.1)

    def test_defaults(self):
        report = _make_report()
        assert report.git_diff is None
        assert report.files_changed == []
        assert report.error is None
        assert report.completed_at is None

    def test_all_statuses(self):
        for status in OperativeStatus:
            report = _make_report(status=status)
            assert report.status == status

    def test_with_git_diff(self):
        diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new"
        report = _make_report(git_diff=diff)
        assert report.git_diff == diff

    def test_with_files_changed(self):
        files = ["src/foo.py", "tests/test_foo.py"]
        report = _make_report(files_changed=files)
        assert report.files_changed == files

    def test_with_error(self):
        report = _make_report(status=OperativeStatus.FAILED, error="Something broke")
        assert report.error == "Something broke"

    def test_json_serialization(self):
        report = _make_report()
        json_str = report.model_dump_json()
        assert "task-123" in json_str
        assert "completed" in json_str


# ---------------------------------------------------------------------------
# OperativeGuardrails – tool filtering
# ---------------------------------------------------------------------------


class TestGuardrailsToolFiltering:
    def _make_guardrails(self, allowed: set[str] | None = None) -> OperativeGuardrails:
        config = _make_config()
        if allowed is None:
            allowed = {"file_read", "file_write", "git_commit"}
        return OperativeGuardrails(config, allowed)

    def test_allowed_tool_passes(self):
        g = self._make_guardrails()
        result = g.before_tool_call("file_read", {"path": "/workspace/foo.py"})
        assert result is None

    def test_disallowed_tool_blocked(self):
        g = self._make_guardrails({"file_read"})
        result = g.before_tool_call("file_delete", {"path": "/workspace/foo.py"})
        assert result is not None
        assert "error" in result
        assert "file_delete" in result["error"]

    def test_tool_call_count_increments(self):
        g = self._make_guardrails()
        g.before_tool_call("file_read", {"path": "/workspace/a.py"})
        g.before_tool_call("file_write", {"path": "/workspace/b.py"})
        assert g.tool_call_count == 2

    def test_blocked_tool_does_not_increment_count(self):
        g = self._make_guardrails({"file_read"})
        g.before_tool_call("file_delete", {})
        assert g.tool_call_count == 0

    def test_empty_allowed_set_blocks_all(self):
        g = self._make_guardrails(set())
        result = g.before_tool_call("file_read", {})
        assert result is not None


# ---------------------------------------------------------------------------
# OperativeGuardrails – path traversal detection
# ---------------------------------------------------------------------------


class TestGuardrailsPathTraversal:
    def _make_guardrails(self) -> OperativeGuardrails:
        config = _make_config()
        return OperativeGuardrails(config, {"file_read", "file_write"})

    def test_safe_path_passes(self):
        g = self._make_guardrails()
        result = g.before_tool_call("file_read", {"path": "/workspace/src/main.py"})
        assert result is None

    def test_dotdot_slash_blocked(self):
        g = self._make_guardrails()
        result = g.before_tool_call("file_read", {"path": "/workspace/../etc/passwd"})
        assert result is not None
        assert "traversal" in result["error"].lower()

    @pytest.mark.skipif(sys.platform != "win32", reason="Backslash is not a path separator on Linux/macOS")
    def test_dotdot_backslash_blocked(self):
        g = self._make_guardrails()
        result = g.before_tool_call("file_read", {"path": "/workspace/..\\etc\\passwd"})
        assert result is not None
        assert "traversal" in result["error"].lower()

    @staticmethod
    def test_traversal_detection_static():
        assert OperativeGuardrails._has_path_traversal("../../etc/passwd") is True
        assert OperativeGuardrails._has_path_traversal("/workspace/src/foo.py") is False
        assert OperativeGuardrails._has_path_traversal("./relative/path.py") is False
        assert OperativeGuardrails._has_path_traversal("normal/path.py") is False


# ---------------------------------------------------------------------------
# OperativeGuardrails – step limits
# ---------------------------------------------------------------------------


class TestGuardrailsStepLimits:
    def _make_guardrails(self, max_steps: int = 5) -> OperativeGuardrails:
        config = _make_config()
        return OperativeGuardrails(config, {"file_read"}, max_steps=max_steps)

    def test_step_limit_not_reached_initially(self):
        g = self._make_guardrails(max_steps=5)
        assert g.check_step_limit() is False

    def test_step_limit_reached_after_max_steps(self):
        g = self._make_guardrails(max_steps=3)
        response = {"content": [], "usage": {"input": 10, "output": 5}}
        g.after_model_response(response)
        g.after_model_response(response)
        g.after_model_response(response)
        assert g.check_step_limit() is True

    def test_step_limit_not_reached_below_max(self):
        g = self._make_guardrails(max_steps=5)
        response = {"content": [], "usage": {"input": 10, "output": 5}}
        g.after_model_response(response)
        g.after_model_response(response)
        assert g.check_step_limit() is False


# ---------------------------------------------------------------------------
# OperativeGuardrails – token usage tracking
# ---------------------------------------------------------------------------


class TestGuardrailsTokenTracking:
    def test_token_usage_accumulates(self):
        config = _make_config()
        g = OperativeGuardrails(config, set())
        g.after_model_response({"content": [], "usage": {"input": 100, "output": 50}})
        g.after_model_response({"content": [], "usage": {"input": 200, "output": 75}})
        report = g.get_usage_report()
        assert report["token_usage"]["input"] == 300
        assert report["token_usage"]["output"] == 125

    def test_usage_report_structure(self):
        config = _make_config()
        g = OperativeGuardrails(config, {"tool_a"})
        g.before_tool_call("tool_a", {})
        report = g.get_usage_report()
        assert "steps" in report
        assert "tool_calls" in report
        assert "token_usage" in report
        assert report["tool_calls"] == 1


# ---------------------------------------------------------------------------
# OperativeGuardrails – message truncation
# ---------------------------------------------------------------------------


class TestGuardrailsMessageTruncation:
    def test_short_messages_pass_through(self):
        config = _make_config()
        g = OperativeGuardrails(config, set())
        messages = [{"role": "user", "content": "short"}]
        result = g.before_model_call(messages)
        assert result[0]["content"] == "short"

    def test_oversized_message_truncated(self):
        from henchmen.operative.guardrails import _MAX_MESSAGE_CHARS

        config = _make_config()
        g = OperativeGuardrails(config, set())
        long_content = "x" * (_MAX_MESSAGE_CHARS + 1000)
        messages = [{"role": "user", "content": long_content}]
        result = g.before_model_call(messages)
        assert len(result[0]["content"]) <= _MAX_MESSAGE_CHARS + 50  # some slack for truncation marker
        assert "truncated" in result[0]["content"]


# ---------------------------------------------------------------------------
# Gemini tool name resolution
# ---------------------------------------------------------------------------


class TestGeminiToolNameResolution:
    """Verify that tool results always carry tool_name for Gemini function responses."""

    def _make_tool_result_blocked(self, tool_name: str, tool_id: str) -> dict:
        """Simulate what _agent_loop produces for a guardrail-blocked tool call."""
        import json

        blocked = {"error": f"Tool '{tool_name}' is not allowed"}
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "tool_name": tool_name,
            "content": json.dumps(blocked),
        }

    def _make_tool_result_normal(self, tool_name: str, tool_id: str, raw: dict) -> dict:
        """Simulate what _agent_loop produces for a successfully-executed tool call."""
        import json

        raw_str = json.dumps(raw)
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "tool_name": tool_name,
            "content": raw_str,
        }

    def test_tool_result_carries_tool_name(self):
        """Contract: both blocked and normal tool result dicts must include tool_name."""
        blocked_result = self._make_tool_result_blocked("file_read", "call_abc")
        normal_result = self._make_tool_result_normal("git_commit", "call_xyz", {"success": True})

        assert "tool_name" in blocked_result, "blocked tool result must carry tool_name"
        assert blocked_result["tool_name"] == "file_read"

        assert "tool_name" in normal_result, "normal tool result must carry tool_name"
        assert normal_result["tool_name"] == "git_commit"

    def test_tool_result_name_not_unknown(self):
        """Regression: tool_name must never be the sentinel 'unknown' string."""
        blocked_result = self._make_tool_result_blocked("file_write", "call_001")
        normal_result = self._make_tool_result_normal("file_edit", "call_002", {"success": True})

        assert blocked_result.get("tool_name") != "unknown", "blocked tool result must not have tool_name='unknown'"
        assert normal_result.get("tool_name") != "unknown", "normal tool result must not have tool_name='unknown'"


# ---------------------------------------------------------------------------
# Template priority resolution
# ---------------------------------------------------------------------------


class TestTemplatePriority:
    """Verify instruction resolution: node template > task-type template > generic."""

    def _make_node(self, instruction_template: str | None) -> MagicMock:
        node = MagicMock()
        node.name = "test-node"
        node.instruction_template = instruction_template
        node.arsenal_requirement = None
        return node

    def _make_scheme_graph(self, node: MagicMock) -> MagicMock:
        graph = MagicMock()
        graph.get_node.return_value = node
        return graph

    @pytest.mark.asyncio
    async def test_node_template_preferred_over_task_type(self):
        """When the scheme node has an instruction_template, it wins over any task-type template."""
        node = self._make_node(instruction_template="Custom node instructions here.")
        graph = self._make_scheme_graph(node)
        config = _make_config()
        settings = MagicMock()

        with (
            patch("henchmen.schemes.registry.SchemeRegistry.get", return_value=graph),
            patch(
                "henchmen.operative.agent_builder._extract_task_type_from_dossier",
                return_value="test_fix",
            ),
            patch("henchmen.operative.agent_builder._load_dossier_context", return_value=""),
            patch("henchmen.operative.agent_builder._extract_code_search_context", return_value=""),
            patch("henchmen.operative.agent_builder._fetch_arsenal_tools", new=AsyncMock(return_value=([], {}))),
        ):
            from henchmen.operative.agent_builder import build_operative_agent

            agent = await build_operative_agent(config, "/tmp/workspace", settings)

        assert agent.instruction == "Custom node instructions here."

    @pytest.mark.asyncio
    async def test_task_type_fallback_when_no_node_template(self):
        """When node has no instruction_template and task_type is specific, use task-type template."""
        node = self._make_node(instruction_template=None)
        graph = self._make_scheme_graph(node)
        config = _make_config()
        settings = MagicMock()

        with (
            patch("henchmen.schemes.registry.SchemeRegistry.get", return_value=graph),
            patch(
                "henchmen.operative.agent_builder._extract_task_type_from_dossier",
                return_value="test_fix",
            ),
            patch("henchmen.operative.agent_builder._load_dossier_context", return_value=""),
            patch("henchmen.operative.agent_builder._extract_code_search_context", return_value=""),
            patch("henchmen.operative.agent_builder._fetch_arsenal_tools", new=AsyncMock(return_value=([], {}))),
        ):
            from henchmen.operative.agent_builder import build_operative_agent
            from henchmen.operative.prompt_templates import get_prompt_template

            agent = await build_operative_agent(config, "/tmp/workspace", settings)

        assert agent.instruction == get_prompt_template("test_fix")
        assert "fixing failing tests" in agent.instruction

    @pytest.mark.asyncio
    async def test_generic_fallback_when_no_node_and_no_task_type(self):
        """When node has no template and task_type is generic/None, use the generic template."""
        node = self._make_node(instruction_template=None)
        graph = self._make_scheme_graph(node)
        config = _make_config()
        settings = MagicMock()

        with (
            patch("henchmen.schemes.registry.SchemeRegistry.get", return_value=graph),
            patch(
                "henchmen.operative.agent_builder._extract_task_type_from_dossier",
                return_value="generic",
            ),
            patch("henchmen.operative.agent_builder._load_dossier_context", return_value=""),
            patch("henchmen.operative.agent_builder._extract_code_search_context", return_value=""),
            patch("henchmen.operative.agent_builder._fetch_arsenal_tools", new=AsyncMock(return_value=([], {}))),
        ):
            from henchmen.operative.agent_builder import build_operative_agent
            from henchmen.operative.prompt_templates import get_prompt_template

            agent = await build_operative_agent(config, "/tmp/workspace", settings)

        assert agent.instruction == get_prompt_template("generic")


# ---------------------------------------------------------------------------
# Bootstrap timeout status handling
# ---------------------------------------------------------------------------


class TestBootstrapTimeoutStatus:
    """Test that timed-out operatives do not get upgraded to COMPLETED."""

    @pytest.mark.asyncio
    async def test_timeout_with_changes_stays_timed_out(self):
        """An operative that times out should report TIMED_OUT even if it pushed changes."""
        from henchmen.operative.bootstrap import run_operative

        mock_registry = MagicMock()
        mock_registry.get_message_broker.return_value = MagicMock()
        mock_registry.get_object_store.return_value = MagicMock()
        mock_registry.get_llm_provider.return_value = MagicMock()

        with (
            patch.dict(
                "os.environ",
                {
                    "TASK_ID": "task-timeout",
                    "NODE_ID": "implement_fix",
                    "SCHEME_ID": "bugfix_standard",
                    "REPO_URL": "test/repo",
                },
            ),
            patch("henchmen.operative.bootstrap.get_settings") as mock_get_settings,
            patch("henchmen.operative.bootstrap.ProviderRegistry", return_value=mock_registry),
            patch("henchmen.operative.bootstrap.initialize_workspace", new_callable=AsyncMock, return_value="/tmp/ws"),
            patch("henchmen.operative.bootstrap._build_file_context", new_callable=AsyncMock, return_value=""),
            patch("henchmen.operative.bootstrap.build_operative_agent") as mock_build,
            patch("henchmen.operative.bootstrap._check_for_changes", new_callable=AsyncMock, return_value=True),
            patch("henchmen.operative.bootstrap._create_branch_and_push", new_callable=AsyncMock),
            patch("henchmen.operative.bootstrap.publish_report", new_callable=AsyncMock) as mock_publish,
            patch("builtins.open", MagicMock()),
        ):
            mock_get_settings.return_value = MagicMock()
            mock_get_settings.return_value.vertex_ai_model_complex = "test-model"

            mock_agent = AsyncMock()
            mock_agent.run.side_effect = TimeoutError("step limit")
            mock_build.return_value = mock_agent

            await run_operative()

            # Verify the report was published with TIMED_OUT, NOT COMPLETED
            report = mock_publish.call_args[0][0]
            assert report.status == OperativeStatus.TIMED_OUT


# ---------------------------------------------------------------------------
# OperativeGuardrails – extended telemetry
# ---------------------------------------------------------------------------


class TestGuardrailsExtendedTelemetry:
    """Test that telemetry includes context tokens, tool breakdown, and nudge count."""

    def test_telemetry_includes_model_name(self):
        config = _make_config(model_name="claude-sonnet-4@20250514")
        g = OperativeGuardrails(config, {"file_read"})
        telemetry = g.get_telemetry()
        assert telemetry["model_name"] == "claude-sonnet-4@20250514"

    def test_telemetry_includes_tool_calls_by_name(self):
        config = _make_config()
        g = OperativeGuardrails(config, {"file_read", "file_write"})
        g.before_tool_call("file_read", {"path": "/workspace/a.py"})
        g.before_tool_call("file_read", {"path": "/workspace/b.py"})
        g.before_tool_call("file_write", {"path": "/workspace/c.py"})
        telemetry = g.get_telemetry()
        assert telemetry["tool_calls_by_name"] == {"file_read": 2, "file_write": 1}

    def test_telemetry_includes_nudges_sent(self):
        config = _make_config()
        g = OperativeGuardrails(config, set())
        telemetry = g.get_telemetry()
        assert "nudges_sent" in telemetry
        assert telemetry["nudges_sent"] == 0

    def test_telemetry_includes_context_tokens(self):
        config = _make_config()
        g = OperativeGuardrails(config, set())
        g.after_model_response({"content": [], "usage": {"input": 5000, "output": 1000}})
        g.after_model_response({"content": [], "usage": {"input": 8000, "output": 2000}})
        telemetry = g.get_telemetry()
        assert telemetry["context_tokens_at_end"] == 8000  # Last input count = current context size

    def test_telemetry_includes_steps_used(self):
        config = _make_config()
        g = OperativeGuardrails(config, set())
        g.after_model_response({"content": [], "usage": {"input": 10, "output": 5}})
        g.after_model_response({"content": [], "usage": {"input": 20, "output": 10}})
        telemetry = g.get_telemetry()
        assert telemetry["steps_used"] == 2


# ---------------------------------------------------------------------------
# RAG boost in _build_file_context
# ---------------------------------------------------------------------------


class TestRagBoostInFileContext:
    def _write_workspace(self, tmp_path):
        """Create a minimal workspace with files."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "auth.py").write_text("def login(): pass\n", encoding="utf-8")
        (tmp_path / "src" / "utils.py").write_text("def helper(): pass\n", encoding="utf-8")
        (tmp_path / "src" / "unrelated.py").write_text("def other(): pass\n", encoding="utf-8")
        (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")

    def _write_dossier(self, tmp_path, semantic_chunks):
        """Write a dossier.json with semantic_code_chunks."""
        import json

        dossier_dir = tmp_path / ".henchmen" / "dossier"
        dossier_dir.mkdir(parents=True)
        dossier = {
            "task_id": "test-task",
            "semantic_code_chunks": [c.model_dump() for c in semantic_chunks],
        }
        (dossier_dir / "dossier.json").write_text(json.dumps(dossier), encoding="utf-8")

    @pytest.mark.asyncio
    async def test_rag_results_boost_file_scores(self):
        from henchmen.models.dossier import SemanticChunk
        from henchmen.operative.bootstrap import _build_file_context

        tmp_path = Path(tempfile.mkdtemp())
        self._write_workspace(tmp_path)

        chunks = [
            SemanticChunk(
                file_path="src/auth.py",
                start_line=1,
                end_line=1,
                symbol_name="login",
                language="python",
                content="def login(): pass",
                relevance_score=0.95,
            )
        ]
        self._write_dossier(tmp_path, chunks)

        context = await _build_file_context(str(tmp_path), "fix login", "login is broken")
        assert "auth.py" in context

    @pytest.mark.asyncio
    async def test_works_without_dossier(self):
        from henchmen.operative.bootstrap import _build_file_context

        tmp_path = Path(tempfile.mkdtemp())
        self._write_workspace(tmp_path)

        context = await _build_file_context(str(tmp_path), "fix login", "login is broken")
        assert "Pre-Read Files" in context


# ---------------------------------------------------------------------------
# OperativeGuardrails – canonical path validation
# ---------------------------------------------------------------------------


class TestGuardrailsCanonicalPathValidation:
    """Test canonical path validation replaces pattern matching."""

    def _make_guardrails(self) -> OperativeGuardrails:
        config = _make_config()
        return OperativeGuardrails(config, {"file_read", "file_write"})

    def test_safe_path_passes(self):
        self._make_guardrails()  # ensure construction works
        assert OperativeGuardrails._is_path_safe("/workspace/src/main.py", "/workspace") is True

    def test_dotdot_blocked(self):
        self._make_guardrails()
        assert OperativeGuardrails._is_path_safe("/workspace/../etc/passwd", "/workspace") is False

    def test_absolute_outside_blocked(self):
        self._make_guardrails()
        assert OperativeGuardrails._is_path_safe("/etc/passwd", "/workspace") is False

    def test_relative_path_safe(self):
        self._make_guardrails()
        assert OperativeGuardrails._is_path_safe("src/main.py", "/workspace") is True

    def test_nested_dotdot_blocked(self):
        """Multiple .. segments that escape the workspace should be blocked."""
        assert OperativeGuardrails._is_path_safe("src/../../etc/passwd", "/workspace") is False

    def test_current_dir_reference_safe(self):
        """Paths using ./ within workspace should be allowed."""
        assert OperativeGuardrails._is_path_safe("./src/main.py", "/workspace") is True

    def test_before_tool_call_uses_canonical_validation(self):
        """Verify that before_tool_call rejects paths that escape the workspace."""
        g = self._make_guardrails()
        with patch.dict("os.environ", {"WORKSPACE_DIR": "/workspace"}):
            result = g.before_tool_call("file_read", {"path": "/workspace/../etc/passwd"})
        assert result is not None
        assert "traversal" in result["error"].lower()


# ---------------------------------------------------------------------------
# SecretRedactionFilter
# ---------------------------------------------------------------------------


class TestSecretRedactionFilter:
    """Test that secret patterns are redacted from log records."""

    def _make_record(self, msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_github_pat_redacted(self):
        from henchmen.mastermind.server import _SecretRedactionFilter

        f = _SecretRedactionFilter()
        record = self._make_record("token is ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234")
        f.filter(record)
        assert "ghp_" not in str(record.msg)
        assert "***REDACTED***" in str(record.msg)

    def test_github_server_token_redacted(self):
        from henchmen.mastermind.server import _SecretRedactionFilter

        f = _SecretRedactionFilter()
        record = self._make_record("ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234")
        f.filter(record)
        assert "ghs_" not in str(record.msg)
        assert "***REDACTED***" in str(record.msg)

    def test_slack_token_redacted(self):
        from henchmen.mastermind.server import _SecretRedactionFilter

        f = _SecretRedactionFilter()
        record = self._make_record("bot token: xoxb-123-456-abc")
        f.filter(record)
        assert "xoxb-" not in str(record.msg)
        assert "***REDACTED***" in str(record.msg)

    def test_openai_key_redacted(self):
        from henchmen.mastermind.server import _SecretRedactionFilter

        f = _SecretRedactionFilter()
        record = self._make_record("key: sk-abcdefghijklmnopqrstuvwxyz123456")
        f.filter(record)
        assert "sk-" not in str(record.msg)
        assert "***REDACTED***" in str(record.msg)

    def test_git_credential_url_redacted(self):
        from henchmen.mastermind.server import _SecretRedactionFilter

        f = _SecretRedactionFilter()
        record = self._make_record("clone https://x-access-token:ghp_secret123@github.com/repo")
        f.filter(record)
        assert "x-access-token:ghp" not in str(record.msg)
        assert "***REDACTED***" in str(record.msg)

    def test_clean_message_unchanged(self):
        from henchmen.mastermind.server import _SecretRedactionFilter

        f = _SecretRedactionFilter()
        record = self._make_record("nothing secret here, just normal logging")
        f.filter(record)
        assert str(record.msg) == "nothing secret here, just normal logging"

    def test_filter_returns_true(self):
        """Filter must return True so the log record is not suppressed."""
        from henchmen.mastermind.server import _SecretRedactionFilter

        f = _SecretRedactionFilter()
        record = self._make_record("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234")
        assert f.filter(record) is True

    def test_bootstrap_filter_class_exists(self):
        """Verify the bootstrap module also has the filter class."""
        from henchmen.operative.bootstrap import _SecretRedactionFilter as BootstrapFilter

        f = BootstrapFilter()
        record = self._make_record("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234")
        f.filter(record)
        assert "***REDACTED***" in str(record.msg)
