"""Integration tests for Mastermind orchestration.

Focused on the three seams that sit between ``MastermindAgent`` and the
scheme executor handlers:

  * ``_select_scheme`` — keyword matching into the scheme registry
  * ``_build_dossier`` — extracting the first agentic node's
    ``DossierRequirement`` and invoking :class:`DossierBuilder`
  * ``SchemeExecutor.execute`` — DAG walk over deterministic + agentic
    nodes, with pass/fail branches driven by real handlers

Full-lifecycle ``handle_task`` flows live in ``test_end_to_end.py``; that
duplication was removed when the quarantines were lifted. The former
``TestStateMachineIntegration`` class covered an in-memory
``TaskStateMachine`` that was deleted in the 2026-04-09 E1 remediation.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.mastermind.agent import MastermindAgent
from henchmen.mastermind.lair_manager import LairManager
from henchmen.mastermind.scheme_executor import SchemeExecutor
from henchmen.mastermind.scheme_executor import handlers as scheme_handlers
from henchmen.models.dossier import Dossier, RuleFile
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

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

REPO = "acme-org/sample-repo"


def make_task(title: str, description: str = "") -> HenchmenTask:
    """Build a minimal HenchmenTask."""
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
    task_id: str,
    node_id: str,
    scheme_id: str = "test_scheme",
    status: OperativeStatus = OperativeStatus.COMPLETED,
) -> OperativeReport:
    """Build an OperativeReport with files_changed populated for the diff evaluator."""
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
        files_changed=["src/example.py"],
    )


def make_simple_scheme(scheme_id: str = "simple_scheme") -> SchemeGraph:
    """Build a minimal two-node linear deterministic scheme."""
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
    """Build a scheme with pass/fail edges for branch routing tests."""
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
    """Build a scheme with one agentic node followed by a PR creation node."""
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


# ---------------------------------------------------------------------------
# TestSchemeSelection
# ---------------------------------------------------------------------------


class TestSchemeSelection:
    """Verify ``_select_scheme`` picks the correct scheme_id from task content."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings):
        self.settings = integration_settings

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "title",
        ["bug report", "fix this", "error in auth", "app crash", "broken login"],
    )
    async def test_bugfix_keywords_select_bugfix_standard(self, title):
        agent = MastermindAgent(self.settings)
        task = make_task(title=title)
        scheme_id = await agent._select_scheme(task)
        assert scheme_id == "bugfix_standard"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "title",
        ["new feature", "add endpoint", "implement oauth", "create widget", "build dashboard"],
    )
    async def test_feature_keywords_select_feature_standard(self, title):
        agent = MastermindAgent(self.settings)
        task = make_task(title=title)
        scheme_id = await agent._select_scheme(task)
        assert scheme_id == "feature_standard"

    @pytest.mark.asyncio
    async def test_ambiguous_defaults_to_bugfix(self):
        """A task with no matching keywords defaults to bugfix_standard."""
        agent = MastermindAgent(self.settings)
        # Avoid words that match goal_keywords (improve/optimize/refactor all/...)
        task = make_task(title="Update documentation", description="Revise README layout")
        scheme_id = await agent._select_scheme(task)
        assert scheme_id == "bugfix_standard"

    @pytest.mark.asyncio
    async def test_keyword_in_description_also_matches(self):
        """Keywords appearing in description (not title) still match."""
        agent = MastermindAgent(self.settings)

        # Title has no feature keyword; description has "feature" and "add"
        task = make_task(
            title="Rework the authentication module",
            description="Add a new feature for OAuth2 support",
        )
        assert await agent._select_scheme(task) == "feature_standard"

        # "Improve" would route to goal_decomposition; use a neutral title
        # with a bug keyword only in the description.
        task2 = make_task(
            title="Reliability work",
            description="There is a bug in the session handling code",
        )
        assert await agent._select_scheme(task2) == "bugfix_standard"


# ---------------------------------------------------------------------------
# TestDossierBuilding
# ---------------------------------------------------------------------------


