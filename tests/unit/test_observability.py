"""Unit tests for the observability module."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Cost calculator
# ---------------------------------------------------------------------------


class TestEstimateCost:
    def test_claude_sonnet_cost(self):
        from henchmen.observability.tracker import estimate_cost

        cost = estimate_cost("claude-sonnet-4@20250514", 100_000, 5_000)
        assert cost == pytest.approx(0.375, abs=0.001)

    def test_gemini_cost(self):
        from henchmen.observability.tracker import estimate_cost

        cost = estimate_cost("gemini-2.5-pro", 100_000, 5_000)
        assert cost == pytest.approx(0.175, abs=0.001)

    def test_unknown_model_returns_zero(self):
        from henchmen.observability.tracker import estimate_cost

        cost = estimate_cost("unknown-model-v1", 100_000, 5_000)
        assert cost == 0.0

    def test_zero_tokens(self):
        from henchmen.observability.tracker import estimate_cost

        cost = estimate_cost("claude-sonnet-4@20250514", 0, 0)
        assert cost == 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task():
    from henchmen.models.task import HenchmenTask, TaskContext, TaskSource

    return HenchmenTask(
        source=TaskSource.SLACK,
        source_id="C01234567/1700000000.000001",
        title="Fix login bug",
        description="Users cannot log in",
        context=TaskContext(repo="org/repo"),
        created_by="user1",
    )


def _make_report(**kwargs):
    from henchmen.models.operative import OperativeReport, OperativeStatus

    defaults = {
        "task_id": "test-task",
        "scheme_id": "bugfix_standard",
        "node_id": "implement_fix",
        "operative_id": "op-123",
        "status": OperativeStatus.COMPLETED,
        "summary": "Fixed the bug",
        "confidence_score": 0.85,
        "started_at": datetime(2026, 3, 28, 12, 0, 0, tzinfo=UTC),
        "completed_at": datetime(2026, 3, 28, 12, 5, 0, tzinfo=UTC),
        "total_input_tokens": 100_000,
        "total_output_tokens": 5_000,
        "model_calls": 15,
        "tool_calls_count": 30,
        "tool_calls_detail": {"code_edit": 5, "git_ops": 3},
        "wall_clock_seconds": 300.0,
        "files_changed": ["src/auth.py"],
    }
    defaults.update(kwargs)
    return OperativeReport(**defaults)


def _mock_settings():
    """Build a real ``Settings`` instance with test-safe defaults.

    Seeds ``os.environ`` for the required ``HENCHMEN_GCP_PROJECT_ID``
    field and leverages the autouse ``_isolate_settings`` fixture to
    guarantee a fresh cache on every call.
    """
    import os

    from henchmen.config.settings import get_settings

    os.environ.setdefault("HENCHMEN_GCP_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    return get_settings()


# ---------------------------------------------------------------------------
# TaskTracker
# ---------------------------------------------------------------------------


def _make_mock_store() -> MagicMock:
    """Create a mock DocumentStore with async methods."""
    store = MagicMock()
    store.get = AsyncMock(return_value=None)
    store.set = AsyncMock()
    store.update = AsyncMock()
    store.delete = AsyncMock()
    store.query = AsyncMock(return_value=[])
    store.increment = AsyncMock()
    store.update_if = AsyncMock(return_value=True)
    return store


class TestTaskTracker:
    def _make_tracker(self, store: MagicMock | None = None):  # type: ignore[return]
        from henchmen.observability.tracker import TaskTracker

        settings = _mock_settings()
        mock_store = store or _make_mock_store()
        return TaskTracker(settings, document_store=mock_store)

    @pytest.mark.asyncio
    async def test_start_task_creates_doc(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        task = _make_task()
        await tracker.start_task(task, "bugfix_standard")
        store.set.assert_called_once()
        call_collection, call_id, call_data = store.set.call_args[0]
        assert call_collection == "task_executions"
        assert call_id == task.id
        assert call_data["task_id"] == task.id
        assert call_data["title"] == "Fix login bug"
        assert call_data["scheme_id"] == "bugfix_standard"
        assert call_data["source"] == "slack"
        assert call_data["final_status"] is None
        assert call_data["node_metrics"] == {}
        assert "expires_at" in call_data

    @pytest.mark.asyncio
    async def test_record_node_result_updates_doc(self):
        store = _make_mock_store()
        # Return an empty current doc so the structured-field merge has a baseline.
        store.get = AsyncMock(return_value={})
        tracker = self._make_tracker(store)
        report = _make_report()
        await tracker.record_node_result("test-task", "implement_fix", report)

        # Counter-style fields go through the atomic increment primitive.
        store.increment.assert_called_once()
        _coll, _id, deltas = store.increment.call_args.args
        assert deltas["total_input_tokens"] == 100_000
        assert deltas["total_output_tokens"] == 5_000
        assert deltas["total_model_calls"] == 15
        # estimated_cost_usd is omitted when the cost is 0 (unknown model in this fixture);
        # the increment helper filters out zero deltas to avoid no-op writes.

        # Structured-field merge goes through update.
        store.update.assert_called_once()
        _coll, _id, update_data = store.update.call_args[0]
        node_data = update_data["node_metrics"]["implement_fix"]
        assert node_data["input_tokens"] == 100_000
        assert node_data["output_tokens"] == 5_000
        assert node_data["model_calls"] == 15
        assert node_data["confidence_score"] == 0.85

    @pytest.mark.asyncio
    async def test_record_node_result_empty_files_changed(self):
        store = _make_mock_store()
        store.get = AsyncMock(return_value={})
        tracker = self._make_tracker(store)
        report = _make_report(files_changed=[])
        await tracker.record_node_result("test-task", "verify_changes", report)
        _coll, _id, update_data = store.update.call_args[0]
        # files_changed should NOT be in the update when empty
        assert "files_changed" not in update_data

    @pytest.mark.asyncio
    async def test_record_ci_result(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.record_ci_result("test-task", True)
        _coll, _id, update_data = store.update.call_args[0]
        assert update_data["ci_passed"] is True

    @pytest.mark.asyncio
    async def test_finalize_task(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.finalize_task("test-task", "pr_created", "https://github.com/org/repo/pull/1", 1)
        _coll, _id, update_data = store.update.call_args[0]
        assert update_data["final_status"] == "pr_created"
        assert update_data["pr_url"] == "https://github.com/org/repo/pull/1"
        assert update_data["pr_number"] == 1
        assert "completed_at" in update_data

    @pytest.mark.asyncio
    async def test_firestore_error_does_not_raise(self):
        store = _make_mock_store()
        store.set = AsyncMock(side_effect=Exception("Store down"))
        tracker = self._make_tracker(store)
        task = _make_task()
        await tracker.start_task(task, "bugfix_standard")  # Should not raise

    @pytest.mark.asyncio
    async def test_get_task(self):
        store = _make_mock_store()
        store.get = AsyncMock(return_value={"task_id": "t1", "title": "Test"})
        tracker = self._make_tracker(store)
        result = await tracker.get_task("t1")
        assert result["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_get_task_not_found(self):
        store = _make_mock_store()
        store.get = AsyncMock(return_value=None)
        tracker = self._make_tracker(store)
        result = await tracker.get_task("nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# Metrics API
# ---------------------------------------------------------------------------


class TestMetricsAPI:
    def _make_app(self, mock_tasks):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from henchmen.observability.api import create_metrics_router

        mock_tracker = MagicMock()
        mock_tracker.get_recent_tasks = AsyncMock(return_value=mock_tasks)
        mock_tracker.get_task = AsyncMock(return_value=None)

        app = FastAPI()
        app.include_router(create_metrics_router(mock_tracker))
        return TestClient(app)

    def test_summary_empty(self):
        # K8 fix: ci_pass_rate is None when ci_decided == 0, not 0.0,
        # so alerts of the form `rate < 0.5` do not page when there is no data.
        client = self._make_app([])
        resp = client.get("/metrics/summary?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tasks_total"] == 0
        assert data["ci_pass_rate"] is None

    def test_summary_with_tasks(self):
        tasks = [
            {
                "task_id": "t1",
                "scheme_id": "bugfix_standard",
                "ci_passed": True,
                "estimated_cost_usd": 0.30,
                "wall_clock_seconds": 600,
                "total_input_tokens": 200_000,
                "total_output_tokens": 10_000,
                "confidence_score": 0.9,
            },
            {
                "task_id": "t2",
                "scheme_id": "feature_standard",
                "ci_passed": False,
                "estimated_cost_usd": 0.50,
                "wall_clock_seconds": 900,
                "total_input_tokens": 300_000,
                "total_output_tokens": 15_000,
                "confidence_score": 0.7,
            },
            {
                "task_id": "t3",
                "scheme_id": "bugfix_standard",
                "ci_passed": None,
                "estimated_cost_usd": 0.20,
                "wall_clock_seconds": 400,
                "total_input_tokens": 100_000,
                "total_output_tokens": 5_000,
                "confidence_score": 0.8,
            },
        ]
        client = self._make_app(tasks)
        resp = client.get("/metrics/summary?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tasks_total"] == 3
        assert data["tasks_ci_passed"] == 1
        assert data["tasks_ci_failed"] == 1
        assert data["tasks_ci_pending"] == 1
        assert data["ci_pass_rate"] == pytest.approx(0.5, abs=0.01)
        assert data["total_cost_usd"] == pytest.approx(1.0, abs=0.01)
        assert data["avg_cost_per_task_usd"] == pytest.approx(0.333, abs=0.01)
        assert data["by_scheme"]["bugfix_standard"]["count"] == 2
        assert data["by_scheme"]["feature_standard"]["count"] == 1

    def test_tasks_endpoint(self):
        tasks = [{"task_id": "t1", "title": "Fix bug"}]
        client = self._make_app(tasks)
        resp = client.get("/metrics/tasks?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["task_id"] == "t1"


# ---------------------------------------------------------------------------
# get_metrics_summary
# ---------------------------------------------------------------------------


class TestGetMetricsSummary:
    def _make_tracker(self, tasks: list | None = None):
        from henchmen.observability.tracker import TaskTracker

        settings = _mock_settings()
        store = _make_mock_store()
        store.query = AsyncMock(return_value=tasks or [])
        return TaskTracker(settings, document_store=store)

    @pytest.mark.asyncio
    async def test_empty_returns_zero_total(self):
        tracker = self._make_tracker([])
        result = await tracker.get_metrics_summary(days=7)
        assert result["total_tasks"] == 0
        assert result["days"] == 7

    @pytest.mark.asyncio
    async def test_success_rate_calculation(self):
        tasks = [
            {
                "final_status": "pr_created",
                "estimated_cost_usd": 0.20,
                "total_input_tokens": 10000,
                "total_output_tokens": 1000,
                "node_metrics": {},
            },
            {
                "final_status": "pr_created",
                "estimated_cost_usd": 0.30,
                "total_input_tokens": 20000,
                "total_output_tokens": 2000,
                "node_metrics": {},
            },
            {
                "final_status": "escalated",
                "estimated_cost_usd": 0.10,
                "total_input_tokens": 5000,
                "total_output_tokens": 500,
                "node_metrics": {},
                "escalation_reason": "Stalled",
            },
        ]
        tracker = self._make_tracker(tasks)
        result = await tracker.get_metrics_summary(days=7)
        assert result["total_tasks"] == 3
        assert result["success_rate"] == pytest.approx(2 / 3, abs=0.01)
        assert result["escalation_rate"] == pytest.approx(1 / 3, abs=0.01)

    @pytest.mark.asyncio
    async def test_cost_by_model_aggregation(self):
        tasks = [
            {
                "final_status": "pr_created",
                "estimated_cost_usd": 0.50,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "node_metrics": {
                    "implement_fix": {"model_name": "claude-sonnet-4@20250514", "cost_usd": 0.30},
                    "verify_changes": {"model_name": "gemini-2.5-flash", "cost_usd": 0.05},
                },
            },
            {
                "final_status": "pr_created",
                "estimated_cost_usd": 0.35,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "node_metrics": {
                    "implement_fix": {"model_name": "claude-sonnet-4@20250514", "cost_usd": 0.35},
                },
            },
        ]
        tracker = self._make_tracker(tasks)
        result = await tracker.get_metrics_summary(days=7)
        cbm = result["cost_by_model"]
        assert cbm["claude-sonnet-4@20250514"] == pytest.approx(0.65, abs=0.001)
        assert cbm["gemini-2.5-flash"] == pytest.approx(0.05, abs=0.001)

    @pytest.mark.asyncio
    async def test_escalation_reasons_frequency(self):
        tasks = [
            {
                "final_status": "escalated",
                "estimated_cost_usd": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "node_metrics": {},
                "escalation_reason": "Stalled after 3 attempts",
            },
            {
                "final_status": "escalated",
                "estimated_cost_usd": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "node_metrics": {},
                "escalation_reason": "Stalled after 3 attempts",
            },
            {
                "final_status": "escalated",
                "estimated_cost_usd": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "node_metrics": {},
                "escalation_reason": "CI failed",
            },
        ]
        tracker = self._make_tracker(tasks)
        result = await tracker.get_metrics_summary(days=7)
        er = result["escalation_reasons"]
        assert er["Stalled after 3 attempts"] == 2
        assert er["CI failed"] == 1

    @pytest.mark.asyncio
    async def test_token_totals(self):
        tasks = [
            {
                "final_status": "pr_created",
                "estimated_cost_usd": 0,
                "total_input_tokens": 100000,
                "total_output_tokens": 5000,
                "node_metrics": {},
            },
            {
                "final_status": "pr_created",
                "estimated_cost_usd": 0,
                "total_input_tokens": 200000,
                "total_output_tokens": 10000,
                "node_metrics": {},
            },
        ]
        tracker = self._make_tracker(tasks)
        result = await tracker.get_metrics_summary(days=7)
        assert result["total_tokens"]["input"] == 300000
        assert result["total_tokens"]["output"] == 15000

    @pytest.mark.asyncio
    async def test_firestore_error_returns_zero_total(self):
        # get_recent_tasks silently catches the store error and returns [].
        # get_metrics_summary then sees an empty task list and returns {"total_tasks": 0}.
        store = _make_mock_store()
        store.query = AsyncMock(side_effect=Exception("Store down"))
        from henchmen.observability.tracker import TaskTracker

        tracker = TaskTracker(_mock_settings(), document_store=store)
        result = await tracker.get_metrics_summary(days=7)
        assert result["total_tasks"] == 0
        assert result["days"] == 7

    @pytest.mark.asyncio
    async def test_node_metrics_missing_model_name_uses_unknown(self):
        tasks = [
            {
                "final_status": "pr_created",
                "estimated_cost_usd": 0.10,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "node_metrics": {
                    "implement_fix": {"cost_usd": 0.10},  # no model_name key
                },
            },
        ]
        tracker = self._make_tracker(tasks)
        result = await tracker.get_metrics_summary(days=7)
        assert "unknown" in result["cost_by_model"]
        assert result["cost_by_model"]["unknown"] == pytest.approx(0.10, abs=0.001)


class TestMetricsSummaryEndpoint:
    """Test the /api/v1/metrics/summary FastAPI endpoint."""

    def test_default_days(self):
        from fastapi.testclient import TestClient

        from henchmen.mastermind.server import app

        mock_tracker = MagicMock()
        mock_tracker.get_metrics_summary = AsyncMock(return_value={"total_tasks": 5, "days": 7})
        mock_agent = MagicMock()
        mock_agent.tracker = mock_tracker

        # Keep patch active for the duration of the HTTP request.
        with patch("henchmen.mastermind.server.get_agent", return_value=mock_agent):
            client = TestClient(app)
            resp = client.get("/api/v1/metrics/summary")

        assert resp.status_code == 200
        mock_tracker.get_metrics_summary.assert_called_once_with(7)

    def test_custom_days(self):
        from fastapi.testclient import TestClient

        from henchmen.mastermind.server import app

        mock_tracker = MagicMock()
        mock_tracker.get_metrics_summary = AsyncMock(return_value={"total_tasks": 10, "days": 30})
        mock_agent = MagicMock()
        mock_agent.tracker = mock_tracker

        with patch("henchmen.mastermind.server.get_agent", return_value=mock_agent):
            client = TestClient(app)
            resp = client.get("/api/v1/metrics/summary?days=30")

        assert resp.status_code == 200
        mock_tracker.get_metrics_summary.assert_called_once_with(30)

    def test_response_body(self):
        from fastapi.testclient import TestClient

        from henchmen.mastermind.server import app

        summary = {
            "total_tasks": 3,
            "success_rate": 0.667,
            "escalation_rate": 0.333,
            "avg_cost_usd": 0.25,
            "total_cost_usd": 0.75,
            "total_tokens": {"input": 150000, "output": 7500},
            "cost_by_model": {"claude-sonnet-4@20250514": 0.60},
            "escalation_reasons": {"Stalled": 1},
            "days": 7,
        }
        mock_tracker = MagicMock()
        mock_tracker.get_metrics_summary = AsyncMock(return_value=summary)
        mock_agent = MagicMock()
        mock_agent.tracker = mock_tracker

        with patch("henchmen.mastermind.server.get_agent", return_value=mock_agent):
            client = TestClient(app)
            resp = client.get("/api/v1/metrics/summary")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_tasks"] == 3
        assert data["cost_by_model"]["claude-sonnet-4@20250514"] == pytest.approx(0.60, abs=0.001)
        assert data["escalation_reasons"]["Stalled"] == 1


# ---------------------------------------------------------------------------
# Agent tracker integration
# ---------------------------------------------------------------------------


class TestAgentTrackerIntegration:
    @pytest.mark.asyncio
    async def test_handle_task_calls_start_and_finalize(self):
        from henchmen.mastermind.agent import MastermindAgent

        # Override vertex_ai_model_complex via model_copy so the agent's
        # cost accounting resolves to the expected tier. Pinecone fields
        # were removed from Settings when RAG moved to Vertex AI.
        settings = _mock_settings().model_copy(update={"vertex_ai_model_complex": "claude-sonnet-4@20250514"})

        with patch("henchmen.mastermind.agent.LairManager"):
            agent = MastermindAgent(settings=settings)

        agent.tracker = MagicMock()
        agent.tracker.get_task = AsyncMock(return_value=None)  # No prior execution — not a duplicate
        agent.tracker.start_task = AsyncMock()
        agent.tracker.finalize_task = AsyncMock()
        task = _make_task()

        with (
            patch.object(agent, "_select_scheme", return_value="bugfix_standard"),
            patch.object(agent, "_build_dossier", return_value=MagicMock()),
            patch("henchmen.mastermind.agent.SchemeExecutor") as mock_exec_cls,
            patch("henchmen.mastermind.agent.SchemeRegistry") as mock_reg,
        ):
            mock_reg.get.return_value = MagicMock()
            mock_exec = MagicMock()
            mock_exec.execute = AsyncMock(
                return_value={
                    "final_status": "pr_created",
                    "pr_url": "http://pr",
                    "node_results": {"create_pr": {"pr_number": 42}},
                }
            )
            mock_exec_cls.return_value = mock_exec

            await agent.handle_task(task)

        agent.tracker.start_task.assert_called_once_with(task, "bugfix_standard")
        agent.tracker.finalize_task.assert_called_once()


# ---------------------------------------------------------------------------
# SchemeExecutor tracker integration
# ---------------------------------------------------------------------------


class TestSchemeExecutorTracking:
    @pytest.mark.asyncio
    async def test_agentic_node_records_to_tracker(self):
        from henchmen.mastermind.scheme_executor import SchemeExecutor
        from henchmen.models.scheme import NodeType

        mock_tracker = MagicMock()
        mock_lair = MagicMock()

        report = _make_report()
        mock_lair.create_lair = AsyncMock(return_value="lair-123")
        mock_lair.wait_for_completion = AsyncMock(return_value=report)

        mock_graph = MagicMock()

        executor = SchemeExecutor(mock_graph, mock_lair, _mock_settings(), tracker=mock_tracker)

        node = MagicMock()
        node.id = "implement_fix"
        node.node_type = NodeType.AGENTIC
        task = _make_task()
        dossier = MagicMock()

        await executor._execute_agentic(node, task, dossier)

        mock_tracker.record_node_result.assert_called_once_with(task.id, "implement_fix", report)


# ---------------------------------------------------------------------------
# Slack message formatting
# ---------------------------------------------------------------------------


class TestSlackEnrichment:
    def test_format_enriched_message(self):
        from henchmen.mastermind.server import _format_metrics_block

        metrics = {
            "estimated_cost_usd": 0.42,
            "total_input_tokens": 280_000,
            "total_output_tokens": 15_000,
            "wall_clock_seconds": 840,
            "node_metrics": {
                "plan_implementation": {"wall_clock_seconds": 180},
                "implement_feature": {"wall_clock_seconds": 480},
                "verify_changes": {"wall_clock_seconds": 120},
            },
            "files_changed": ["a.py", "b.py", "c.py"],
            "confidence_score": 0.75,
        }
        text = _format_metrics_block(metrics)
        assert "$0.42" in text
        assert "280K" in text
        assert "14m" in text
        assert "0.75" in text

    def test_format_metrics_block_missing_data(self):
        from henchmen.mastermind.server import _format_metrics_block

        text = _format_metrics_block({})
        assert "Cost" in text


# ---------------------------------------------------------------------------
# CI follow-up
# ---------------------------------------------------------------------------


class TestCIFollowUp:
    def test_format_ci_message_passed(self):
        from henchmen.mastermind.server import _format_ci_result_message

        msg = _format_ci_result_message(34, True, [])
        assert "PR #34" in msg
        assert "passed" in msg.lower()

    def test_format_ci_message_failed(self):
        from henchmen.mastermind.server import _format_ci_result_message

        msg = _format_ci_result_message(34, False, ["Build", "Type Check"])
        assert "PR #34" in msg
        assert "Build" in msg
        assert "Type Check" in msg


# ---------------------------------------------------------------------------
# Tracker execution state methods (durable execution)
# ---------------------------------------------------------------------------


class TestTrackerExecutionState:
    """Test durable execution state methods."""

    def _make_tracker(self, store: MagicMock | None = None):
        from henchmen.observability.tracker import TaskTracker

        settings = _mock_settings()
        mock_store = store or _make_mock_store()
        return TaskTracker(settings, document_store=mock_store)

    @pytest.mark.asyncio
    async def test_update_execution_state(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.update_execution_state(
            task_id="task-1",
            current_node_id="implement_fix",
            node_results={"create_branch": {"condition": None}},
            retry_counts={"run_lint": 1},
        )
        store.update.assert_called_once()
        _coll, _id, data = store.update.call_args[0]
        assert data["current_node_id"] == "implement_fix"
        assert data["execution_state"] == "running"
        assert "last_heartbeat" in data

    @pytest.mark.asyncio
    async def test_update_execution_state_stores_node_results_and_retry_counts(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        node_results = {"create_branch": {"condition": None}, "implement_fix": {"condition": "pass"}}
        retry_counts = {"run_lint": 2, "run_tests": 1}
        await tracker.update_execution_state(
            task_id="task-1",
            current_node_id="verify_changes",
            node_results=node_results,
            retry_counts=retry_counts,
        )
        _coll, _id, data = store.update.call_args[0]
        assert data["node_results"] == node_results
        assert data["retry_counts"] == retry_counts

    @pytest.mark.asyncio
    async def test_update_execution_state_swallows_errors(self):
        store = _make_mock_store()
        store.update = AsyncMock(side_effect=Exception("Store down"))
        tracker = self._make_tracker(store)
        # Should not raise
        await tracker.update_execution_state("task-1", "node-1", {}, {})

    @pytest.mark.asyncio
    async def test_mark_stalled(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.mark_stalled("task-1")
        _coll, _id, data = store.update.call_args[0]
        assert data["execution_state"] == "stalled"

    @pytest.mark.asyncio
    async def test_mark_stalled_swallows_errors(self):
        store = _make_mock_store()
        store.update = AsyncMock(side_effect=Exception("Store down"))
        tracker = self._make_tracker(store)
        await tracker.mark_stalled("task-1")  # Should not raise

    @pytest.mark.asyncio
    async def test_mark_escalated_with_reason(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.mark_escalated("task-1", reason="Stalled after 3 attempts")
        _coll, _id, data = store.update.call_args[0]
        assert data["execution_state"] == "escalated"
        assert data["final_status"] == "escalated"
        assert "3 attempts" in data.get("escalation_reason", "")

    @pytest.mark.asyncio
    async def test_mark_escalated_sets_completed_at(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.mark_escalated("task-1", reason="timeout")
        _coll, _id, data = store.update.call_args[0]
        assert "completed_at" in data

    @pytest.mark.asyncio
    async def test_mark_escalated_default_reason(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.mark_escalated("task-1")
        _coll, _id, data = store.update.call_args[0]
        assert data["escalation_reason"] == ""

    @pytest.mark.asyncio
    async def test_increment_recovery_attempts(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.increment_recovery_attempts("task-1")
        # Uses the atomic increment primitive — no read-modify-write.
        store.increment.assert_called_once()
        coll, doc_id, deltas = store.increment.call_args.args
        assert coll == "task_executions"
        assert doc_id == "task-1"
        assert deltas == {"recovery_attempts": 1}

    @pytest.mark.asyncio
    async def test_increment_recovery_attempts_swallows_errors(self):
        store = _make_mock_store()
        store.increment = AsyncMock(side_effect=Exception("Store down"))
        tracker = self._make_tracker(store)
        await tracker.increment_recovery_attempts("task-1")  # Should not raise

    @pytest.mark.asyncio
    async def test_get_stalled_tasks_returns_list(self):
        store = _make_mock_store()
        store.query = AsyncMock(
            return_value=[
                {"task_id": "t1", "execution_state": "running"},
                {"task_id": "t2", "execution_state": "running"},
            ]
        )
        tracker = self._make_tracker(store)
        results = await tracker.get_stalled_tasks(heartbeat_threshold_minutes=10)
        assert len(results) == 2
        assert results[0]["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_get_stalled_tasks_returns_empty_on_error(self):
        store = _make_mock_store()
        store.query = AsyncMock(side_effect=Exception("Store down"))
        tracker = self._make_tracker(store)
        results = await tracker.get_stalled_tasks()
        assert results == []

    @pytest.mark.asyncio
    async def test_start_task_includes_execution_state_fields(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        task = _make_task()
        await tracker.start_task(task, "bugfix_standard")
        _coll, _id, call_data = store.set.call_args[0]
        assert call_data["execution_state"] == "running"
        assert call_data["current_node_id"] is None
        assert "last_heartbeat" in call_data
        assert call_data["recovery_attempts"] == 0
        assert call_data["escalation_reason"] is None


# ---------------------------------------------------------------------------
# Dedup (Layer 1: message, Layer 2: PR, Layer 3: task)
# ---------------------------------------------------------------------------


class TestMessageDedup:
    """Test Layer 1: Pub/Sub message-level dedup via DocumentStore."""

    @pytest.mark.asyncio
    async def test_new_message_returns_false(self):
        """A new message should not be flagged as duplicate."""
        from henchmen.mastermind.server import _check_message_dedup

        mock_store = _make_mock_store()
        mock_store.get = AsyncMock(return_value=None)  # not yet seen
        mock_agent = MagicMock()
        mock_agent.tracker._store = mock_store

        with patch("henchmen.mastermind.server.get_agent", return_value=mock_agent):
            result = await _check_message_dedup("msg-001")

        assert result is False
        # Verify store.set was called to record the message
        mock_store.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_duplicate_message_returns_true(self):
        """An already-processed message should be flagged as duplicate."""
        from henchmen.mastermind.server import _check_message_dedup

        mock_store = _make_mock_store()
        mock_store.get = AsyncMock(return_value={"processed_at": "2026-01-01"})  # already exists
        mock_agent = MagicMock()
        mock_agent.tracker._store = mock_store

        with patch("henchmen.mastermind.server.get_agent", return_value=mock_agent):
            result = await _check_message_dedup("msg-001")

        assert result is True
        # set should NOT be called for duplicates
        mock_store.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_message_id_returns_false(self):
        """Empty message IDs should not be checked (early return)."""
        from henchmen.mastermind.server import _check_message_dedup

        result = await _check_message_dedup("")
        assert result is False


class TestTaskLevelDedup:
    """Test Layer 3: Task-level dedup — skip tasks already running or stalled."""

    @pytest.mark.asyncio
    async def test_running_task_returns_already_running(self):
        from henchmen.mastermind.agent import MastermindAgent

        settings = _mock_settings()
        with patch("henchmen.mastermind.agent.LairManager"):
            agent = MastermindAgent(settings=settings)

        agent.tracker = MagicMock()
        agent.tracker.get_task = AsyncMock(return_value={"execution_state": "running", "task_id": "t1"})
        agent.tracker.start_task = AsyncMock()

        task = _make_task()
        result = await agent.handle_task(task)

        assert result["status"] == "already_running"
        agent.tracker.start_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_stalled_task_returns_already_running(self):
        from henchmen.mastermind.agent import MastermindAgent

        settings = _mock_settings()
        with patch("henchmen.mastermind.agent.LairManager"):
            agent = MastermindAgent(settings=settings)

        agent.tracker = MagicMock()
        agent.tracker.get_task = AsyncMock(return_value={"execution_state": "stalled", "task_id": "t1"})

        task = _make_task()
        result = await agent.handle_task(task)

        assert result["status"] == "already_running"

    @pytest.mark.asyncio
    async def test_completed_task_is_not_deduped(self):
        """A task with execution_state='completed' should be processed normally."""
        from henchmen.mastermind.agent import MastermindAgent

        settings = _mock_settings()
        with patch("henchmen.mastermind.agent.LairManager"):
            agent = MastermindAgent(settings=settings)

        agent.tracker = MagicMock()
        agent.tracker.get_task = AsyncMock(return_value={"execution_state": "completed", "task_id": "t1"})
        agent.tracker.start_task = AsyncMock()
        agent.tracker.finalize_task = AsyncMock()

        task = _make_task()

        with (
            patch.object(agent, "_select_scheme", return_value="bugfix_standard"),
            patch.object(agent, "_build_dossier", return_value=MagicMock()),
            patch("henchmen.mastermind.agent.SchemeExecutor") as mock_exec_cls,
            patch("henchmen.mastermind.agent.SchemeRegistry") as mock_reg,
        ):
            mock_reg.get.return_value = MagicMock()
            mock_exec = MagicMock()
            mock_exec.execute = AsyncMock(return_value={"final_status": "completed", "pr_url": "", "node_results": {}})
            mock_exec_cls.return_value = mock_exec

            result = await agent.handle_task(task)

        assert result["status"] != "already_running"
        agent.tracker.start_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_existing_task_proceeds_normally(self):
        """When get_task returns None, the task should be processed normally."""
        from henchmen.mastermind.agent import MastermindAgent

        settings = _mock_settings()
        with patch("henchmen.mastermind.agent.LairManager"):
            agent = MastermindAgent(settings=settings)

        agent.tracker = MagicMock()
        agent.tracker.get_task = AsyncMock(return_value=None)
        agent.tracker.start_task = AsyncMock()
        agent.tracker.finalize_task = AsyncMock()

        task = _make_task()

        with (
            patch.object(agent, "_select_scheme", return_value="bugfix_standard"),
            patch.object(agent, "_build_dossier", return_value=MagicMock()),
            patch("henchmen.mastermind.agent.SchemeExecutor") as mock_exec_cls,
            patch("henchmen.mastermind.agent.SchemeRegistry") as mock_reg,
        ):
            mock_reg.get.return_value = MagicMock()
            mock_exec = MagicMock()
            mock_exec.execute = AsyncMock(return_value={"final_status": "completed", "pr_url": "", "node_results": {}})
            mock_exec_cls.return_value = mock_exec

            result = await agent.handle_task(task)

        assert result["status"] != "already_running"


class TestPRDedup:
    """Test Layer 2: PR dedup — return existing PR instead of creating a new one."""

    @pytest.mark.asyncio
    async def test_existing_pr_is_returned(self):
        from henchmen.mastermind.scheme_executor import SchemeExecutor

        mock_graph = MagicMock()
        executor = SchemeExecutor(mock_graph, MagicMock(), _mock_settings())

        task = _make_task()
        node = MagicMock()
        node.id = "create_pr"
        dossier = MagicMock()

        mock_pr = MagicMock()
        mock_pr.html_url = "https://github.com/org/repo/pull/99"
        mock_pr.number = 99

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = [mock_pr]

        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}),
            patch("github.Github") as mock_github_cls,
        ):
            mock_github_cls.return_value.get_repo.return_value = mock_repo
            from henchmen.mastermind.scheme_executor.handlers import handle_create_pr

            result = await handle_create_pr(executor, node, task, dossier)

        assert result["condition"] == "pass"
        assert result["pr_url"] == "https://github.com/org/repo/pull/99"
        assert result["pr_number"] == 99
        assert result["message"] == "PR already exists"
        # create_pull should NOT have been called
        mock_repo.create_pull.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_existing_pr_creates_new(self):
        from henchmen.mastermind.scheme_executor import SchemeExecutor

        mock_graph = MagicMock()
        executor = SchemeExecutor(mock_graph, MagicMock(), _mock_settings())

        task = _make_task()
        node = MagicMock()
        node.id = "create_pr"
        dossier = MagicMock()

        mock_new_pr = MagicMock()
        mock_new_pr.html_url = "https://github.com/org/repo/pull/100"
        mock_new_pr.number = 100

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = []  # No existing PRs
        mock_repo.create_pull.return_value = mock_new_pr

        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "fake-token"}),
            patch("github.Github") as mock_github_cls,
        ):
            mock_github_cls.return_value.get_repo.return_value = mock_repo
            from henchmen.mastermind.scheme_executor.handlers import handle_create_pr

            result = await handle_create_pr(executor, node, task, dossier)

        assert result["condition"] == "pass"
        assert result["pr_url"] == "https://github.com/org/repo/pull/100"
        assert result["pr_number"] == 100
        mock_repo.create_pull.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 1: New observability fields
# ---------------------------------------------------------------------------


class TestRecordNodeResultNewFields:
    """Test that record_node_result persists steps_used, context_tokens_at_start/end."""

    def _make_tracker(self, store: MagicMock | None = None):
        from henchmen.observability.tracker import TaskTracker

        settings = _mock_settings()
        mock_store = store or _make_mock_store()
        return TaskTracker(settings, document_store=mock_store)

    @pytest.mark.asyncio
    async def test_record_node_result_includes_steps_and_context_tokens(self):
        store = _make_mock_store()
        store.get = AsyncMock(return_value={})
        tracker = self._make_tracker(store)
        report = _make_report(
            steps_used=25,
            context_tokens_at_start=5000,
            context_tokens_at_end=12000,
        )
        await tracker.record_node_result("test-task", "implement_fix", report)
        _coll, _id, update_data = store.update.call_args[0]
        node_data = update_data["node_metrics"]["implement_fix"]
        assert node_data["steps_used"] == 25
        assert node_data["context_tokens_at_start"] == 5000
        assert node_data["context_tokens_at_end"] == 12000


class TestRecordRagChunks:
    def _make_tracker(self, store: MagicMock | None = None):
        from henchmen.observability.tracker import TaskTracker

        settings = _mock_settings()
        mock_store = store or _make_mock_store()
        return TaskTracker(settings, document_store=mock_store)

    @pytest.mark.asyncio
    async def test_record_rag_chunks(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.record_rag_chunks("task-1", 15)
        store.increment.assert_called_once()
        _coll, _id, deltas = store.increment.call_args.args
        assert deltas == {"rag_chunks_retrieved": 15}

    @pytest.mark.asyncio
    async def test_record_rag_chunks_swallows_errors(self):
        store = _make_mock_store()
        store.increment = AsyncMock(side_effect=Exception("Store down"))
        tracker = self._make_tracker(store)
        await tracker.record_rag_chunks("task-1", 10)  # Should not raise


class TestCleanupExpiredTasks:
    def _make_tracker(self, store: MagicMock | None = None):
        from henchmen.observability.tracker import TaskTracker

        settings = _mock_settings()
        mock_store = store or _make_mock_store()
        return TaskTracker(settings, document_store=mock_store)

    @pytest.mark.asyncio
    async def test_cleanup_expired_deletes_old_docs(self):
        store = _make_mock_store()
        store.query = AsyncMock(
            return_value=[
                {"task_id": "t1"},
                {"task_id": "t2"},
            ]
        )
        tracker = self._make_tracker(store)
        deleted = await tracker.cleanup_expired()
        assert deleted == 2
        assert store.delete.call_count == 2

    @pytest.mark.asyncio
    async def test_cleanup_expired_returns_zero_on_empty(self):
        store = _make_mock_store()
        store.query = AsyncMock(return_value=[])
        tracker = self._make_tracker(store)
        deleted = await tracker.cleanup_expired()
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_cleanup_expired_swallows_errors(self):
        store = _make_mock_store()
        store.query = AsyncMock(side_effect=Exception("Store down"))
        tracker = self._make_tracker(store)
        deleted = await tracker.cleanup_expired()
        assert deleted == 0


class TestCleanupProcessedMessages:
    def _make_tracker(self, store: MagicMock | None = None):
        from henchmen.observability.tracker import TaskTracker

        settings = _mock_settings()
        mock_store = store or _make_mock_store()
        return TaskTracker(settings, document_store=mock_store)

    @pytest.mark.asyncio
    async def test_cleanup_processed_messages(self):
        store = _make_mock_store()
        store.query = AsyncMock(
            return_value=[
                {"key": "msg-1", "processed_at": "2026-01-01T00:00:00+00:00"},
                {"key": "msg-2", "processed_at": "2026-01-01T00:00:00+00:00"},
                {"key": "msg-3", "processed_at": "2026-01-01T00:00:00+00:00"},
            ]
        )
        tracker = self._make_tracker(store)
        deleted = await tracker.cleanup_processed_messages()
        assert deleted == 3
        assert store.delete.call_count == 3


class TestMarkEscalatedWithNode:
    def _make_tracker(self, store: MagicMock | None = None):
        from henchmen.observability.tracker import TaskTracker

        settings = _mock_settings()
        mock_store = store or _make_mock_store()
        return TaskTracker(settings, document_store=mock_store)

    @pytest.mark.asyncio
    async def test_mark_escalated_with_node(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.mark_escalated("task-1", reason="cycle", escalation_node="implement_fix")
        _coll, _id, data = store.update.call_args[0]
        assert data["escalation_node"] == "implement_fix"
        assert data["escalation_reason"] == "cycle"

    @pytest.mark.asyncio
    async def test_mark_escalated_without_node_omits_field(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.mark_escalated("task-1", reason="timeout")
        _coll, _id, data = store.update.call_args[0]
        assert "escalation_node" not in data


class TestUpdateHeartbeat:
    def _make_tracker(self, store: MagicMock | None = None):
        from henchmen.observability.tracker import TaskTracker

        settings = _mock_settings()
        mock_store = store or _make_mock_store()
        return TaskTracker(settings, document_store=mock_store)

    @pytest.mark.asyncio
    async def test_update_heartbeat(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.update_heartbeat("task-1")
        store.update.assert_called_once()
        _coll, _id, data = store.update.call_args[0]
        assert "last_heartbeat" in data
