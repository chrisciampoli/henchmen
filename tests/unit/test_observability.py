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

        # First-party dash ID: the ``claude-*@date`` spelling is the Vertex AI
        # form, and Claude is never run on Vertex AI.
        cost = estimate_cost("claude-sonnet-4-20250514", 100_000, 5_000)
        assert cost == pytest.approx(0.375, abs=0.001)

    def test_claude_haiku_4_5_uses_the_current_rate(self):
        """Haiku 4.5 is $1 / $5 per MTok, not the retired Haiku 3.5 rate of $0.80 / $4."""
        from henchmen.observability.tracker import estimate_cost

        assert estimate_cost("claude-haiku-4-5", 1_000_000, 0) == pytest.approx(1.0, abs=0.001)
        assert estimate_cost("claude-haiku-4-5", 0, 1_000_000) == pytest.approx(5.0, abs=0.001)

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

        cost = estimate_cost("claude-sonnet-4-20250514", 0, 0)
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
                    "implement_fix": {"model_name": "claude-sonnet-5", "cost_usd": 0.30},
                    "verify_changes": {"model_name": "gemini-2.5-flash", "cost_usd": 0.05},
                },
            },
            {
                "final_status": "pr_created",
                "estimated_cost_usd": 0.35,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "node_metrics": {
                    "implement_fix": {"model_name": "claude-sonnet-5", "cost_usd": 0.35},
                },
            },
        ]
        tracker = self._make_tracker(tasks)
        result = await tracker.get_metrics_summary(days=7)
        cbm = result["cost_by_model"]
        assert cbm["claude-sonnet-5"] == pytest.approx(0.65, abs=0.001)
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
            "cost_by_model": {"claude-sonnet-5": 0.60},
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
        assert data["cost_by_model"]["claude-sonnet-5"] == pytest.approx(0.60, abs=0.001)
        assert data["escalation_reasons"]["Stalled"] == 1

    @staticmethod
    def _get(headers: dict[str, str] | None = None):
        from fastapi.testclient import TestClient

        from henchmen.mastermind.server import app

        mock_agent = MagicMock()
        mock_agent.tracker.get_metrics_summary = AsyncMock(return_value={"cost_by_model": {"m": 1.0}})
        with patch("henchmen.mastermind.server.get_agent", return_value=mock_agent):
            return TestClient(app).get("/api/v1/metrics/summary", headers=headers or {})

    def test_requires_the_metrics_bearer_token(self, monkeypatch: pytest.MonkeyPatch):
        """Cost-by-model and escalation reasons sit behind the same token as /metrics."""
        monkeypatch.setenv("HENCHMEN_METRICS_AUTH_TOKEN", "s3cret")
        assert self._get().status_code == 401
        assert self._get({"Authorization": "Bearer wrong"}).status_code == 401
        assert self._get({"Authorization": "Bearer s3cret"}).status_code == 200

    def test_fails_closed_in_prod_without_a_token(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "prod")
        monkeypatch.setenv("HENCHMEN_PUBSUB_OIDC_AUDIENCE", "https://example.test")
        assert self._get().status_code == 401


# ---------------------------------------------------------------------------
# Agent tracker integration
# ---------------------------------------------------------------------------


