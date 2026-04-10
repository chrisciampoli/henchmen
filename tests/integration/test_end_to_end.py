"""End-to-end integration tests for the full Henchmen pipeline.

Exercises the task-source normalizer -> ``MastermindAgent.handle_task``
path with ``SchemeExecutor.execute`` stubbed out so these tests focus on
orchestration wiring rather than the downstream DAG walk (which has its
own dedicated unit coverage). Dossier + LairManager boundaries are
mocked, and a minimal ``_MockBroker`` is injected into the agent so
Pub/Sub publish calls are observable without any real provider.

Repo slugs used in fixture data are deliberately generic (``acme-org/sample-repo``)
so no real target repository is referenced.
"""

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.mastermind.agent import MastermindAgent
from henchmen.models.dossier import Dossier
from henchmen.models.operative import OperativeReport, OperativeStatus
from henchmen.models.task import HenchmenTask, TaskContext, TaskPriority, TaskSource
from henchmen.schemes.registry import SchemeRegistry

REPO = "acme-org/sample-repo"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _MockBroker:
    """Minimal MessageBroker double that records every publish call."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.publish = AsyncMock(side_effect=self._record)

    async def _record(self, topic: str, data: bytes, **attributes: Any) -> str:
        self.published.append(
            {
                "topic": topic,
                "data": json.loads(data.decode("utf-8")) if data else None,
                "attributes": attributes,
            }
        )
        return "mock-msg-id"

    def messages_for(self, topic_fragment: str) -> list[dict[str, Any]]:
        return [m for m in self.published if topic_fragment in m["topic"]]


def _make_operative_report(
    task_id: str,
    node_id: str,
    scheme_id: str,
    status: OperativeStatus = OperativeStatus.COMPLETED,
) -> OperativeReport:
    """Build an OperativeReport with files_changed set (required by the L8 diff evaluator)."""
    now = datetime.now(UTC)
    return OperativeReport(
        task_id=task_id,
        scheme_id=scheme_id,
        node_id=node_id,
        operative_id=f"mock-lair-{node_id}",
        status=status,
        summary=f"Node {node_id} finished ({status.value})",
        confidence_score=0.9 if status == OperativeStatus.COMPLETED else 0.1,
        started_at=now,
        completed_at=now,
        files_changed=["src/example.py"],
    )


def _canned_success_result() -> dict[str, Any]:
    """Stub SchemeExecutor.execute return value for the happy path."""
    return {
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
        "pr_url": "https://github.com/acme-org/sample-repo/pull/1",
        "node_results": {"create_pr": {"pr_url": "https://github.com/acme-org/sample-repo/pull/1", "pr_number": 1}},
    }


def _make_agent(settings) -> tuple[MastermindAgent, _MockBroker]:
    """Build a MastermindAgent wired to a mock broker and stub lair manager."""
    broker = _MockBroker()
    agent = MastermindAgent(settings=settings, broker=broker)
    agent.lair_manager = AsyncMock()
    agent.lair_manager.create_lair = AsyncMock(return_value="mock-lair-id")
    return agent, broker


def _dossier_patch():
    """Return a context manager that stubs ``DossierBuilder`` inside agent.py."""

    class _Patch:
        def __enter__(self) -> Any:
            self._p = patch("henchmen.mastermind.agent.DossierBuilder")
            mock_cls = self._p.start()
            mock_instance = AsyncMock()
            mock_instance.build = AsyncMock(return_value=Dossier(task_id="stub"))
            mock_cls.return_value = mock_instance
            return mock_cls

        def __exit__(self, *exc: Any) -> None:
            self._p.stop()

    return _Patch()


# ===========================================================================
# TestCLIToCompletionPipeline
# ===========================================================================


@pytest.fixture(autouse=True)
def _disable_vertex_evaluation(integration_settings, monkeypatch):
    """Ensure VertexAI evaluation is off for every test in this module."""
    monkeypatch.setattr(integration_settings, "vertex_ai_evaluation_enabled", False, raising=False)


class TestCLIToCompletionPipeline:
    """Full flow starting from CLI task submission."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_gcs):
        self.settings = integration_settings
        SchemeRegistry.clear()
        SchemeRegistry.auto_discover()

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.scheme_executor.executor.SchemeExecutor.execute", new_callable=AsyncMock)
    async def test_cli_bugfix_task_reaches_completed_state(self, mock_execute, cli_task_data):
        """CLI bugfix task -> handle_task -> scheme_id=bugfix_standard, status=completed."""
        mock_execute.return_value = _canned_success_result()

        normalizer = TaskNormalizer()
        task = normalizer.from_cli(cli_task_data)
        assert task.source == TaskSource.CLI

        agent, _ = _make_agent(self.settings)
        with _dossier_patch():
            result = await agent.handle_task(task)

        assert result["scheme_id"] == "bugfix_standard"
        assert result["status"] == "completed"
        assert result["result"]["pr_url"].endswith("/pull/1")

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.scheme_executor.executor.SchemeExecutor.execute", new_callable=AsyncMock)
    async def test_cli_feature_task_selects_feature_scheme(self, mock_execute):
        """CLI task with 'implement' keyword -> feature_standard scheme."""
        mock_execute.return_value = _canned_success_result()

        cli_data = {
            "title": "Implement OAuth2 login flow",
            "description": "Add support for OAuth2 authentication",
            "repo": REPO,
            "branch": "main",
            "priority": "normal",
            "created_by": "developer@acme.com",
        }

        normalizer = TaskNormalizer()
        task = normalizer.from_cli(cli_data)

        agent, _ = _make_agent(self.settings)
        with _dossier_patch():
            result = await agent.handle_task(task)

        assert result["scheme_id"] == "feature_standard"
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_cli_task_publishes_to_pubsub_on_dispatch(self, cli_task_data):
        """normalize + publish_task -> message on the task-intake topic via the injected broker."""
        normalizer = TaskNormalizer()
        task = normalizer.from_cli(cli_task_data)

        broker = _MockBroker()
        await normalizer.publish_task(task, self.settings, broker=broker)

        intake_msgs = broker.messages_for("task-intake")
        assert len(intake_msgs) == 1
        data = intake_msgs[0]["data"]
        assert data["source"] == "cli"
        assert data["title"] == cli_task_data["title"]
        assert data["context"]["repo"] == cli_task_data["repo"]