class TestDossierBuilding:
    """Verify ``_build_dossier`` extracts the right requirement from the scheme graph."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_gcs):
        self.settings = integration_settings

    @pytest.mark.asyncio
    async def test_dossier_built_from_first_agentic_node(self):
        """_build_dossier calls DossierBuilder.build with the first agentic node's requirement.

        ``_build_dossier`` merges ``rule_files`` and ``related_prs`` from the
        builder result into its own Dossier; ``relevant_files`` is populated
        from the GitHub tree fetch (which is skipped here without a GITHUB_TOKEN).
        We assert on the merge targets the agent actually uses.
        """
        agent = MastermindAgent(self.settings)
        task = make_task(title="Fix the login bug")
        scheme_graph = make_agentic_scheme()

        with patch("henchmen.mastermind.agent.DossierBuilder") as mock_builder_cls:
            mock_builder_instance = AsyncMock()
            expected = Dossier(
                task_id=task.id,
                rule_files=[
                    RuleFile(path="CLAUDE.md", scope="/", content="rules 1"),
                    RuleFile(path="AGENTS.md", scope="/", content="rules 2"),
                ],
            )
            mock_builder_instance.build = AsyncMock(return_value=expected)
            mock_builder_cls.return_value = mock_builder_instance

            dossier = await agent._build_dossier(task, scheme_graph)

        assert dossier.task_id == task.id
        assert [rf.path for rf in dossier.rule_files] == ["CLAUDE.md", "AGENTS.md"]
        mock_builder_instance.build.assert_called_once()
        call_args = mock_builder_instance.build.call_args
        assert call_args[0][0] is task
        dossier_req = call_args[0][1]
        assert dossier_req.fetch_files is True

    @pytest.mark.asyncio
    async def test_dossier_fallback_when_no_agentic_nodes(self):
        """_build_dossier returns a minimal Dossier when the scheme has no agentic nodes."""
        agent = MastermindAgent(self.settings)
        task = make_task(title="Fix the login bug")
        scheme_graph = make_simple_scheme()

        dossier = await agent._build_dossier(task, scheme_graph)

        assert dossier.task_id == task.id
        assert dossier.rule_files == []
        assert dossier.relevant_files == []

    @pytest.mark.asyncio
    async def test_dossier_has_correct_task_id(self):
        """The returned Dossier's task_id matches the input task ID."""
        agent = MastermindAgent(self.settings)
        task = make_task(title="Fix some bug")
        scheme_graph = make_simple_scheme()

        dossier = await agent._build_dossier(task, scheme_graph)
        assert dossier.task_id == task.id


# ---------------------------------------------------------------------------
# TestSchemeExecution
# ---------------------------------------------------------------------------


class TestSchemeExecution:
    """Verify SchemeExecutor walks the DAG correctly against real handlers."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings):
        self.settings = integration_settings

    def make_mock_lair_manager(self) -> LairManager:
        """Return a LairManager with async create_lair mocked."""
        mock_lm = MagicMock(spec=LairManager)
        mock_lm.create_lair = AsyncMock(return_value="mock-lair-id")
        return mock_lm

    @pytest.mark.asyncio
    async def test_linear_deterministic_execution(self):
        """SchemeExecutor walks a linear deterministic DAG and reports both node results."""
        task = make_task(title="Fix bug")
        dossier = Dossier(task_id=task.id)
        executor = SchemeExecutor(make_simple_scheme(), self.make_mock_lair_manager(), self.settings)

        report = await executor.execute(task, dossier)

        assert "create_branch" in report["node_results"]
        assert "create_pr" in report["node_results"]
        assert report.get("pr_url") is not None

    @pytest.mark.asyncio
    async def test_agentic_node_dispatches_to_lair_manager(self):
        """When an agentic node is reached, LairManager.create_lair is invoked."""
        task = make_task(title="Fix bug")
        dossier = Dossier(task_id=task.id)

        mock_lm = self.make_mock_lair_manager()
        report_obj = make_operative_report(task.id, "implement_fix", "agentic_scheme")
        mock_lm.wait_for_completion = AsyncMock(return_value=report_obj)

        executor = SchemeExecutor(make_agentic_scheme(), mock_lm, self.settings)
        await executor.execute(task, dossier)

        mock_lm.create_lair.assert_called_once()
        call_args = mock_lm.create_lair.call_args
        assert call_args[0][0] is task
        assert call_args[0][1].id == "implement_fix"

    @pytest.mark.asyncio
    async def test_pass_branch_followed_after_success(self):
        """When run_tests returns pass, the executor takes the create_pr branch."""
        task = make_task(title="Fix bug")
        dossier = Dossier(task_id=task.id)
        executor = SchemeExecutor(make_branching_scheme(), self.make_mock_lair_manager(), self.settings)

        async def fake_run_tests(executor_, node, task_, dossier_):
            return {"condition": "pass", "message": "Tests passed"}

        with patch.dict(scheme_handlers._HANDLERS, {"run_tests": fake_run_tests}):
            report = await executor.execute(task, dossier)

        assert "create_pr" in report["node_results"]
        assert "escalate" not in report["node_results"]
        assert report.get("pr_url") is not None

    @pytest.mark.asyncio
    async def test_fail_branch_followed_after_failure(self):
        """When run_tests returns fail, the executor takes the escalate branch."""
        task = make_task(title="Fix bug")
        dossier = Dossier(task_id=task.id)
        executor = SchemeExecutor(make_branching_scheme(), self.make_mock_lair_manager(), self.settings)

        async def fake_run_tests(executor_, node, task_, dossier_):
            return {"condition": "fail", "message": "Tests failed"}

        with patch.dict(scheme_handlers._HANDLERS, {"run_tests": fake_run_tests}):
            report = await executor.execute(task, dossier)

        assert "escalate" in report["node_results"]
        assert "create_pr" not in report["node_results"]

    @pytest.mark.asyncio
    async def test_execution_report_contains_all_node_results(self):
        """Final report has an entry for every executed node."""
        task = make_task(title="Fix bug")
        dossier = Dossier(task_id=task.id)
        executor = SchemeExecutor(make_simple_scheme(), self.make_mock_lair_manager(), self.settings)

        report = await executor.execute(task, dossier)

        assert set(report["nodes_executed"]) == {"create_branch", "create_pr"}
        assert len(report["node_results"]) == 2