class TestAgentTrackerIntegration:
    @pytest.mark.asyncio
    async def test_handle_task_calls_start_and_finalize(self):
        from henchmen.mastermind.agent import MastermindAgent

        # Pin vertex_ai_model_complex so the agent's cost accounting resolves
        # the COMPLEX tier to a known Gemini model (never Claude on Vertex AI).
        settings = _mock_settings().model_copy(update={"vertex_ai_model_complex": "gemini-2.5-pro"})

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
    async def test_get_stalled_tasks_fails_closed_on_query_error(self, caplog):
        """A failing query (e.g. missing Firestore index) must not look like "0 stalled"."""
        store = _make_mock_store()
        store.query = AsyncMock(side_effect=RuntimeError("The query requires an index"))
        tracker = self._make_tracker(store)
        with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="requires an index"):
            await tracker.get_stalled_tasks()
        assert "Failed to query stalled tasks" in caplog.text

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
        # The handler re-checks head.ref locally because GitHub ignores an
        # unqualified ``head`` filter and returns every open PR.
        mock_pr.head.ref = task.branch_name

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = [mock_pr]

        with (
            # The handler reads the token through Settings, and get_settings()
            # is cached, so patching os.environ after the instance was built
            # would not reach it.
            patch("henchmen.mastermind.scheme_executor.handlers.get_github_token_async", return_value="fake-token"),
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
            # The handler reads the token through Settings, and get_settings()
            # is cached, so patching os.environ after the instance was built
            # would not reach it.
            patch("henchmen.mastermind.scheme_executor.handlers.get_github_token_async", return_value="fake-token"),
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


# ---------------------------------------------------------------------------
# Provider-aware cost estimation (tier names must never be priced as Claude)
# ---------------------------------------------------------------------------


def _settings_for(**overrides):
    """A Settings instance with the given field overrides."""
    return _mock_settings().model_copy(update=overrides)


class TestEstimateCostTierResolution:
    def test_tier_resolves_through_vertex_settings_on_gcp(self):
        from henchmen.observability.tracker import estimate_cost

        settings = _settings_for(llm_provider="gcp", vertex_ai_model_complex="gemini-2.5-pro")
        # gemini-2.5-pro is $1.25 / $10 per MTok, NOT Sonnet's $3 / $15.
        cost = estimate_cost("default/complex", 1_000_000, 0, settings=settings)
        assert cost == pytest.approx(1.25, abs=0.001)

    def test_reasoning_tier_is_not_priced_as_opus_on_gcp(self):
        from henchmen.observability.tracker import estimate_cost

        settings = _settings_for(llm_provider="gcp", vertex_ai_model_reasoning="gemini-3.1-pro")
        cost = estimate_cost("default/reasoning", 1_000_000, 0, settings=settings)
        # Opus pricing would be $15.00 for the same call.
        assert cost == pytest.approx(2.0, abs=0.001)

    def test_tier_resolves_through_anthropic_settings(self):
        from henchmen.observability.tracker import estimate_cost

        settings = _settings_for(llm_provider="anthropic", anthropic_model_complex="claude-sonnet-4")
        cost = estimate_cost("default/complex", 1_000_000, 0, settings=settings)
        assert cost == pytest.approx(3.0, abs=0.001)

    def test_local_provider_is_free_so_wall_clock_ceiling_stays_reachable(self, caplog):
        from henchmen.observability.tracker import estimate_cost

        settings = _settings_for(llm_provider="local", llm_ollama_model="qwen2.5-coder:7b")
        with caplog.at_level("WARNING"):
            cost = estimate_cost("default/complex", 5_000_000, 100_000, settings=settings)
        assert cost == 0.0
        # A free local model is expected to be unpriced - no warning noise.
        assert "Unknown model for cost estimation" not in caplog.text

    def test_unknown_model_warns_on_a_paid_provider(self, caplog):
        from henchmen.observability.tracker import estimate_cost

        settings = _settings_for(llm_provider="gcp")
        with caplog.at_level("WARNING"):
            cost = estimate_cost("no-such-model-v9", 1000, 10, settings=settings)
        assert cost == 0.0
        assert "Unknown model for cost estimation" in caplog.text

    def test_cached_input_tokens_get_the_cache_rate(self):
        from henchmen.observability.tracker import estimate_cost

        settings = _settings_for(llm_provider="gcp")
        full = estimate_cost("gemini-2.5-pro", 1_000_000, 0, settings=settings)
        cached = estimate_cost("gemini-2.5-pro", 1_000_000, 0, cached_input_tokens=1_000_000, settings=settings)
        # Vertex context cache reads bill at 25% of the input rate.
        assert cached == pytest.approx(full * 0.25, abs=0.001)

    def test_openai_models_are_priced(self):
        from henchmen.observability.tracker import estimate_cost

        settings = _settings_for(llm_provider="openai", openai_model_complex="gpt-4.1")
        assert estimate_cost("default/complex", 1_000_000, 0, settings=settings) > 0.0


class TestRecordNodeResultCost:
    def _make_tracker(self, store, **overrides):
        from henchmen.observability.tracker import TaskTracker

        return TaskTracker(_settings_for(**overrides), document_store=store)

    @pytest.mark.asyncio
    async def test_tier_model_name_is_recorded_as_the_concrete_model(self):
        store = _make_mock_store()
        store.get = AsyncMock(return_value={})
        tracker = self._make_tracker(store, llm_provider="gcp", vertex_ai_model_complex="gemini-2.5-pro")
        report = _make_report(model_name="default/complex")

        await tracker.record_node_result("test-task", "implement_fix", report)

        _coll, _id, update_data = store.update.call_args[0]
        node_data = update_data["node_metrics"]["implement_fix"]
        assert node_data["model_name"] == "gemini-2.5-pro"

    @pytest.mark.asyncio
    async def test_cost_uses_provider_pricing_not_anthropic(self):
        store = _make_mock_store()
        store.get = AsyncMock(return_value={})
        tracker = self._make_tracker(store, llm_provider="gcp", vertex_ai_model_complex="gemini-2.5-pro")
        report = _make_report(model_name="default/complex", total_input_tokens=1_000_000, total_output_tokens=0)

        await tracker.record_node_result("test-task", "implement_fix", report)

        _coll, _id, deltas = store.increment.call_args.args
        assert deltas["estimated_cost_usd"] == pytest.approx(1.25, abs=0.001)

    @pytest.mark.asyncio
    async def test_cached_tokens_are_discounted(self):
        store = _make_mock_store()
        store.get = AsyncMock(return_value={})
        tracker = self._make_tracker(store, llm_provider="gcp")
        report = _make_report(
            model_name="gemini-2.5-pro",
            total_input_tokens=1_000_000,
            total_output_tokens=0,
            cached_input_tokens=1_000_000,
        )

        await tracker.record_node_result("test-task", "implement_fix", report)

        _coll, _id, update_data = store.update.call_args[0]
        node_data = update_data["node_metrics"]["implement_fix"]
        assert node_data["cached_input_tokens"] == 1_000_000
        assert node_data["cost_usd"] == pytest.approx(1.25 * 0.25, abs=0.001)

    @staticmethod
    def _report_with_provider_cost(cost: float, **overrides):
        """An OperativeReport carrying the provider-summed ``estimated_cost_usd``."""
        from henchmen.models.operative import OperativeReport

        base = _make_report(**overrides).model_dump()
        return OperativeReport(**{**base, "estimated_cost_usd": cost})

    @pytest.mark.asyncio
    async def test_provider_billed_cost_is_persisted_as_is(self):
        """Anthropic cache writes bill at 125%; re-deriving from token counters cannot see them."""
        store = _make_mock_store()
        store.get = AsyncMock(return_value={})
        tracker = self._make_tracker(store, llm_provider="anthropic", anthropic_api_key="k")
        report = self._report_with_provider_cost(
            0.4321, model_name="claude-sonnet-5", total_input_tokens=100_000, total_output_tokens=0
        )

        await tracker.record_node_result("test-task", "implement_fix", report)

        _coll, _id, deltas = store.increment.call_args.args
        assert deltas["estimated_cost_usd"] == pytest.approx(0.4321)
        _coll, _id, update_data = store.update.call_args[0]
        assert update_data["node_metrics"]["implement_fix"]["cost_usd"] == pytest.approx(0.4321)

    @pytest.mark.asyncio
    async def test_zero_provider_cost_falls_back_to_token_estimate(self):
        store = _make_mock_store()
        store.get = AsyncMock(return_value={})
        tracker = self._make_tracker(store, llm_provider="gcp")
        report = self._report_with_provider_cost(
            0.0, model_name="gemini-2.5-pro", total_input_tokens=1_000_000, total_output_tokens=0
        )

        await tracker.record_node_result("test-task", "implement_fix", report)

        _coll, _id, deltas = store.increment.call_args.args
        assert deltas["estimated_cost_usd"] == pytest.approx(1.25, abs=0.001)


# ---------------------------------------------------------------------------
# Cost double counting
# ---------------------------------------------------------------------------


class _InMemoryStore:
    """Minimal DocumentStore with real increment semantics."""

    def __init__(self) -> None:
        self.docs: dict = {}

    async def get(self, collection, document_id):
        return self.docs.get((collection, document_id))

    async def set(self, collection, document_id, data):
        self.docs[(collection, document_id)] = dict(data)

    async def update(self, collection, document_id, data):
        self.docs.setdefault((collection, document_id), {}).update(data)

    async def delete(self, collection, document_id):
        self.docs.pop((collection, document_id), None)

    async def query(self, collection, filters=None, order_by=None, order_direction="ASCENDING", limit=None):
        return []

    async def increment(self, collection, document_id, field_deltas):
        doc = self.docs.setdefault((collection, document_id), {})
        for field, delta in field_deltas.items():
            doc[field] = doc.get(field, 0) + delta

    async def update_if(self, collection, document_id, expected_field, expected_value, new_values):
        return False


class TestCostIsCountedOnce:
    @pytest.mark.asyncio
    async def test_accumulator_plus_tracker_records_node_cost_once(self):
        from henchmen.observability.cost_accumulator import TaskCostAccumulator
        from henchmen.observability.tracker import TaskTracker

        store = _InMemoryStore()
        await store.set("task_executions", "task-1", {"estimated_cost_usd": 0.0})

        # The operative accumulates the same spend in memory while the node runs.
        accumulator = TaskCostAccumulator(store, "task-1", ceiling_usd=6.0)
        await accumulator.add(1.25)

        tracker = TaskTracker(
            _settings_for(llm_provider="gcp", vertex_ai_model_complex="gemini-2.5-pro"),
            document_store=store,
        )
        report = _make_report(model_name="default/complex", total_input_tokens=1_000_000, total_output_tokens=0)
        await tracker.record_node_result("task-1", "implement_fix", report)

        persisted = store.docs[("task_executions", "task-1")]["estimated_cost_usd"]
        assert persisted == pytest.approx(1.25, abs=0.001)

    @pytest.mark.asyncio
    async def test_accumulator_does_not_write_to_the_store(self):
        from henchmen.observability.cost_accumulator import TaskCostAccumulator

        store = _make_mock_store()
        store.get = AsyncMock(return_value={"estimated_cost_usd": 2.0})
        accumulator = TaskCostAccumulator(store, "task-1", ceiling_usd=6.0)

        await accumulator.add(1.0)

        store.update.assert_not_called()
        assert await accumulator.current_total() == pytest.approx(3.0)
        assert await accumulator.check_ceiling() is True

    @pytest.mark.asyncio
    async def test_accumulator_ceiling_trips_on_cumulative_spend(self):
        from henchmen.observability.cost_accumulator import TaskCostAccumulator

        store = _make_mock_store()
        store.get = AsyncMock(return_value={"estimated_cost_usd": 5.5})
        accumulator = TaskCostAccumulator(store, "task-1", ceiling_usd=6.0)
        await accumulator.add(1.0)
        assert await accumulator.check_ceiling() is False

    @pytest.mark.asyncio
    async def test_non_desktop_seed_error_defaults_to_zero(self):
        """Dev on a repository checkout: a seed error is swallowed and the ceiling starts at zero."""
        from henchmen.observability.cost_accumulator import TaskCostAccumulator

        store = _make_mock_store()
        store.get = AsyncMock(side_effect=RuntimeError("store unavailable"))
        settings = _settings_for(environment="dev")
        accumulator = TaskCostAccumulator(store, "task-1", ceiling_usd=6.0, settings=settings)

        assert await accumulator.current_total() == 0.0

    @pytest.mark.asyncio
    async def test_desktop_posture_seed_error_fails_closed(self):
        """A desktop install's operative must not silently start the ceiling at zero."""
        from henchmen.observability.cost_accumulator import TaskCostAccumulator

        store = _make_mock_store()
        store.get = AsyncMock(side_effect=RuntimeError("store unavailable"))
        settings = _settings_for(operative_task_token="t" * 64)
        accumulator = TaskCostAccumulator(store, "task-1", ceiling_usd=6.0, settings=settings)

        with pytest.raises(RuntimeError, match="store unavailable"):
            await accumulator.current_total()

    @pytest.mark.asyncio
    async def test_missing_document_is_not_an_error_even_on_desktop(self):
        """A 404 (no prior spend recorded yet) must not be treated as a seed failure."""
        from henchmen.observability.cost_accumulator import TaskCostAccumulator

        store = _make_mock_store()
        store.get = AsyncMock(return_value=None)
        settings = _settings_for(operative_task_token="t" * 64)
        accumulator = TaskCostAccumulator(store, "task-1", ceiling_usd=6.0, settings=settings)

        assert await accumulator.current_total() == 0.0


# ---------------------------------------------------------------------------
# Datetime filters work on every DocumentStore (SQLite compares as strings)
# ---------------------------------------------------------------------------


class TestTrackerTimestampsAreIsoStrings:
    def _make_tracker(self, store):
        from henchmen.observability.tracker import TaskTracker

        return TaskTracker(_mock_settings(), document_store=store)

    @pytest.mark.asyncio
    async def test_start_task_writes_iso_strings(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.start_task(_make_task(), "bugfix_standard")
        _coll, _id, doc = store.set.call_args[0]
        for field in ("created_at", "expires_at", "last_heartbeat"):
            assert isinstance(doc[field], str), field
            datetime.fromisoformat(doc[field])

    @pytest.mark.asyncio
    async def test_datetime_filters_are_passed_as_strings(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)

        await tracker.get_recent_tasks(days=7)
        assert isinstance(store.query.call_args.kwargs["filters"][0][2], str)

        await tracker.get_stalled_tasks(heartbeat_threshold_minutes=10)
        assert isinstance(store.query.call_args.kwargs["filters"][1][2], str)

        await tracker.cleanup_expired()
        assert isinstance(store.query.call_args.kwargs["filters"][0][2], str)

    @pytest.mark.asyncio
    async def test_round_trip_against_the_sqlite_store(self, tmp_path):
        from henchmen.observability.tracker import TaskTracker
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        settings = _mock_settings()
        store = SQLiteDocumentStore(settings, db_path=str(tmp_path / "t.db"))
        tracker = TaskTracker(settings, document_store=store)
        task = _make_task()
        await tracker.start_task(task, "bugfix_standard")

        # These all raised TypeError ('str' vs 'datetime') before timestamps
        # were normalised to ISO-8601 strings, and the tracker swallowed it.
        recent = await tracker.get_recent_tasks(days=7)
        assert [t["task_id"] for t in recent] == [task.id]
        assert await tracker.get_stalled_tasks(heartbeat_threshold_minutes=10) == []
        assert await tracker.cleanup_expired() == 0

        summary = await tracker.get_metrics_summary(days=7)
        assert summary["total_tasks"] == 1

    @pytest.mark.asyncio
    async def test_stalled_query_finds_an_expired_heartbeat(self, tmp_path):
        from henchmen.observability.tracker import TaskTracker
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        settings = _mock_settings()
        store = SQLiteDocumentStore(settings, db_path=str(tmp_path / "t.db"))
        tracker = TaskTracker(settings, document_store=store)
        task = _make_task()
        await tracker.start_task(task, "bugfix_standard")
        await store.update(
            "task_executions",
            task.id,
            {"last_heartbeat": datetime(2020, 1, 1, tzinfo=UTC).isoformat()},
        )

        stalled = await tracker.get_stalled_tasks(heartbeat_threshold_minutes=10)
        assert [t["task_id"] for t in stalled] == [task.id]

    @pytest.mark.asyncio
    async def test_finalized_task_is_invisible_to_the_watchdog(self, tmp_path):
        from henchmen.observability.tracker import TaskTracker
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        settings = _mock_settings()
        store = SQLiteDocumentStore(settings, db_path=str(tmp_path / "t.db"))
        tracker = TaskTracker(settings, document_store=store)
        task = _make_task()
        await tracker.start_task(task, "bugfix_standard")
        await store.update(
            "task_executions",
            task.id,
            {"last_heartbeat": datetime(2020, 1, 1, tzinfo=UTC).isoformat()},
        )
        await tracker.finalize_task(task.id, "pr_created", "https://github.com/org/repo/pull/1", 1)

        assert await tracker.get_stalled_tasks(heartbeat_threshold_minutes=10) == []
        doc = await tracker.get_task(task.id)
        assert doc["execution_state"] == "completed"

    @pytest.mark.asyncio
    async def test_finalize_escalated_sets_escalated_state(self):
        store = _make_mock_store()
        tracker = self._make_tracker(store)
        await tracker.finalize_task("task-1", "escalated")
        _coll, _id, data = store.update.call_args[0]
        assert data["execution_state"] == "escalated"


class TestCleanupProcessedMessagesInFlight:
    @pytest.mark.asyncio
    async def test_in_flight_markers_are_swept(self, tmp_path):
        from henchmen.observability.tracker import TaskTracker
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        settings = _mock_settings()
        store = SQLiteDocumentStore(settings, db_path=str(tmp_path / "t.db"))
        tracker = TaskTracker(settings, document_store=store)

        old = datetime(2020, 1, 1, tzinfo=UTC).isoformat()
        await store.set(
            "processed_messages",
            "stuck",
            {"key": "stuck", "status": "in_flight", "acquired_at": old, "handler": "task-intake"},
        )
        await store.set(
            "processed_messages",
            "done",
            {"key": "done", "status": "done", "acquired_at": old, "processed_at": old},
        )
        await store.set(
            "processed_messages",
            "fresh",
            {"key": "fresh", "status": "in_flight", "acquired_at": datetime.now(UTC).isoformat()},
        )

        deleted = await tracker.cleanup_processed_messages(retention_days=7)

        assert deleted == 2
        assert await store.get("processed_messages", "stuck") is None
        assert await store.get("processed_messages", "done") is None
        assert await store.get("processed_messages", "fresh") is not None


class TestSuccessStatuses:
    @pytest.mark.asyncio
    async def test_completed_counts_as_success(self):
        from henchmen.observability.tracker import TaskTracker

        store = _make_mock_store()
        store.query = AsyncMock(
            return_value=[
                {"final_status": "pr_created", "node_metrics": {}},
                {"final_status": "completed", "node_metrics": {}},
            ]
        )
        tracker = TaskTracker(_mock_settings(), document_store=store)
        summary = await tracker.get_metrics_summary(days=7)
        assert summary["success_rate"] == pytest.approx(1.0)


class TestRecordEvaluation:
    @pytest.mark.asyncio
    async def test_scores_are_persisted_even_when_vertex_failed(self):
        from henchmen.models.evaluation import EvaluationResult
        from henchmen.observability.tracker import TaskTracker

        store = _make_mock_store()
        tracker = TaskTracker(_mock_settings(), document_store=store)
        result = EvaluationResult(
            fulfillment_score=0.5,
            tool_call_valid_score=0.7,
            safety_score=1.0,
            overall_quality=0.68,
            evaluation_error="Vertex unavailable",
        )

        await tracker.record_evaluation("task-1", result)

        _coll, _id, data = store.update.call_args[0]
        assert data["evaluation_scores"]["overall_quality"] == pytest.approx(0.68)
        assert data["evaluation_scores"]["tool_call_valid"] == pytest.approx(0.7)
        assert data["evaluation_error"] == "Vertex unavailable"


# ---------------------------------------------------------------------------
# Metrics API: auth, redaction, Prometheus shape
# ---------------------------------------------------------------------------


def _metrics_client(tasks, settings=None, task_detail=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from henchmen.observability.api import create_metrics_router

    tracker = MagicMock()
    tracker.get_recent_tasks = AsyncMock(return_value=tasks)
    tracker.get_task = AsyncMock(return_value=task_detail)

    app = FastAPI()
    app.include_router(create_metrics_router(tracker, settings=settings or _mock_settings()))
    return TestClient(app, raise_server_exceptions=False)


class TestMetricsAuth:
    def test_open_in_dev_when_no_token(self):
        from henchmen.config.settings import Environment

        settings = _settings_for(environment=Environment.DEV, metrics_auth_token="")
        client = _metrics_client([], settings=settings)
        assert client.get("/metrics/summary").status_code == 200

    def test_fails_closed_in_prod_when_no_token(self):
        from henchmen.config.settings import Environment

        settings = _settings_for(environment=Environment.PROD, metrics_auth_token="")
        client = _metrics_client([], settings=settings)
        assert client.get("/metrics/summary").status_code == 401

    def test_bearer_token_is_required_when_configured(self):
        settings = _settings_for(metrics_auth_token="s3cret")
        client = _metrics_client([], settings=settings)

        assert client.get("/metrics/summary").status_code == 401
        assert client.get("/metrics/summary", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = client.get("/metrics/summary", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200

    def test_token_is_not_echoed_in_the_401_body(self):
        settings = _settings_for(metrics_auth_token="s3cret")
        client = _metrics_client([], settings=settings)
        resp = client.get("/metrics/tasks")
        assert resp.status_code == 401
        assert "s3cret" not in resp.text


class TestMetricsRedaction:
    def _sensitive_task(self):
        return {
            "task_id": "t1",
            "scheme_id": "bugfix_standard",
            "final_status": "pr_created",
            "estimated_cost_usd": 0.3,
            "files_changed": ["a.py", "b.py"],
            "task_payload": {"context": {"thread_messages": ["internal chatter"]}},
            "interrupted_report": {"git_diff": "diff --git a/secret.py"},
            "node_results": {"run_tests": {"output": "FAILED tests/test_secret.py"}},
            "escalation_reason": "lint output with paths",
            "title": "Fix the login bug for customer X",
        }

    def test_list_endpoint_omits_task_content(self):
        client = _metrics_client([self._sensitive_task()])
        body = client.get("/metrics/tasks").json()
        task = body["tasks"][0]
        assert task["task_id"] == "t1"
        assert task["files_changed_count"] == 2
        for leaked in ("task_payload", "interrupted_report", "node_results", "escalation_reason", "title"):
            assert leaked not in task
        assert "internal chatter" not in client.get("/metrics/tasks").text

    def test_detail_endpoint_omits_task_content(self):
        client = _metrics_client([], task_detail=self._sensitive_task())
        resp = client.get("/metrics/tasks/t1")
        assert resp.status_code == 200
        assert "task_payload" not in resp.json()
        assert "secret.py" not in resp.text

    def test_detail_endpoint_404s_for_unknown_task(self):
        client = _metrics_client([], task_detail=None)
        assert client.get("/metrics/tasks/nope").status_code == 404


class TestSummaryCountsPrCreated:
    def test_pr_created_counts_as_completed(self):
        tasks = [
            {"task_id": "t1", "final_status": "pr_created", "ci_passed": True},
            {"task_id": "t2", "final_status": "completed", "ci_passed": None},
            {"task_id": "t3", "final_status": "escalated", "ci_passed": None},
        ]
        data = _metrics_client(tasks).get("/metrics/summary").json()
        assert data["tasks_completed"] == 2
        assert data["tasks_escalated"] == 1


class TestPrometheusEndpoint:
    def test_no_ci_pass_rate_sample_without_data(self):
        pytest.importorskip("prometheus_client")
        tasks = [{"task_id": "t1", "final_status": "pr_created", "ci_passed": None, "estimated_cost_usd": 0.5}]
        body = _metrics_client(tasks).get("/metrics/prometheus").text
        assert "henchmen_ci_pass_rate" not in body

    def test_ci_pass_rate_is_exported_once_decided(self):
        pytest.importorskip("prometheus_client")
        tasks = [
            {"task_id": "t1", "final_status": "pr_created", "ci_passed": True},
            {"task_id": "t2", "final_status": "escalated", "ci_passed": False},
        ]
        body = _metrics_client(tasks).get("/metrics/prometheus").text
        assert "henchmen_ci_pass_rate" in body

    def test_window_series_are_gauges_not_counters(self):
        pytest.importorskip("prometheus_client")
        tasks = [{"task_id": "t1", "final_status": "pr_created", "ci_passed": True, "estimated_cost_usd": 0.25}]
        body = _metrics_client(tasks).get("/metrics/prometheus?days=7").text
        assert "# TYPE henchmen_tasks_completed_window gauge" in body
        assert 'henchmen_tasks_completed_window{window_days="7"} 1.0' in body
        assert "# TYPE henchmen_cost_usd_window gauge" in body
        # Counter-typed snapshots would make rate()/increase() report resets.
        assert "_total" not in body

    def test_prometheus_requires_auth_when_configured(self):
        pytest.importorskip("prometheus_client")
        settings = _settings_for(metrics_auth_token="s3cret")
        client = _metrics_client([], settings=settings)
        assert client.get("/metrics/prometheus").status_code == 401


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class TestDiffSignalPathClassification:
    def test_source_files_named_like_tests_are_not_tests(self):
        from henchmen.observability.evaluator import _is_source_path, _is_test_path

        for path in ("src/app/latest_report.py", "src/contest_rules.py", "pkg/fastest_path.go", "inspec/loader.py"):
            assert not _is_test_path(path), path
            assert _is_source_path(path), path

    def test_real_tests_are_still_detected(self):
        from henchmen.observability.evaluator import _is_test_path

        for path in (
            "tests/unit/test_auth.py",
            "src/auth/test_login.py",
            "src/auth/login_test.go",
            "web/Button.spec.tsx",
            "app/__tests__/Button.tsx",
            "backend\\tests\\test_x.py",
        ):
            assert _is_test_path(path), path

    def test_doc_paths(self):
        from henchmen.observability.evaluator import _is_doc_path

        assert _is_doc_path("docs/architecture.md")
        assert _is_doc_path("README.md")
        assert not _is_doc_path("src/docker_compose_helper.py")

    def test_diff_signal_scores_a_source_only_change(self):
        from henchmen.observability.evaluator import compute_diff_signal

        report = _make_report(files_changed=["src/app/latest_report.py"])
        # Misclassified as a test this scored 0.3 instead of 0.7.
        assert compute_diff_signal("Fix the report", "The report is wrong", report) == pytest.approx(0.7)

    def test_diff_signal_zero_for_empty_diff(self):
        from henchmen.observability.evaluator import compute_diff_signal

        assert compute_diff_signal("t", "d", _make_report(files_changed=[])) == 0.0


class TestEvaluatorScoring:
    @pytest.mark.asyncio
    async def test_instruction_following_is_normalised_and_safety_is_not(self):
        from henchmen.observability.evaluator import OperativeEvaluator

        evaluator = OperativeEvaluator(project_id="p", region="us-central1")
        with patch.object(
            OperativeEvaluator,
            "_run_vertex_evaluation",
            return_value={"instruction_following/mean": 5.0, "safety/mean": 1.0},
        ):
            result = await evaluator.evaluate_operative_result("Fix", "Fix it", _make_report())

        # 1-5 rubric -> 0-1; safety is already binary and must pass through.
        assert result.fulfillment_score == pytest.approx(1.0)
        assert result.safety_score == pytest.approx(1.0)
        assert result.evaluation_error is None

    @pytest.mark.asyncio
    async def test_mid_rubric_score_stays_in_range(self):
        from henchmen.observability.evaluator import OperativeEvaluator

        evaluator = OperativeEvaluator(project_id="p")
        with patch.object(
            OperativeEvaluator,
            "_run_vertex_evaluation",
            return_value={"instruction_following/mean": 3.0, "safety/mean": 0.0},
        ):
            result = await evaluator.evaluate_operative_result("Fix", "Fix it", _make_report())

        assert result.fulfillment_score == pytest.approx(0.5)
        assert result.safety_score == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_vertex_failure_falls_back_to_diff_signal(self):
        from henchmen.observability.evaluator import OperativeEvaluator

        evaluator = OperativeEvaluator(project_id="p")
        with patch.object(OperativeEvaluator, "_run_vertex_evaluation", side_effect=RuntimeError("boom")):
            result = await evaluator.evaluate_operative_result("Fix", "Fix it", _make_report())

        assert result.evaluation_error == "boom"
        assert result.overall_quality > 0.0

    @pytest.mark.asyncio
    async def test_no_project_id_skips_the_vertex_call(self):
        from henchmen.observability.evaluator import OperativeEvaluator

        evaluator = OperativeEvaluator(project_id="")
        with patch.object(OperativeEvaluator, "_run_vertex_evaluation") as run:
            result = await evaluator.evaluate_operative_result("Fix", "Fix it", _make_report())

        run.assert_not_called()
        assert result.evaluation_error == "no GCP project configured"


class TestEvaluateAndRecord:
    @pytest.mark.asyncio
    async def test_scores_persist_through_the_document_store(self):
        from henchmen.observability.evaluator import OperativeEvaluator, evaluate_and_record
        from henchmen.observability.tracker import TaskTracker

        store = _make_mock_store()
        tracker = TaskTracker(_mock_settings(), document_store=store)
        evaluator = OperativeEvaluator(project_id="p")

        with patch.object(OperativeEvaluator, "_run_vertex_evaluation", side_effect=RuntimeError("offline")):
            result = await evaluate_and_record(
                evaluator=evaluator,
                tracker=tracker,
                task_id="task-1",
                task_title="Fix login",
                task_description="Users cannot log in",
                report=_make_report(),
            )

        # The diff-signal fallback must still be persisted; the old code
        # dereferenced tracker._collection (always None) and skipped writing
        # whenever evaluation_error was set.
        store.update.assert_called_once()
        _coll, _id, data = store.update.call_args[0]
        assert data["evaluation_scores"]["overall_quality"] == pytest.approx(result.overall_quality)


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------


class TestExperiments:
    @pytest.mark.asyncio
    async def test_skipped_when_not_on_gcp(self):
        from henchmen.observability import experiments

        settings = _settings_for(provider="local", vertex_ai_experiments_enabled=True)
        with patch.object(experiments, "log_experiment_run", new=AsyncMock()) as run:
            await experiments.maybe_log_experiment(settings, {"task_id": "t1"})
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_when_flag_is_off(self):
        from henchmen.observability import experiments

        settings = _settings_for(provider="gcp", vertex_ai_experiments_enabled=False)
        with patch.object(experiments, "log_experiment_run", new=AsyncMock()) as run:
            await experiments.maybe_log_experiment(settings, {"task_id": "t1"})
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_the_module_level_aiplatform_api(self, monkeypatch):
        import contextlib
        import sys
        import types

        from henchmen.observability.experiments import log_experiment_run

        calls: dict = {}

        @contextlib.contextmanager
        def _start_run(run_name, **kwargs):
            calls["run_name"] = run_name
            yield MagicMock()

        # A stub stands in for the (very slow to import) real SDK. The point of
        # the test is the shape of the API call: `vertexai.experiment` does not
        # exist and `aiplatform.Experiment` has no `start_run`.
        fake = types.ModuleType("google.cloud.aiplatform")
        fake.init = lambda **kw: calls.setdefault("init", kw)
        fake.start_run = _start_run
        fake.log_params = lambda p: calls.setdefault("params", p)
        fake.log_metrics = lambda m: calls.setdefault("metrics", m)
        monkeypatch.setitem(sys.modules, "google.cloud.aiplatform", fake)
        # ``from google.cloud import aiplatform`` reads the attribute off the
        # already-imported namespace package, so patching sys.modules alone is
        # not enough once another test has imported the real SDK.
        import google.cloud

        monkeypatch.setattr(google.cloud, "aiplatform", fake, raising=False)

        await log_experiment_run(
            task_id="task-abcdefghijklmnop",
            scheme_id="bugfix_standard",
            model_name="gemini-2.5-pro",
            final_status="pr_created",
            cost_usd=0.42,
            wall_clock_seconds=120.0,
            ci_passed=True,
            evaluation_scores={"overall_quality": 0.8},
            project_id="proj",
            region="us-central1",
            experiment_name="henchmen-operatives",
        )

        assert calls["init"]["experiment"] == "henchmen-operatives"
        assert calls["run_name"] == "task-task-abcdefg"
        assert calls["params"]["model_name"] == "gemini-2.5-pro"
        assert calls["metrics"]["success"] == 1.0
        assert calls["metrics"]["eval_overall_quality"] == 0.8

    @pytest.mark.asyncio
    async def test_completed_status_counts_as_success(self, monkeypatch):
        from henchmen.observability import experiments

        captured: dict = {}

        def _fake(project_id, region, experiment_name, run_name, params, metrics):
            captured.update(metrics)

        monkeypatch.setattr(experiments, "_log_run_blocking", _fake)
        await experiments.log_experiment_run(
            task_id="t1",
            scheme_id="s",
            model_name="m",
            final_status="completed",
            cost_usd=0.0,
            wall_clock_seconds=0.0,
            ci_passed=None,
            evaluation_scores=None,
        )
        assert captured["success"] == 1.0


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------


class TestTracing:
    def test_init_tracing_noops_off_gcp(self, monkeypatch):
        from henchmen.observability import tracing

        monkeypatch.setattr(tracing, "_tracer_provider", None, raising=False)
        monkeypatch.setattr(tracing, "_tracing_provider_is_gcp", lambda: False)

        tracing.init_tracing("mastermind", project_id="")

        assert tracing._tracer_provider is None

    def test_exporter_failure_leaves_no_half_configured_provider(self, monkeypatch):
        pytest.importorskip("opentelemetry.sdk.trace")
        import sys
        import types

        from henchmen.observability import tracing

        monkeypatch.setattr(tracing, "_tracer_provider", None, raising=False)
        monkeypatch.setattr(tracing, "_tracing_provider_is_gcp", lambda: True)

        def _boom(*args, **kwargs):
            raise RuntimeError("no credentials")

        # Stub rather than import the real (slow) Cloud Trace exporter.
        fake = types.ModuleType("opentelemetry.exporter.cloud_trace")
        fake.CloudTraceSpanExporter = _boom
        monkeypatch.setitem(sys.modules, "opentelemetry.exporter.cloud_trace", fake)

        tracing.init_tracing("mastermind", project_id="p")

        # The provider used to be published globally before the exporter was
        # built, so a credentials failure left tracing permanently wedged.
        assert tracing._tracer_provider is None


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


class TestPrimaryModelName:
    def test_picks_the_model_with_the_most_calls(self):
        from henchmen.observability.structured_logging import primary_model_name

        task_data = {
            "node_metrics": {
                "verify_changes": {"model_name": "gemini-2.5-flash", "model_calls": 2},
                "implement_fix": {"model_name": "gemini-2.5-pro", "model_calls": 30},
            }
        }
        assert primary_model_name(task_data) == "gemini-2.5-pro"

    def test_unknown_when_no_model_recorded(self):
        from henchmen.observability.structured_logging import primary_model_name

        assert primary_model_name({}) == "unknown"
        assert primary_model_name({"node_metrics": {"n": {"cost_usd": 0.1}}}) == "unknown"