# ===========================================================================
# TestSlackToCompletionPipeline
# ===========================================================================


class TestSlackToCompletionPipeline:
    """Full flow starting from a Slack app_mention event."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_gcs):
        self.settings = integration_settings
        SchemeRegistry.clear()
        SchemeRegistry.auto_discover()

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.scheme_executor.executor.SchemeExecutor.execute", new_callable=AsyncMock)
    async def test_slack_mention_to_completed(self, mock_execute, slack_event_data):
        """Slack app_mention -> normalize -> handle_task -> completed."""
        mock_execute.return_value = _canned_success_result()

        normalizer = TaskNormalizer()
        task = normalizer.from_slack(slack_event_data)
        assert task.source == TaskSource.SLACK

        agent, _ = _make_agent(self.settings)
        with _dossier_patch():
            result = await agent.handle_task(task)

        assert result["status"] == "completed"
        assert result["task_id"] == task.id


# ===========================================================================
# TestGitHubToCompletionPipeline
# ===========================================================================


class TestGitHubToCompletionPipeline:
    """Full flow starting from GitHub webhook events."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_gcs):
        self.settings = integration_settings
        SchemeRegistry.clear()
        SchemeRegistry.auto_discover()

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.scheme_executor.executor.SchemeExecutor.execute", new_callable=AsyncMock)
    async def test_github_issue_labeled_to_completed(self, mock_execute, github_issue_event):
        """GitHub issue labeled 'henchmen' -> normalize -> handle_task -> completed."""
        mock_execute.return_value = _canned_success_result()

        normalizer = TaskNormalizer()
        task = normalizer.from_github(github_issue_event)
        assert task.source == TaskSource.GITHUB

        agent, _ = _make_agent(self.settings)
        with _dossier_patch():
            result = await agent.handle_task(task)

        assert result["status"] == "completed"

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.scheme_executor.executor.SchemeExecutor.execute", new_callable=AsyncMock)
    async def test_github_pr_comment_to_completed(self, mock_execute, github_pr_comment_event):
        """PR comment with @henchmen -> normalize -> handle_task -> completed."""
        mock_execute.return_value = _canned_success_result()

        normalizer = TaskNormalizer()
        task = normalizer.from_github(github_pr_comment_event)
        assert task.source == TaskSource.GITHUB
        assert task.context.branch == "feature/auth-update"

        agent, _ = _make_agent(self.settings)
        with _dossier_patch():
            result = await agent.handle_task(task)

        assert result["status"] == "completed"


