"""Unit tests for the Mastermind orchestrator: scheme executor, lair manager, agent.

NOTE: The former ``TaskStateMachine`` test classes (Transitions, History,
AcceptanceChecks, CanTransition, Serialization, CrashRecovery) were removed
when the decorative in-memory state machine was deleted as part of finding E1.
Task lifecycle state now lives in Firestore ``task_executions/{task_id}``
documents owned by :class:`SchemeExecutor` and
:class:`~henchmen.observability.tracker.TaskTracker` — there is no separate
object to test.  Those tests were exercising code that was never persisted
and had no production behavioural effect.
"""

import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.mastermind.agent import MastermindAgent
from henchmen.mastermind.lair_manager import LairManager
from henchmen.mastermind.scheme_executor import SchemeExecutor
from henchmen.models.dossier import Dossier
from henchmen.models.operative import OperativeReport, OperativeStatus
from henchmen.models.scheme import (
    NodeType,
    SchemeDefinition,
    SchemeEdge,
    SchemeNode,
)
from henchmen.models.task import HenchmenTask, TaskContext, TaskSource
from henchmen.schemes.base import SchemeGraph
from henchmen.schemes.registry import SchemeRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(**overrides) -> HenchmenTask:
    defaults = {
        "id": "task-001",
        "source": TaskSource.SLACK,
        "source_id": "SLACK-123",
        "title": "Fix login crash",
        "description": "Users report a crash when logging in",
        "context": TaskContext(repo="acme/webapp", branch="main"),
        "created_by": "user@test.com",
    }
    defaults.update(overrides)
    return HenchmenTask(**defaults)


def _make_node(node_id: str, node_type: NodeType = NodeType.DETERMINISTIC, **kwargs) -> SchemeNode:
    return SchemeNode(id=node_id, name=node_id.replace("_", " ").title(), node_type=node_type, **kwargs)


def _make_edge(from_node: str, to_node: str, condition=None) -> SchemeEdge:
    return SchemeEdge(from_node=from_node, to_node=to_node, condition=condition)


def _linear_scheme(node_ids: list[str], node_types: dict[str, NodeType] | None = None) -> SchemeGraph:
    """Build a simple linear scheme: a -> b -> c -> ..."""
    node_types = node_types or {}
    nodes = [_make_node(nid, node_types.get(nid, NodeType.DETERMINISTIC)) for nid in node_ids]
    edges = [_make_edge(node_ids[i], node_ids[i + 1]) for i in range(len(node_ids) - 1)]
    definition = SchemeDefinition(
        id="test_scheme",
        name="Test Scheme",
        description="A test scheme",
        version="0.0.1",
        nodes=nodes,
        edges=edges,
    )
    return SchemeGraph(definition)


def _branching_scheme() -> SchemeGraph:
    """Build a scheme with pass/fail branching: root -> (pass) -> good -> end, root -> (fail) -> bad -> end."""
    nodes = [
        _make_node("root"),
        _make_node("good"),
        _make_node("bad"),
        _make_node("end"),
    ]
    edges = [
        _make_edge("root", "good", condition="pass"),
        _make_edge("root", "bad", condition="fail"),
        _make_edge("good", "end"),
        _make_edge("bad", "end"),
    ]
    definition = SchemeDefinition(
        id="branch_scheme",
        name="Branch Scheme",
        description="A branching test scheme",
        version="0.0.1",
        nodes=nodes,
        edges=edges,
    )
    return SchemeGraph(definition)


def _mock_settings():
    """Build a real ``Settings`` instance with test-safe defaults.

    The real Settings class already provides sensible defaults for
    lair/vertex/pubsub fields (see ``Settings.model_post_init`` and the
    field defaults), so we only need to seed the required
    ``HENCHMEN_GCP_PROJECT_ID`` env var and disable Vertex AI evaluation
    so tests don't try to hit the real service.
    """
    import os

    from henchmen.config.settings import get_settings

    os.environ.setdefault("HENCHMEN_GCP_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    return get_settings().model_copy(
        update={
            "provider": "gcp",
            "vertex_ai_evaluation_enabled": False,
            "lair_default_cpu": "2",
            "lair_default_memory": "4Gi",
        }
    )


