"""Integration tests for Mastermind orchestration.

Verifies the MastermindAgent works end-to-end:
  - scheme selection (keyword matching)
  - dossier building from the first agentic node
  - scheme DAG execution (linear, pass/fail branches, escalation)
  - state machine lifecycle, crash recovery, and history recording
  - full handle_task flows with mocked LairManager and CI

Target repo: acme-org/sample-repo
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.mastermind.agent import MastermindAgent
from henchmen.mastermind.lair_manager import LairManager
from henchmen.mastermind.scheme_executor import SchemeExecutor
from henchmen.mastermind.state_machine import TaskState, TaskStateMachine
from henchmen.models.dossier import Dossier
from henchmen.models.operative import OperativeReport, OperativeStatus
from henchmen.models.scheme import (
    ArsenalRequirement,
    DossierRequirement,
    NodeType,
    SchemeDefinition,
    SchemeEdge,
    SchemeNode,
)
from henchmen.models.task import HenchmenTask, TaskContext, TaskPriority, TaskSource
from henchmen.schemes.base import SchemeGraph
from henchmen.schemes.registry import SchemeRegistry

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

REPO = "acme-org/sample-repo"


def make_task(title: str, description: str = "") -> HenchmenTask:
    """Create a minimal HenchmenTask pointing at the target repo."""
    return HenchmenTask(
        source=TaskSource.CLI,
        source_id="test-source-id",
        title=title,
        description=description,
        context=TaskContext(repo=REPO, branch="main"),
        priority=TaskPriority.NORMAL,
        created_by="test@example.com",
    )


def make_operative_report(
    task_id: str, node_id: str, scheme_id: str = "test_scheme", status: OperativeStatus = OperativeStatus.COMPLETED
) -> OperativeReport:
    """Create a real OperativeReport model object."""
    now = datetime.now(UTC)
    return OperativeReport(
        task_id=task_id,
        scheme_id=scheme_id,
        node_id=node_id,
        operative_id=f"mock-lair-{node_id}",
        status=status,
        summary=f"Node {node_id} completed",
        confidence_score=0.9,
        started_at=now,
        completed_at=now,
    )


def make_simple_scheme(scheme_id: str = "simple_scheme") -> SchemeGraph:
    """Build a minimal two-node deterministic linear scheme for executor tests."""
    definition = SchemeDefinition(
        id=scheme_id,
        name="Simple Test Scheme",
        description="A minimal scheme for executor tests",
        version="1.0.0",
        nodes=[
            SchemeNode(
                id="create_branch",
                name="Create Branch",
                node_type=NodeType.DETERMINISTIC,
                timeout_seconds=30,
            ),
            SchemeNode(
                id="create_pr",
                name="Create PR",
                node_type=NodeType.DETERMINISTIC,
                timeout_seconds=30,
            ),
        ],
        edges=[
            SchemeEdge(from_node="create_branch", to_node="create_pr"),
        ],
    )
    return SchemeGraph(definition)


def make_branching_scheme(scheme_id: str = "branching_scheme") -> SchemeGraph:
    """Build a scheme with pass/fail edges for branch tests."""
    definition = SchemeDefinition(
        id=scheme_id,
        name="Branching Test Scheme",
        description="Scheme with pass/fail branches",
        version="1.0.0",
        nodes=[
            SchemeNode(
                id="create_branch",
                name="Create Branch",
                node_type=NodeType.DETERMINISTIC,
                timeout_seconds=30,
            ),
            SchemeNode(
                id="run_tests",
                name="Run Tests",
                node_type=NodeType.DETERMINISTIC,
                timeout_seconds=60,
            ),
            SchemeNode(
                id="create_pr",
                name="Create PR (pass)",
                node_type=NodeType.DETERMINISTIC,
                timeout_seconds=30,
            ),
            SchemeNode(
                id="escalate",
                name="Escalate (fail)",
                node_type=NodeType.DETERMINISTIC,
                timeout_seconds=30,
            ),
        ],
        edges=[
            SchemeEdge(from_node="create_branch", to_node="run_tests"),
            SchemeEdge(from_node="run_tests", to_node="create_pr", condition="pass"),
            SchemeEdge(from_node="run_tests", to_node="escalate", condition="fail"),
        ],
    )
    return SchemeGraph(definition)


def make_agentic_scheme(scheme_id: str = "agentic_scheme") -> SchemeGraph:
    """Build a scheme with one agentic node followed by a PR creation."""
    definition = SchemeDefinition(
        id=scheme_id,
        name="Agentic Test Scheme",
        description="Scheme with an agentic node",
        version="1.0.0",
        nodes=[
            SchemeNode(
                id="implement_fix",
                name="Implement Fix",
                node_type=NodeType.AGENTIC,
                arsenal_requirement=ArsenalRequirement(tool_sets=["code_intel", "code_edit"]),
                dossier_requirement=DossierRequirement(fetch_files=True, fetch_rules=True),
                timeout_seconds=300,
            ),
            SchemeNode(
                id="create_pr",
                name="Create PR",
                node_type=NodeType.DETERMINISTIC,
                timeout_seconds=30,
            ),
        ],
        edges=[
            SchemeEdge(from_node="implement_fix", to_node="create_pr", condition="pass"),
        ],
    )
    return SchemeGraph(definition)


def make_agentic_retry_scheme(scheme_id: str = "retry_scheme") -> SchemeGraph:
    """Build a scheme where test failures cause escalation after retry."""
    definition = SchemeDefinition(
        id=scheme_id,
        name="Retry Scheme",
        description="Scheme with agentic node then test retry leading to escalation",
        version="1.0.0",
        nodes=[
            SchemeNode(
                id="implement_fix",
                name="Implement Fix",
                node_type=NodeType.AGENTIC,
                arsenal_requirement=ArsenalRequirement(tool_sets=["code_edit"]),
                dossier_requirement=DossierRequirement(fetch_files=True),
                timeout_seconds=300,
            ),
            SchemeNode(
                id="run_tests",
                name="Run Tests",
                node_type=NodeType.DETERMINISTIC,
                timeout_seconds=60,
            ),
            SchemeNode(
                id="run_tests_retry",
                name="Run Tests Retry",
                node_type=NodeType.DETERMINISTIC,
                timeout_seconds=60,
            ),
            SchemeNode(
                id="create_pr",
                name="Create PR",
                node_type=NodeType.DETERMINISTIC,
                timeout_seconds=30,
            ),
            SchemeNode(
                id="escalate",
                name="Escalate",
                node_type=NodeType.DETERMINISTIC,
                timeout_seconds=30,
            ),
        ],
        edges=[
            SchemeEdge(from_node="implement_fix", to_node="run_tests", condition="pass"),
            SchemeEdge(from_node="run_tests", to_node="create_pr", condition="pass"),
            SchemeEdge(from_node="run_tests", to_node="run_tests_retry", condition="fail"),
            SchemeEdge(from_node="run_tests_retry", to_node="create_pr", condition="pass"),
            SchemeEdge(from_node="run_tests_retry", to_node="escalate", condition="fail"),
        ],
    )
    return SchemeGraph(definition)


# ---------------------------------------------------------------------------
# TestSchemeSelection
# ---------------------------------------------------------------------------


class TestSchemeSelection:
    """Verify _select_scheme picks the right scheme_id from task content."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings):
        self.settings = integration_settings

    @pytest.mark.asyncio
    async def test_bugfix_keywords_select_bugfix_standard(self):
        """Task with 'bug', 'fix', 'error', 'crash', 'broken' → bugfix_standard."""
        agent = MastermindAgent(self.settings)
        for keyword in ["bug report", "fix this", "error in auth", "app crash", "broken login"]:
            task = make_task(title=keyword)
            scheme_id = await agent._select_scheme(task)
            assert scheme_id == "bugfix_standard", f"Expected bugfix_standard for title {keyword!r}, got {scheme_id!r}"

    @pytest.mark.asyncio
    async def test_feature_keywords_select_feature_standard(self):
        """Task with 'feature', 'add', 'implement', 'create', 'build' → feature_standard."""
        agent = MastermindAgent(self.settings)
        for keyword in ["new feature", "add endpoint", "implement oauth", "create widget", "build dashboard"]:
            task = make_task(title=keyword)
            scheme_id = await agent._select_scheme(task)
            assert scheme_id == "feature_standard", (
                f"Expected feature_standard for title {keyword!r}, got {scheme_id!r}"
            )

    @pytest.mark.asyncio
    async def test_ambiguous_defaults_to_bugfix(self):
        """Task with an unrelated title defaults to bugfix_standard."""
        agent = MastermindAgent(self.settings)
        task = make_task(title="Update documentation", description="Improve the README layout")
        scheme_id = await agent._select_scheme(task)
        assert scheme_id == "bugfix_standard"

    @pytest.mark.asyncio
    async def test_keyword_in_description_also_matches(self):
        """Keywords in description (not just title) trigger the correct scheme."""
        agent = MastermindAgent(self.settings)

        # 'feature' is only in description — title has no keyword
        task = make_task(
            title="Refactor the authentication module",
            description="Add a new feature for OAuth2 support",
        )
        scheme_id = await agent._select_scheme(task)
        assert scheme_id == "feature_standard"

        # 'bug' only in description
        task2 = make_task(
            title="Improve reliability",
            description="There is a bug in the session handling code",
        )
        scheme_id2 = await agent._select_scheme(task2)
        assert scheme_id2 == "bugfix_standard"