# ===========================================================================
# TestFailureAndEscalation
# ===========================================================================


class TestFailureAndEscalation:
    """Tests for failure branches and escalation paths."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_gcs):
        self.settings = integration_settings
        SchemeRegistry.clear()
        SchemeRegistry.auto_discover()

    def _make_task(self, title: str = "Fix the login bug") -> HenchmenTask:
        return HenchmenTask(
            source=TaskSource.CLI,
            source_id="e2e-failure-test",
            title=title,
            description="Login endpoint returns 500 for special chars",
            context=TaskContext(repo=REPO, branch="main"),
            priority=TaskPriority.NORMAL,
            created_by="tester@acme.com",
        )

    @pytest.mark.asyncio
    async def test_explicit_fail_edge_takes_fail_branch(self):
        """Build a synthetic scheme with explicit fail/pass edges and verify routing.

        This exercises the real SchemeExecutor against a minimal three-node
        graph. The root's deterministic handler is patched to return
        ``{"condition": "fail"}``; the executor must follow the fail edge
        to 'bad' and skip 'good'.
        """
        from henchmen.mastermind.scheme_executor import SchemeExecutor
        from henchmen.models.scheme import (
            NodeType,
            SchemeDefinition,
            SchemeEdge,
            SchemeNode,
        )
        from henchmen.schemes.base import SchemeGraph

        definition = SchemeDefinition(
            id="fail_edge_test",
            name="Fail Edge Test",
            description="Minimal scheme to exercise explicit fail edges",
            version="1.0.0",
            nodes=[
                SchemeNode(id="root", name="Root", node_type=NodeType.DETERMINISTIC, timeout_seconds=30),
                SchemeNode(id="good", name="Good", node_type=NodeType.DETERMINISTIC, timeout_seconds=30),
                SchemeNode(id="bad", name="Bad", node_type=NodeType.DETERMINISTIC, timeout_seconds=30),
            ],
            edges=[
                SchemeEdge(from_node="root", to_node="good", condition="pass"),
                SchemeEdge(from_node="root", to_node="bad", condition="fail"),
            ],
        )
        graph = SchemeGraph(definition)

        task = self._make_task()
        dossier = Dossier(task_id=task.id)
        executor = SchemeExecutor(graph, MagicMock(), self.settings)

        async def fake_deterministic(node, t, d):
            if node.id == "root":
                return {"condition": "fail", "message": "synthetic failure"}
            return {"condition": None, "message": f"{node.id} no-op"}

        executor._execute_deterministic = fake_deterministic  # type: ignore[method-assign]

        result = await executor.execute(task, dossier)

        assert "bad" in result["nodes_executed"], f"Expected fail branch ('bad') to run, got {result['nodes_executed']}"
        assert "good" not in result["nodes_executed"], "Fail branch should have bypassed pass branch"

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.agent.DossierBuilder")
    async def test_unknown_scheme_escalates_immediately(self, mock_builder_cls):
        """Clear scheme registry -> handle_task -> escalated immediately."""
        mock_instance = AsyncMock()
        mock_instance.build = AsyncMock(return_value=Dossier(task_id="t"))
        mock_builder_cls.return_value = mock_instance

        SchemeRegistry.clear()
        task = self._make_task()
        agent, _ = _make_agent(self.settings)

        result = await agent.handle_task(task)

        assert result["status"] == "escalated"
        assert "Unknown scheme" in result.get("reason", "")

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.scheme_executor.executor.SchemeExecutor.execute", new_callable=AsyncMock)
    async def test_scheme_executor_escalation_propagates(self, mock_execute):
        """When SchemeExecutor returns final_status='escalated', handle_task propagates it."""
        mock_execute.return_value = {
            "final_status": "escalated",
            "nodes_executed": ["create_branch", "implement_fix"],
            "escalated": True,
            "pr_url": "",
        }

        task = self._make_task()
        agent, _ = _make_agent(self.settings)
        with _dossier_patch():
            result = await agent.handle_task(task)

        assert result["status"] == "escalated"

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.agent.DossierBuilder")
    async def test_exception_during_scheme_selection_escalates(self, mock_builder_cls):
        """If _select_scheme raises, handle_task should return escalated with the error."""
        task = self._make_task()
        agent, _ = _make_agent(self.settings)
        agent._select_scheme = AsyncMock(side_effect=RuntimeError("boom"))

        result = await agent.handle_task(task)

        assert result["status"] == "escalated"
        assert "boom" in result.get("error", "")


# ===========================================================================
# TestMultiTaskConcurrency
# ===========================================================================


class TestMultiTaskConcurrency:
    """Tests for concurrent task handling at the agent level."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_gcs):
        self.settings = integration_settings
        SchemeRegistry.clear()
        SchemeRegistry.auto_discover()

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.scheme_executor.executor.SchemeExecutor.execute", new_callable=AsyncMock)
    @patch("henchmen.mastermind.agent.DossierBuilder")
    async def test_two_tasks_get_independent_results(self, mock_builder_cls, mock_execute):
        """Submit two tasks -> both complete with distinct task IDs in _active_tasks."""
        mock_execute.return_value = _canned_success_result()
        mock_instance = AsyncMock()
        mock_instance.build = AsyncMock(return_value=Dossier(task_id="stub"))
        mock_builder_cls.return_value = mock_instance

        tasks = [
            HenchmenTask(
                source=TaskSource.CLI,
                source_id=f"e2e-multi-{i}",
                title=title,
                description="Concurrent test",
                context=TaskContext(repo=REPO, branch="main"),
                priority=TaskPriority.NORMAL,
                created_by="tester@acme.com",
            )
            for i, title in enumerate(["Fix bug in auth", "Fix error in payments"])
        ]

        agent, _ = _make_agent(self.settings)

        results = [await agent.handle_task(t) for t in tasks]

        assert results[0]["status"] == "completed"
        assert results[1]["status"] == "completed"
        assert results[0]["task_id"] != results[1]["task_id"]
        assert results[0]["task_id"] == tasks[0].id
        assert results[1]["task_id"] == tasks[1].id
        # Both tasks should still appear in the local active-task hint set.
        assert tasks[0].id in agent._active_tasks
        assert tasks[1].id in agent._active_tasks