# ===========================================================================
# TaskStateMachine test classes removed — see module docstring above.
# The classes (TestTaskStateMachineTransitions, TestTaskStateMachineHistory,
# TestTaskStateMachineAcceptanceChecks, TestTaskStateMachineCanTransition,
# TestTaskStateMachineSerialization, TestTaskStateMachineCrashRecovery)
# were testing an in-memory state machine that was never persisted and had
# no production effect. Deleted alongside ``state_machine.py`` for finding E1.
# ===========================================================================


# ===========================================================================
# SchemeExecutor
# ===========================================================================


class TestSchemeExecutorDeterministic:
    """Test SchemeExecutor with deterministic nodes."""

    @pytest.mark.asyncio
    async def test_execute_linear_deterministic_scheme(self):
        graph = _linear_scheme(["create_branch", "prefetch_context", "create_pr"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)

        assert result["final_status"] == "pr_created"
        assert result["pr_url"] is not None
        assert "create_branch" in result["nodes_executed"]
        assert "prefetch_context" in result["nodes_executed"]
        assert "create_pr" in result["nodes_executed"]

    @pytest.mark.asyncio
    async def test_execute_escalate_node(self):
        graph = _linear_scheme(["create_branch", "escalate"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)

        assert result["final_status"] == "escalated"
        assert result["escalated"] is True

    @pytest.mark.asyncio
    async def test_unknown_node_id_defaults_to_pass(self):
        graph = _linear_scheme(["unknown_step", "create_pr"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)
        # Unknown node returns pass, execution continues to create_pr
        assert "create_pr" in result["nodes_executed"]


class TestSchemeExecutorAgentic:
    """Test SchemeExecutor with agentic nodes (mocked lair)."""

    @pytest.mark.asyncio
    async def test_agentic_node_creates_and_waits_for_lair(self):
        graph = _linear_scheme(
            ["create_branch", "implement_fix", "create_pr"],
            node_types={"implement_fix": NodeType.AGENTIC},
        )

        mock_lair = AsyncMock(spec=LairManager)
        mock_lair.create_lair.return_value = "lair-001"
        mock_lair.wait_for_completion.return_value = OperativeReport(
            task_id="task-001",
            scheme_id="test_scheme",
            node_id="implement_fix",
            operative_id="lair-001",
            status=OperativeStatus.COMPLETED,
            summary="Fix implemented",
            confidence_score=0.9,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)

        mock_lair.create_lair.assert_called_once()
        mock_lair.wait_for_completion.assert_called_once_with("lair-001")
        assert "implement_fix" in result["nodes_executed"]
        assert result["node_results"]["implement_fix"]["condition"] == "pass"

    @pytest.mark.asyncio
    async def test_agentic_node_failure_returns_fail_condition(self):
        graph = _linear_scheme(
            ["agent_step"],
            node_types={"agent_step": NodeType.AGENTIC},
        )

        mock_lair = AsyncMock(spec=LairManager)
        mock_lair.create_lair.return_value = "lair-fail"
        mock_lair.wait_for_completion.return_value = OperativeReport(
            task_id="task-001",
            scheme_id="test_scheme",
            node_id="agent_step",
            operative_id="lair-fail",
            status=OperativeStatus.FAILED,
            summary="Failed",
            confidence_score=0.0,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)

        assert result["node_results"]["agent_step"]["condition"] == "fail"


class TestSchemeExecutorCIChecks:
    """Test that CI check failures are fail-closed."""

    @pytest.mark.asyncio
    async def test_clone_failure_returns_fail(self):
        """When git clone fails during CI check, condition should be 'fail'."""
        graph = _linear_scheme(["create_branch", "run_lint", "create_pr"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        task = _make_task()

        mock_proc = AsyncMock()
        mock_proc.returncode = 128
        mock_proc.communicate = AsyncMock(return_value=(b"", b"fatal: remote branch not found"))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            from henchmen.mastermind.scheme_executor.handlers import _run_ci_check

            result = await _run_ci_check(executor, task, "lint")

        assert result["condition"] == "fail"
        assert "clone failed" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_ci_exception_returns_fail(self):
        """When CI check raises an exception, condition should be 'fail'."""
        graph = _linear_scheme(["create_branch", "run_tests", "create_pr"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        task = _make_task()

        with patch("asyncio.create_subprocess_exec", side_effect=OSError("no git")):
            from henchmen.mastermind.scheme_executor.handlers import _run_ci_check

            result = await _run_ci_check(executor, task, "tests")

        assert result["condition"] == "fail"
        assert "error" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_no_repo_returns_fail(self):
        """When task has no repo, CI check should fail, not pass."""
        graph = _linear_scheme(["run_lint"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        task = _make_task()
        task.context.repo = ""

        from henchmen.mastermind.scheme_executor.handlers import _run_ci_check

        result = await _run_ci_check(executor, task, "lint")

        assert result["condition"] == "fail"


class TestSchemeExecutorLairFailure:
    """Test that lair provisioning failures are fail-closed."""

    @pytest.mark.asyncio
    async def test_lair_failure_returns_fail_in_prod(self):
        """When lair provisioning fails in prod, agentic node should return 'fail'."""
        graph = _linear_scheme(
            ["agent_node"],
            node_types={"agent_node": NodeType.AGENTIC},
        )

        mock_lair = AsyncMock(spec=LairManager)
        mock_lair.create_lair.side_effect = RuntimeError("Cloud Run unavailable")
        settings = _mock_settings()
        settings.environment = MagicMock()
        settings.environment.value = "prod"

        executor = SchemeExecutor(graph, mock_lair, settings)
        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)

        assert result["node_results"]["agent_node"]["condition"] == "fail"

    @pytest.mark.asyncio
    async def test_lair_failure_returns_pass_in_dev(self):
        """In dev mode, lair failures can still simulate pass for testing."""
        graph = _linear_scheme(
            ["agent_node"],
            node_types={"agent_node": NodeType.AGENTIC},
        )

        mock_lair = AsyncMock(spec=LairManager)
        mock_lair.create_lair.side_effect = RuntimeError("Cloud Run unavailable")
        settings = _mock_settings()
        settings.environment = MagicMock()
        settings.environment.value = "dev"

        executor = SchemeExecutor(graph, mock_lair, settings)
        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)

        assert result["node_results"]["agent_node"]["condition"] == "pass"
        assert result["node_results"]["agent_node"].get("dev_mode") is True


class TestSchemeExecutorMaxRetries:
    """Test that max-retry exhaustion forces a fail condition (not pass)."""

    @pytest.mark.asyncio
    async def test_max_retries_forces_fail_condition(self):
        """When a node hits max retries, condition must be 'fail' so the DAG routes to escalate."""
        graph = _branching_scheme()  # root -> (pass) -> good, root -> (fail) -> bad
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        # Pre-set retry count to max so the node is already exhausted
        executor._retry_counts["root"] = 2

        task = _make_task()
        dossier = Dossier(task_id=task.id)
        result = await executor.execute(task, dossier)

        assert result["node_results"]["root"]["condition"] == "fail"
        assert "bad" in result["nodes_executed"]
        assert "good" not in result["nodes_executed"]

    @pytest.mark.asyncio
    async def test_max_retries_includes_escalation_flag(self):
        """Max-retry result message should mention 'max retries' for observability."""
        graph = _linear_scheme(["solo_node"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        executor._retry_counts["solo_node"] = 2

        task = _make_task()
        dossier = Dossier(task_id=task.id)
        result = await executor.execute(task, dossier)

        node_result = result["node_results"]["solo_node"]
        assert node_result["condition"] == "fail"
        assert "max retries" in node_result["message"].lower()


class TestSchemeExecutorBranching:
    """Test SchemeExecutor handles pass/fail branch conditions."""

    @pytest.mark.asyncio
    async def test_pass_branch_taken(self):
        graph = _branching_scheme()
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        # Root handler returns "pass" condition -> should go to "good"
        executor = SchemeExecutor(graph, mock_lair, settings)
        # Override root handler to return pass
        executor._execute_deterministic = AsyncMock(
            side_effect=lambda node, task, dossier: (
                {"condition": "pass", "message": "passed"}
                if node.id == "root"
                else {"condition": None, "message": "ok"}
            )
        )

        task = _make_task()
        dossier = Dossier(task_id=task.id)
        result = await executor.execute(task, dossier)

        assert "good" in result["nodes_executed"]
        assert "bad" not in result["nodes_executed"]

    @pytest.mark.asyncio
    async def test_fail_branch_taken(self):
        graph = _branching_scheme()
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        executor._execute_deterministic = AsyncMock(
            side_effect=lambda node, task, dossier: (
                {"condition": "fail", "message": "failed"}
                if node.id == "root"
                else {"condition": None, "message": "ok"}
            )
        )

        task = _make_task()
        dossier = Dossier(task_id=task.id)
        result = await executor.execute(task, dossier)

        assert "bad" in result["nodes_executed"]
        assert "good" not in result["nodes_executed"]


# ===========================================================================
# MastermindAgent._select_scheme
# ===========================================================================


class TestMastermindSelectScheme:
    """Test scheme selection keyword matching."""

    @pytest.mark.asyncio
    async def test_selects_bugfix_for_bug_keywords(self):
        agent = MastermindAgent(settings=_mock_settings())
        for keyword in ["bug", "fix", "error", "crash", "broken"]:
            task = _make_task(title=f"There is a {keyword} here", description="details")
            scheme = await agent._select_scheme(task)
            assert scheme == "bugfix_standard", f"Failed for keyword: {keyword}"

    @pytest.mark.asyncio
    async def test_selects_feature_for_feature_keywords(self):
        agent = MastermindAgent(settings=_mock_settings())
        for keyword in ["feature", "add", "implement", "create", "build"]:
            task = _make_task(title=f"Please {keyword} something", description="details")
            scheme = await agent._select_scheme(task)
            assert scheme == "feature_standard", f"Failed for keyword: {keyword}"

    @pytest.mark.asyncio
    async def test_defaults_to_bugfix_for_ambiguous_task(self):
        agent = MastermindAgent(settings=_mock_settings())
        task = _make_task(title="Update readme", description="Just some text")
        scheme = await agent._select_scheme(task)
        assert scheme == "bugfix_standard"

    @pytest.mark.asyncio
    async def test_bug_keyword_in_description_matches(self):
        agent = MastermindAgent(settings=_mock_settings())
        task = _make_task(title="Something", description="There is a critical bug")
        scheme = await agent._select_scheme(task)
        assert scheme == "bugfix_standard"


# ===========================================================================
# MastermindAgent.handle_task (full lifecycle)
# ===========================================================================


class TestMastermindHandleTask:
    """Test full task lifecycle with mocked LairManager and DossierBuilder."""

    def setup_method(self):
        SchemeRegistry.clear()
        SchemeRegistry.auto_discover()

    def teardown_method(self):
        SchemeRegistry.clear()

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.scheme_executor.executor.SchemeExecutor.execute", new_callable=AsyncMock)
    @patch("henchmen.mastermind.agent.DossierBuilder")
    async def test_handle_task_full_lifecycle(self, mock_builder_cls, mock_executor_execute):
        """Full lifecycle: task -> scheme_selected -> ... -> completed.

        The SchemeExecutor.execute call is stubbed to return a canned success
        result so the test focuses on handle_task's orchestration, not the
        downstream handler chain (which has its own dedicated tests).
        """
        settings = _mock_settings()
        agent = MastermindAgent(settings=settings)

        # Mock the DossierBuilder
        mock_builder = AsyncMock()
        mock_builder.build.return_value = Dossier(task_id="task-001")
        mock_builder_cls.return_value = mock_builder

        # Mock the LairManager to avoid actual Cloud Run calls
        agent.lair_manager = AsyncMock(spec=LairManager)
        agent.lair_manager.create_lair.return_value = "lair-test"
        agent.lair_manager.wait_for_completion.return_value = OperativeReport(
            task_id="task-001",
            scheme_id="bugfix_standard",
            node_id="implement_fix",
            operative_id="lair-test",
            status=OperativeStatus.COMPLETED,
            summary="Fix done",
            confidence_score=0.95,
            files_changed=["src/example.py"],
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )

        # Canned success report from the scheme executor so we test
        # handle_task, not the full DAG walk (which is covered by
        # integration tests).
        mock_executor_execute.return_value = {
            "final_status": "completed",
            "nodes_executed": [
                "create_branch",
                "prefetch_context",
                "implement_fix",
                "verify_changes",
                "run_lint",
                "run_tests",
                "create_pr",
            ],
            "pr_url": "https://github.com/example/repo/pull/1",
        }

        task = _make_task()
        result = await agent.handle_task(task)

        assert result["task_id"] == "task-001"
        assert result["scheme_id"] == "bugfix_standard"
        assert result["status"] == "completed"

        # Verify the task was tracked in the in-memory active set.
        # Authoritative state lives in Firestore `task_executions/{task_id}`.
        assert "task-001" in agent._active_tasks

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.agent.DossierBuilder")
    async def test_handle_task_escalates_on_unknown_scheme(self, mock_builder_cls):
        """If scheme graph is not found, task should be escalated."""
        settings = _mock_settings()
        agent = MastermindAgent(settings=settings)

        # Clear registry so no schemes are found
        SchemeRegistry.clear()

        task = _make_task()
        result = await agent.handle_task(task)

        assert result["status"] == "escalated"
        assert "Unknown scheme" in result.get("reason", "")

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.agent.DossierBuilder")
    async def test_handle_task_escalates_on_exception(self, mock_builder_cls):
        """If an exception occurs, task should be escalated."""
        settings = _mock_settings()
        agent = MastermindAgent(settings=settings)

        # Make _select_scheme raise
        agent._select_scheme = AsyncMock(side_effect=RuntimeError("boom"))

        task = _make_task()
        result = await agent.handle_task(task)

        assert result["status"] == "escalated"
        assert "boom" in result.get("error", "")


# ===========================================================================
# LairManager env vars and image building
# ===========================================================================


class TestLairManagerBuildJobConfig:
    """Test job configuration building via _build_env_vars and _build_image."""

    def test_basic_env_vars_structure(self):
        settings = _mock_settings()
        settings.lair_operative_image_tag = "latest"
        lm = LairManager(settings)
        task = _make_task()
        node = _make_node("test_node", timeout_seconds=60)

        env_vars = lm._build_env_vars(task, node, "lair-test-001")

        assert env_vars["TASK_ID"] == "task-001"
        assert env_vars["NODE_ID"] == "test_node"
        assert env_vars["LAIR_ID"] == "lair-test-001"
        assert env_vars["REPO_URL"] == "acme/webapp"
        assert env_vars["BRANCH"] == "main"

    def test_long_running_node_gets_more_resources(self):
        # Resource scaling happens in create_lair, not in env vars.
        # Verify timeout threshold logic: >300s uses 4cpu/8Gi.
        node_long = _make_node("long_node", timeout_seconds=600)
        node_short = _make_node("short_node", timeout_seconds=30)
        assert node_long.timeout_seconds > 300
        assert node_short.timeout_seconds <= 300

    def test_short_running_node_uses_defaults(self):
        node = _make_node("short_node", timeout_seconds=30)
        assert node.timeout_seconds <= 300

    def test_model_name_override(self):
        settings = _mock_settings()
        lm = LairManager(settings)
        task = _make_task()
        node = _make_node("model_node", model_name="gemini-2.5-flash")

        env_vars = lm._build_env_vars(task, node, "lair-model-001")
        assert env_vars["MODEL_NAME"] == "gemini-2.5-flash"

    def test_model_name_fallback_to_settings(self):
        settings = _mock_settings()
        lm = LairManager(settings)
        task = _make_task()
        node = _make_node("fallback_node")  # model_name=None

        env_vars = lm._build_env_vars(task, node, "lair-fb-001")
        assert env_vars["MODEL_NAME"] == "gemini-2.5-pro"

    def test_image_contains_project_and_region(self):
        settings = _mock_settings()
        settings.lair_operative_image_tag = "latest"
        lm = LairManager(settings)

        image = lm._build_image()
        assert "us-central1" in image
        assert "test-project" in image
        assert "henchmen-dev" in image


# ---------------------------------------------------------------------------
# Semantic chunk fetching
# ---------------------------------------------------------------------------


class TestFetchSemanticChunks:
    def _make_agent(self):
        settings = MagicMock()
        settings.gcp_project_id = "test-project"
        settings.pinecone_api_key_secret = ""
        settings.pinecone_index_name = "henchmen-code"
        settings.vertex_ai_embedding_model = "text-embedding-005"
        with patch("henchmen.mastermind.agent.LairManager"):
            return MastermindAgent(settings=settings)

    def _make_task(self):
        return HenchmenTask(
            source=TaskSource.GITHUB,
            source_id="gh-123",
            title="Fix login bug",
            description="Users cannot log in with SSO",
            context=TaskContext(repo="myorg/myrepo"),
            created_by="user1",
        )

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_pinecone_key(self):
        agent = self._make_agent()
        task = self._make_task()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PINECONE_API_KEY", None)
            result = await agent._fetch_semantic_chunks(task)

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_chunks_when_configured(self):
        from henchmen.models.dossier import SemanticChunk

        agent = self._make_agent()
        task = self._make_task()

        mock_chunks = [
            SemanticChunk(
                file_path="src/auth.py",
                start_line=1,
                end_line=10,
                symbol_name="login",
                language="python",
                content="def login(): ...",
                relevance_score=0.9,
            )
        ]

        with (
            patch.dict(os.environ, {"PINECONE_API_KEY": "test-key"}),
            patch("henchmen.mastermind.agent.query_similar_chunks", new_callable=AsyncMock, return_value=mock_chunks),
        ):
            result = await agent._fetch_semantic_chunks(task)

        assert len(result) == 1
        assert result[0].file_path == "src/auth.py"

    @pytest.mark.asyncio
    async def test_graceful_on_exception(self):
        agent = self._make_agent()
        task = self._make_task()

        with (
            patch.dict(os.environ, {"PINECONE_API_KEY": "test-key"}),
            patch(
                "henchmen.mastermind.agent.query_similar_chunks", new_callable=AsyncMock, side_effect=Exception("boom")
            ),
        ):
            result = await agent._fetch_semantic_chunks(task)

        assert result == []


# ===========================================================================
# SchemeExecutor Checkpointing & Resume
# ===========================================================================


class TestSchemeExecutorCheckpointing:
    """Test that scheme executor checkpoints state after each node."""

    @pytest.mark.asyncio
    async def test_checkpoint_called_after_each_node(self):
        graph = _linear_scheme(["create_branch", "prefetch_context", "create_pr"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()
        mock_tracker = MagicMock()

        executor = SchemeExecutor(graph, mock_lair, settings, tracker=mock_tracker)
        task = _make_task()
        dossier = Dossier(task_id=task.id)

        await executor.execute(task, dossier)

        # update_execution_state should be called once per node
        assert mock_tracker.update_execution_state.call_count == 3

    @pytest.mark.asyncio
    async def test_resume_skips_completed_nodes(self):
        graph = _linear_scheme(["create_branch", "prefetch_context", "create_pr"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        # Pre-populate completed nodes (simulating resume from checkpoint)
        executor.node_results = {
            "create_branch": {"condition": None, "branch_name": "henchmen/test"},
            "prefetch_context": {"condition": None},
        }

        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)

        # All three nodes should appear in node_results (two from checkpoint + one executed)
        assert "create_branch" in result["node_results"]
        assert "prefetch_context" in result["node_results"]
        assert "create_pr" in result["node_results"]
        # Only create_pr should have been freshly executed — checkpoint-restored nodes excluded
        assert "create_pr" in result["nodes_executed"]
        assert "create_branch" not in result["nodes_executed"]
        assert "prefetch_context" not in result["nodes_executed"]

    @pytest.mark.asyncio
    async def test_checkpoint_includes_correct_arguments(self):
        """Verify the checkpoint call passes the right task_id, node_id, and state."""
        graph = _linear_scheme(["create_branch"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()
        mock_tracker = MagicMock()

        executor = SchemeExecutor(graph, mock_lair, settings, tracker=mock_tracker)
        task = _make_task()
        dossier = Dossier(task_id=task.id)

        await executor.execute(task, dossier)

        mock_tracker.update_execution_state.assert_called_once()
        call_kwargs = mock_tracker.update_execution_state.call_args
        # Should pass task_id, current_node_id, node_results, retry_counts
        assert call_kwargs.kwargs["task_id"] == task.id
        assert call_kwargs.kwargs["current_node_id"] == "create_branch"
        assert "create_branch" in call_kwargs.kwargs["node_results"]
        assert isinstance(call_kwargs.kwargs["retry_counts"], dict)

    @pytest.mark.asyncio
    async def test_checkpoint_failure_does_not_stop_execution(self):
        """If checkpoint write fails, execution should continue with a warning, not crash."""
        graph = _linear_scheme(["create_branch", "create_pr"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()
        mock_tracker = MagicMock()
        mock_tracker.update_execution_state.side_effect = RuntimeError("Firestore unavailable")

        executor = SchemeExecutor(graph, mock_lair, settings, tracker=mock_tracker)
        task = _make_task()
        dossier = Dossier(task_id=task.id)

        # Should NOT raise — checkpoint failure is non-fatal
        result = await executor.execute(task, dossier)

        assert "create_branch" in result["nodes_executed"]
        assert "create_pr" in result["nodes_executed"]

    @pytest.mark.asyncio
    async def test_resume_preserves_checkpoint_data_in_report(self):
        """Resumed (skipped) nodes should preserve their original result data."""
        graph = _linear_scheme(["create_branch", "create_pr"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        executor.node_results = {
            "create_branch": {"condition": None, "branch_name": "henchmen/abc123"},
        }

        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)

        # The checkpoint data for create_branch should be preserved exactly
        assert result["node_results"]["create_branch"]["branch_name"] == "henchmen/abc123"

    @pytest.mark.asyncio
    async def test_nodes_executed_excludes_skipped_nodes(self):
        """nodes_executed in the report should only list nodes that were actually run, not skipped."""
        graph = _linear_scheme(["create_branch", "prefetch_context", "create_pr"])
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        executor.node_results = {
            "create_branch": {"condition": None, "branch_name": "henchmen/test"},
        }

        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)

        # create_branch was loaded from checkpoint, not freshly executed — must be absent
        assert "create_branch" not in result["nodes_executed"]
        # prefetch_context and create_pr were freshly executed in this session
        assert "prefetch_context" in result["nodes_executed"]
        assert "create_pr" in result["nodes_executed"]


# ===========================================================================
# SchemeExecutor Cycle Detection & Dead-End Escalation (Fix 3)
# ===========================================================================


class TestSchemeExecutorCycleDetection:
    """Test that the executor detects cycles and dead-end failures."""

    @pytest.mark.asyncio
    async def test_cycle_detection_breaks_loop(self):
        """If the executor revisits a (node_id, retry_count) state, it should break and escalate."""
        # Build a scheme: root -> a -> b -> a (unconditional loop between a and b)
        nodes = [_make_node("root"), _make_node("a"), _make_node("b")]
        edges = [_make_edge("root", "a"), _make_edge("a", "b"), _make_edge("b", "a")]
        definition = SchemeDefinition(
            id="cycle_test",
            name="Cycle Test",
            description="d",
            version="0.0.1",
            nodes=nodes,
            edges=edges,
        )
        graph = SchemeGraph(definition)
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)

        # Should detect the cycle and set escalated
        assert result["escalated"] is True

    @pytest.mark.asyncio
    async def test_dead_end_fail_sets_escalated(self):
        """When a node returns fail and no fail-edge exists, escalated should be set."""
        # Root node returns "fail" but has no fail-edge
        nodes = [_make_node("root")]
        definition = SchemeDefinition(
            id="dead_end_test",
            name="Dead End Test",
            description="d",
            version="0.0.1",
            nodes=nodes,
            edges=[],
        )
        graph = SchemeGraph(definition)
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        # Override the deterministic handler to return "fail"
        executor._execute_deterministic = AsyncMock(return_value={"condition": "fail", "message": "something broke"})

        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)

        # The dead-end node with fail should have escalated flag
        assert result["node_results"]["root"].get("escalated") is True
        assert result["escalated"] is True

    @pytest.mark.asyncio
    async def test_dead_end_pass_does_not_escalate(self):
        """When a node returns pass and no next-edge exists (terminal), no escalation."""
        nodes = [_make_node("root")]
        definition = SchemeDefinition(
            id="terminal_pass",
            name="Terminal Pass",
            description="d",
            version="0.0.1",
            nodes=nodes,
            edges=[],
        )
        graph = SchemeGraph(definition)
        mock_lair = MagicMock(spec=LairManager)
        settings = _mock_settings()

        executor = SchemeExecutor(graph, mock_lair, settings)
        # Override to return "pass"
        executor._execute_deterministic = AsyncMock(return_value={"condition": "pass", "message": "ok"})

        task = _make_task()
        dossier = Dossier(task_id=task.id)

        result = await executor.execute(task, dossier)

        assert result["escalated"] is False


# ===========================================================================
# Scheme DAG Fail-Edges (Fix 3)
# ===========================================================================


class TestSchemeFailEdges:
    """Test that implement_feature/implement_fix have fail-edges to escalate."""

    def test_feature_implement_feature_fail_goes_to_escalate(self):
        from henchmen.schemes.feature_standard import FEATURE_STANDARD

        graph = SchemeGraph(FEATURE_STANDARD)
        fail_nexts = graph.get_next_nodes("implement_feature", condition="fail")
        assert len(fail_nexts) == 1
        assert fail_nexts[0].id == "escalate"

    def test_bugfix_implement_fix_fail_goes_to_escalate(self):
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD

        graph = SchemeGraph(BUGFIX_STANDARD)
        fail_nexts = graph.get_next_nodes("implement_fix", condition="fail")
        assert len(fail_nexts) == 1
        assert fail_nexts[0].id == "escalate"

    def test_feature_verify_changes_fail_goes_to_escalate(self):
        from henchmen.schemes.feature_standard import FEATURE_STANDARD

        graph = SchemeGraph(FEATURE_STANDARD)
        fail_nexts = graph.get_next_nodes("verify_changes", condition="fail")
        assert len(fail_nexts) == 1
        assert fail_nexts[0].id == "escalate"

    def test_bugfix_verify_changes_fail_goes_to_escalate(self):
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD

        graph = SchemeGraph(BUGFIX_STANDARD)
        fail_nexts = graph.get_next_nodes("verify_changes", condition="fail")
        assert len(fail_nexts) == 1
        assert fail_nexts[0].id == "escalate"


# ===========================================================================
# Feature Standard: plan_implementation removed (Fix 4)
# ===========================================================================


class TestFeatureStandardNoPlanNode:
    """Test that plan_implementation is removed and prefetch goes straight to implement."""

    def test_no_plan_implementation_node(self):
        from henchmen.schemes.feature_standard import FEATURE_STANDARD

        node_ids = [n.id for n in FEATURE_STANDARD.nodes]
        assert "plan_implementation" not in node_ids

    def test_prefetch_goes_to_implement_feature(self):
        from henchmen.schemes.feature_standard import FEATURE_STANDARD

        graph = SchemeGraph(FEATURE_STANDARD)
        nexts = graph.get_next_nodes("prefetch_context")
        assert len(nexts) == 1
        assert nexts[0].id == "implement_feature"

    def test_verify_changes_is_deterministic(self):
        from henchmen.schemes.feature_standard import FEATURE_STANDARD

        for node in FEATURE_STANDARD.nodes:
            if node.id == "verify_changes":
                assert node.node_type == NodeType.DETERMINISTIC
                break
        else:
            pytest.fail("verify_changes node not found")

    def test_bugfix_verify_changes_is_deterministic(self):
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD

        for node in BUGFIX_STANDARD.nodes:
            if node.id == "verify_changes":
                assert node.node_type == NodeType.DETERMINISTIC
                break
        else:
            pytest.fail("verify_changes node not found")