# ---------------------------------------------------------------------------
# TestDossierBuilding
# ---------------------------------------------------------------------------


class TestDossierBuilding:
    """Verify _build_dossier behaves correctly for different scheme shapes."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_gcs):
        self.settings = integration_settings
        self.mock_gcs = mock_gcs

    @pytest.mark.asyncio
    async def test_dossier_built_from_first_agentic_node(self):
        """_build_dossier uses the first agentic node's DossierRequirement."""
        agent = MastermindAgent(self.settings)
        task = make_task(title="Fix the login bug")
        scheme_graph = make_agentic_scheme()

        # Patch DossierBuilder.build so we don't hit GCP
        with patch("henchmen.mastermind.agent.DossierBuilder") as mock_builder_cls:
            mock_builder_instance = AsyncMock()
            expected_dossier = Dossier(task_id=task.id, relevant_files=["src/auth.py"])
            mock_builder_instance.build = AsyncMock(return_value=expected_dossier)
            mock_builder_cls.return_value = mock_builder_instance

            dossier = await agent._build_dossier(task, scheme_graph)

        assert dossier.task_id == task.id
        assert dossier.relevant_files == ["src/auth.py"]
        # build() should have been called with the DossierRequirement from the agentic node
        mock_builder_instance.build.assert_called_once()
        call_args = mock_builder_instance.build.call_args
        assert call_args[0][0] is task  # first positional arg is the task
        dossier_req = call_args[0][1]
        assert dossier_req.fetch_files is True

    @pytest.mark.asyncio
    async def test_dossier_fallback_when_no_agentic_nodes(self):
        """_build_dossier returns a minimal Dossier when no agentic nodes have requirements."""
        agent = MastermindAgent(self.settings)
        task = make_task(title="Fix the login bug")
        scheme_graph = make_simple_scheme()  # all deterministic, no dossier_requirement

        dossier = await agent._build_dossier(task, scheme_graph)

        assert dossier.task_id == task.id
        assert dossier.rule_files == []
        assert dossier.relevant_files == []

    @pytest.mark.asyncio
    async def test_dossier_has_correct_task_id(self):
        """Dossier.task_id matches the input task ID."""
        agent = MastermindAgent(self.settings)
        task = make_task(title="Fix some bug")
        scheme_graph = make_simple_scheme()

        dossier = await agent._build_dossier(task, scheme_graph)
        assert dossier.task_id == task.id