# ===========================================================================
# TestBrokerPublishBehavior
# ===========================================================================


class TestBrokerPublishBehavior:
    """Observe the Pub/Sub publish side-effects of handle_task via the injected broker."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_gcs):
        self.settings = integration_settings
        SchemeRegistry.clear()
        SchemeRegistry.auto_discover()

    def _make_task(self) -> HenchmenTask:
        return HenchmenTask(
            source=TaskSource.CLI,
            source_id="e2e-broker-test",
            title="Fix the login bug",
            description="Login endpoint returns 500",
            context=TaskContext(repo=REPO, branch="main"),
            priority=TaskPriority.NORMAL,
            created_by="tester@acme.com",
        )

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.scheme_executor.executor.SchemeExecutor.execute", new_callable=AsyncMock)
    async def test_handle_task_publishes_forge_request_when_pr_created(self, mock_execute):
        """When the scheme executor returns a real PR URL, handle_task should publish to forge-request."""
        mock_execute.return_value = _canned_success_result()  # contains pull/1 URL

        task = self._make_task()
        agent, broker = _make_agent(self.settings)

        with _dossier_patch():
            result = await agent.handle_task(task)

        assert result["status"] == "completed"
        forge_msgs = broker.messages_for("forge-request")
        assert len(forge_msgs) == 1
        assert "pull/1" in forge_msgs[0]["data"]["pr_url"]
        assert forge_msgs[0]["data"]["action"] == "run_ci"

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.scheme_executor.executor.SchemeExecutor.execute", new_callable=AsyncMock)
    async def test_handle_task_skips_forge_request_without_pr(self, mock_execute):
        """When no PR is produced, handle_task should NOT publish to forge-request."""
        mock_execute.return_value = {
            "final_status": "completed",
            "nodes_executed": ["prefetch_context"],
            "pr_url": "",
            "node_results": {},
        }

        task = self._make_task()
        agent, broker = _make_agent(self.settings)

        with _dossier_patch():
            await agent.handle_task(task)

        forge_msgs = broker.messages_for("forge-request")
        assert forge_msgs == []