# ---------------------------------------------------------------------------
# TestSchemeExecution
# ---------------------------------------------------------------------------


class TestSchemeExecution:
    """Verify SchemeExecutor walks the DAG correctly."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings):
        self.settings = integration_settings

    def make_mock_lair_manager(self) -> LairManager:
        """Return a LairManager with create_lair and wait_for_completion mocked."""
        mock_lm = MagicMock(spec=LairManager)
        mock_lm.create_lair = AsyncMock(return_value="mock-lair-id")
        return mock_lm

    @pytest.mark.asyncio
    async def test_linear_deterministic_execution(self):
        """SchemeExecutor walks a linear DAG of deterministic nodes; all return pass."""
        task = make_task(title="Fix bug")
        dossier = Dossier(task_id=task.id)
        scheme_graph = make_simple_scheme()
        mock_lm = self.make_mock_lair_manager()

        executor = SchemeExecutor(scheme_graph, mock_lm, self.settings)
        report = await executor.execute(task, dossier)

        assert "node_results" in report
        # Both nodes should have been executed
        assert "create_branch" in report["node_results"]
        assert "create_pr" in report["node_results"]
        # Should have a PR URL from the create_pr handler
        assert report.get("pr_url") is not None

    @pytest.mark.asyncio
    async def test_agentic_node_dispatches_to_lair_manager(self):
        """When an agentic node is reached, LairManager.create_lair is called."""
        task = make_task(title="Fix bug")
        dossier = Dossier(task_id=task.id)
        scheme_graph = make_agentic_scheme()

        mock_lm = self.make_mock_lair_manager()
        report_obj = make_operative_report(task.id, "implement_fix", "agentic_scheme")
        mock_lm.wait_for_completion = AsyncMock(return_value=report_obj)

        executor = SchemeExecutor(scheme_graph, mock_lm, self.settings)
        await executor.execute(task, dossier)

        mock_lm.create_lair.assert_called_once()
        call_kwargs = mock_lm.create_lair.call_args
        # First positional arg should be the task, second the node
        assert call_kwargs[0][0] is task
        assert call_kwargs[0][1].id == "implement_fix"

    @pytest.mark.asyncio
    async def test_pass_branch_followed_after_success(self):
        """After a node returns pass, the 'pass' edge is followed."""
        task = make_task(title="Fix bug")
        dossier = Dossier(task_id=task.id)
        scheme_graph = make_branching_scheme()
        mock_lm = self.make_mock_lair_manager()

        executor = SchemeExecutor(scheme_graph, mock_lm, self.settings)
        # run_tests handler returns {"condition": "pass"} by default
        report = await executor.execute(task, dossier)

        # The pass branch leads to create_pr
        assert "create_pr" in report["node_results"]
        assert "escalate" not in report["node_results"]
        assert report.get("pr_url") is not None

    @pytest.mark.asyncio
    async def test_fail_branch_followed_after_failure(self):
        """After a node returns fail, the 'fail' edge is followed."""
        task = make_task(title="Fix bug")
        dossier = Dossier(task_id=task.id)
        scheme_graph = make_branching_scheme()
        mock_lm = self.make_mock_lair_manager()

        executor = SchemeExecutor(scheme_graph, mock_lm, self.settings)

        # Patch _handle_run_tests to return fail
        async def fake_run_tests(node, task, dossier):
            return {"condition": "fail", "message": "Tests failed"}

        executor._handle_run_tests = fake_run_tests

        report = await executor.execute(task, dossier)

        # The fail branch leads to escalate
        assert "escalate" in report["node_results"]
        assert "create_pr" not in report["node_results"]
        assert report.get("escalated") is True

    @pytest.mark.asyncio
    async def test_escalation_node_reached_on_repeated_failure(self):
        """When test retries fail, the escalate terminal node is reached."""
        task = make_task(title="Fix bug")
        dossier = Dossier(task_id=task.id)

        # Use retry scheme: implement_fix (agentic) → run_tests → run_tests_retry → escalate
        scheme_graph = make_agentic_retry_scheme()
        mock_lm = self.make_mock_lair_manager()
        report_obj = make_operative_report(task.id, "implement_fix", "retry_scheme")
        mock_lm.wait_for_completion = AsyncMock(return_value=report_obj)

        executor = SchemeExecutor(scheme_graph, mock_lm, self.settings)

        # Patch both test handlers to always return fail
        async def always_fail(node, task, dossier):
            return {"condition": "fail", "message": "Tests failed"}

        executor._handle_run_tests = always_fail

        report = await executor.execute(task, dossier)

        assert "escalate" in report["node_results"]
        assert report.get("escalated") is True
        assert "create_pr" not in report["node_results"]

    @pytest.mark.asyncio
    async def test_execution_report_contains_all_node_results(self):
        """Final report has an entry for every executed node."""
        task = make_task(title="Fix bug")
        dossier = Dossier(task_id=task.id)
        scheme_graph = make_simple_scheme()
        mock_lm = self.make_mock_lair_manager()

        executor = SchemeExecutor(scheme_graph, mock_lm, self.settings)
        report = await executor.execute(task, dossier)

        # Simple scheme has create_branch → create_pr
        assert set(report["nodes_executed"]) == {"create_branch", "create_pr"}
        assert len(report["node_results"]) == 2


# ---------------------------------------------------------------------------
# TestStateMachineIntegration
# ---------------------------------------------------------------------------


class TestStateMachineIntegration:
    """Verify TaskStateMachine lifecycle, history, recovery, and error handling."""

    def test_full_lifecycle_happy_path(self):
        """Happy path: RECEIVED → SCHEME_SELECTED → LAIR_PROVISIONED → DOSSIER_BUILT
        → EXECUTING → CI_RUNNING → AWAITING_REVIEW → COMPLETED."""
        sm = TaskStateMachine(task_id="task-001")

        sm.transition(TaskState.SCHEME_SELECTED, {"scheme_id": "bugfix_standard"})
        sm.transition(TaskState.LAIR_PROVISIONED)
        sm.transition(TaskState.DOSSIER_BUILT)
        sm.transition(TaskState.EXECUTING)
        sm.transition(TaskState.CI_RUNNING)
        sm.transition(TaskState.AWAITING_REVIEW)
        sm.transition(TaskState.COMPLETED)

        assert sm.current_state == TaskState.COMPLETED
        # All states (including initial RECEIVED implied by history start) are recorded
        state_sequence = [t.to_state for t in sm.history]
        assert TaskState.SCHEME_SELECTED in state_sequence
        assert TaskState.COMPLETED in state_sequence

    def test_escalation_on_ci_failure(self):
        """After max CI retries, state reaches ESCALATED."""
        sm = TaskStateMachine(task_id="task-002")

        sm.transition(TaskState.SCHEME_SELECTED)
        sm.transition(TaskState.LAIR_PROVISIONED)
        sm.transition(TaskState.DOSSIER_BUILT)
        sm.transition(TaskState.EXECUTING)
        sm.transition(TaskState.CI_RUNNING)
        sm.transition(TaskState.CI_RETRY, {"attempt": 1})
        sm.transition(TaskState.CI_RUNNING)
        sm.transition(TaskState.CI_RETRY, {"attempt": 2})
        sm.transition(TaskState.CI_RUNNING)
        sm.transition(TaskState.ESCALATED, {"reason": "CI failed after max retries"})

        assert sm.current_state == TaskState.ESCALATED

    def test_crash_recovery_resumes_from_last_state(self):
        """Serialize SM at EXECUTING, deserialize, verify get_recovery_state returns EXECUTING."""
        sm = TaskStateMachine(task_id="task-003")
        sm.transition(TaskState.SCHEME_SELECTED)
        sm.transition(TaskState.LAIR_PROVISIONED)
        sm.transition(TaskState.DOSSIER_BUILT)
        sm.transition(TaskState.EXECUTING)

        serialized = sm.to_dict()

        # Simulate a crash and deserialization
        recovered_sm = TaskStateMachine.from_dict(serialized)
        assert recovered_sm.current_state == TaskState.EXECUTING
        recovery_state = recovered_sm.get_recovery_state()
        assert recovery_state == TaskState.EXECUTING

    def test_state_history_records_all_transitions(self):
        """Every transition appears in history with timestamps."""
        sm = TaskStateMachine(task_id="task-004")
        transitions = [
            (TaskState.SCHEME_SELECTED, {"scheme_id": "bugfix_standard"}),
            (TaskState.LAIR_PROVISIONED, {}),
            (TaskState.DOSSIER_BUILT, {}),
            (TaskState.EXECUTING, {}),
        ]
        for state, meta in transitions:
            sm.transition(state, meta)

        assert len(sm.history) == len(transitions)
        for i, (expected_state, _) in enumerate(transitions):
            assert sm.history[i].to_state == expected_state
            assert isinstance(sm.history[i].timestamp, datetime)

    def test_invalid_transition_raises_error(self):
        """Attempting RECEIVED → COMPLETED raises ValueError."""
        sm = TaskStateMachine(task_id="task-005")
        # SM starts in RECEIVED; COMPLETED is not a valid next state
        with pytest.raises(ValueError, match="Invalid transition"):
            sm.transition(TaskState.COMPLETED)


# ---------------------------------------------------------------------------
# TestMastermindEndToEnd
# ---------------------------------------------------------------------------


class TestMastermindEndToEnd:
    """Full-stack handle_task tests with mocked LairManager and CI."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_gcs):
        self.settings = integration_settings
        # Ensure both real schemes are registered (reload to re-execute module-level calls)
        SchemeRegistry.clear()
        SchemeRegistry.auto_discover()

    @pytest.mark.asyncio
    async def test_handle_task_bugfix_complete_lifecycle(self):
        """Full flow: bugfix keywords → handle_task → COMPLETED with pr_url."""
        task = make_task(
            title="Fix the login bug",
            description="Users are seeing errors when logging in",
        )
        agent = MastermindAgent(self.settings)

        # Mock LairManager methods on the agent's instance
        report_obj = make_operative_report(task.id, "implement_fix", "bugfix_standard")
        agent.lair_manager.create_lair = AsyncMock(return_value="mock-lair-id")
        agent.lair_manager.wait_for_completion = AsyncMock(return_value=report_obj)

        # Mock DossierBuilder so it doesn't need real GCP
        with patch("henchmen.mastermind.agent.DossierBuilder") as mock_builder_cls:
            mock_builder_instance = AsyncMock()
            mock_builder_instance.build = AsyncMock(return_value=Dossier(task_id=task.id))
            mock_builder_cls.return_value = mock_builder_instance

            # Mock _run_ci to return passed
            agent._run_ci = AsyncMock(return_value={"status": "passed"})

            result = await agent.handle_task(task)

        assert result["status"] == "completed"
        assert result["task_id"] == task.id
        assert result["scheme_id"] == "bugfix_standard"
        assert "pr_url" in result["result"]
        assert result["result"]["pr_url"] is not None

    @pytest.mark.asyncio
    async def test_handle_task_escalates_on_unknown_scheme(self):
        """Register no schemes → handle_task → ESCALATED."""
        SchemeRegistry.clear()
        task = make_task(title="Fix the login bug")
        agent = MastermindAgent(self.settings)

        with patch("henchmen.mastermind.agent.DossierBuilder"):
            result = await agent.handle_task(task)

        assert result["status"] == "escalated"

    @pytest.mark.asyncio
    async def test_handle_task_ci_retry_then_pass(self):
        """Mock _run_ci to fail once then pass → CI_RETRY visited, final state COMPLETED."""
        task = make_task(title="Fix the crash in login")
        agent = MastermindAgent(self.settings)

        report_obj = make_operative_report(task.id, "implement_fix", "bugfix_standard")
        agent.lair_manager.create_lair = AsyncMock(return_value="mock-lair-id")
        agent.lair_manager.wait_for_completion = AsyncMock(return_value=report_obj)

        # _run_ci fails on first call, passes on second
        call_count = 0

        async def ci_fail_then_pass(pr_url):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"status": "failed"}
            return {"status": "passed"}

        agent._run_ci = ci_fail_then_pass

        with patch("henchmen.mastermind.agent.DossierBuilder") as mock_builder_cls:
            mock_builder_instance = AsyncMock()
            mock_builder_instance.build = AsyncMock(return_value=Dossier(task_id=task.id))
            mock_builder_cls.return_value = mock_builder_instance

            result = await agent.handle_task(task)

        sm = agent._active_tasks[task.id]
        state_history = [t.to_state for t in sm.history]

        assert TaskState.CI_RETRY in state_history
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_handle_task_ci_max_retries_escalates(self):
        """Mock _run_ci to always fail → ESCALATED after max retries."""
        task = make_task(title="Fix the broken auth flow")
        agent = MastermindAgent(self.settings)

        report_obj = make_operative_report(task.id, "implement_fix", "bugfix_standard")
        agent.lair_manager.create_lair = AsyncMock(return_value="mock-lair-id")
        agent.lair_manager.wait_for_completion = AsyncMock(return_value=report_obj)
        agent._run_ci = AsyncMock(return_value={"status": "failed"})

        with patch("henchmen.mastermind.agent.DossierBuilder") as mock_builder_cls:
            mock_builder_instance = AsyncMock()
            mock_builder_instance.build = AsyncMock(return_value=Dossier(task_id=task.id))
            mock_builder_cls.return_value = mock_builder_instance

            result = await agent.handle_task(task)

        assert result["status"] == "escalated"
        sm = agent._active_tasks[task.id]
        assert sm.current_state == TaskState.ESCALATED
