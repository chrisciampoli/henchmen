"""Unit tests for CI feedback loop tracker retry methods."""

from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings():
    s = MagicMock()
    s.gcp_project_id = "test-project"
    s.firestore_database = "(default)"
    return s


def _make_mock_store() -> MagicMock:
    """Create a mock DocumentStore with async methods."""
    store = MagicMock()
    store.get = AsyncMock(return_value=None)
    store.set = AsyncMock()
    store.update = AsyncMock()
    store.delete = AsyncMock()
    store.query = AsyncMock(return_value=[])
    return store


def _make_tracker(store: MagicMock | None = None):
    from henchmen.observability.tracker import TaskTracker

    mock_store = store or _make_mock_store()
    return TaskTracker(_mock_settings(), document_store=mock_store)


# ---------------------------------------------------------------------------
# record_ci_fix_attempt
# ---------------------------------------------------------------------------


class TestRecordCIFixAttempt:
    async def test_increments_and_sets_in_progress(self):
        store = _make_mock_store()
        store.get = AsyncMock(return_value={"ci_fix_attempts": 1})
        tracker = _make_tracker(store)
        await tracker.record_ci_fix_attempt("task-123")

        store.update.assert_called_once()
        _coll, _id, update_data = store.update.call_args[0]
        assert update_data["ci_fix_attempts"] == 2  # incremented from 1
        assert update_data["ci_fix_in_progress"] is True

    async def test_firestore_error_does_not_raise(self):
        store = _make_mock_store()
        store.update = AsyncMock(side_effect=Exception("Store down"))
        tracker = _make_tracker(store)
        # Should not raise
        await tracker.record_ci_fix_attempt("task-123")


# ---------------------------------------------------------------------------
# clear_ci_fix_in_progress
# ---------------------------------------------------------------------------


class TestClearCIFixInProgress:
    async def test_sets_in_progress_false(self):
        store = _make_mock_store()
        tracker = _make_tracker(store)
        await tracker.clear_ci_fix_in_progress("task-123")

        store.update.assert_called_once()
        _coll, _id, update_data = store.update.call_args[0]
        assert update_data["ci_fix_in_progress"] is False

    async def test_firestore_error_does_not_raise(self):
        store = _make_mock_store()
        store.update = AsyncMock(side_effect=Exception("Store down"))
        tracker = _make_tracker(store)
        # Should not raise
        await tracker.clear_ci_fix_in_progress("task-123")


# ---------------------------------------------------------------------------
# get_task_by_id_prefix
# ---------------------------------------------------------------------------


class TestGetTaskByIdPrefix:
    async def test_returns_dict_when_found(self):
        store = _make_mock_store()
        store.query = AsyncMock(return_value=[{"task_id": "abc-1234-xyz", "title": "Fix bug"}])
        tracker = _make_tracker(store)

        result = await tracker.get_task_by_id_prefix("abc-1234")
        assert result is not None
        assert result["task_id"] == "abc-1234-xyz"

    async def test_returns_none_when_not_found(self):
        store = _make_mock_store()
        store.query = AsyncMock(return_value=[])
        tracker = _make_tracker(store)

        result = await tracker.get_task_by_id_prefix("nonexistent-prefix")
        assert result is None

    async def test_firestore_error_returns_none(self):
        store = _make_mock_store()
        store.query = AsyncMock(side_effect=Exception("Store down"))
        tracker = _make_tracker(store)

        result = await tracker.get_task_by_id_prefix("abc-1234")
        assert result is None


# ---------------------------------------------------------------------------
# start_task includes ci_fix fields
# ---------------------------------------------------------------------------


class TestStartTaskCIFixFields:
    async def test_start_task_includes_ci_fix_attempts(self):
        from henchmen.models.task import HenchmenTask, TaskContext, TaskSource

        store = _make_mock_store()
        tracker = _make_tracker(store)
        task = HenchmenTask(
            source=TaskSource.SLACK,
            source_id="C01234567/1700000000.000001",
            title="Fix login bug",
            description="Users cannot log in",
            context=TaskContext(repo="org/repo"),
            created_by="user1",
        )
        await tracker.start_task(task, "bugfix_standard")

        store.set.assert_called_once()
        _coll, _id, call_data = store.set.call_args[0]
        assert call_data["ci_fix_attempts"] == 0
        assert call_data["ci_fix_in_progress"] is False


# ---------------------------------------------------------------------------
# Helpers (shared)
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


# ---------------------------------------------------------------------------
# _is_ci_failure_on_henchmen_branch
# ---------------------------------------------------------------------------


class TestCIFailureWebhook:
    def test_detects_failure_on_henchmen_branch(self):
        from henchmen.dispatch.handlers.github import _is_ci_failure_on_henchmen_branch

        payload = {
            "action": "completed",
            "check_suite": {
                "conclusion": "failure",
                "head_branch": "henchmen/abc-123",
            },
        }
        assert _is_ci_failure_on_henchmen_branch(payload) is True

    def test_ignores_success(self):
        from henchmen.dispatch.handlers.github import _is_ci_failure_on_henchmen_branch

        payload = {
            "action": "completed",
            "check_suite": {
                "conclusion": "success",
                "head_branch": "henchmen/abc-123",
            },
        }
        assert _is_ci_failure_on_henchmen_branch(payload) is False

    def test_ignores_non_henchmen_branch(self):
        from henchmen.dispatch.handlers.github import _is_ci_failure_on_henchmen_branch

        payload = {
            "action": "completed",
            "check_suite": {
                "conclusion": "failure",
                "head_branch": "feature/abc-123",
            },
        }
        assert _is_ci_failure_on_henchmen_branch(payload) is False

    def test_ignores_non_completed_action(self):
        from henchmen.dispatch.handlers.github import _is_ci_failure_on_henchmen_branch

        payload = {
            "action": "requested",
            "check_suite": {
                "conclusion": "failure",
                "head_branch": "henchmen/abc-123",
            },
        }
        assert _is_ci_failure_on_henchmen_branch(payload) is False

    @pytest.mark.asyncio
    async def test_handle_publishes_message(self):
        from unittest.mock import AsyncMock

        from henchmen.dispatch.handlers.github import handle_ci_failure_webhook

        settings = MagicMock()
        settings.pubsub_topic_ci_failure = "ci-failure-topic"

        payload = {
            "check_suite": {
                "id": 999,
                "head_branch": "henchmen/task-prefix",
                "head_sha": "abc123",
                "conclusion": "failure",
            },
            "repository": {"full_name": "org/repo"},
        }

        mock_broker = AsyncMock()
        mock_broker.publish = AsyncMock(return_value="msg-ci-1")
        result = await handle_ci_failure_webhook(payload, settings, broker=mock_broker)

        assert result["status"] == "ci_failure_dispatched"
        assert result["task_id_prefix"] == "task-prefix"
        mock_broker.publish.assert_awaited_once()


# ---------------------------------------------------------------------------
# handle_ci_failure on MastermindAgent
# ---------------------------------------------------------------------------


class TestHandleCIFailure:
    def _make_agent(self):
        from henchmen.mastermind.agent import MastermindAgent

        store = _make_mock_store()
        agent = MastermindAgent(settings=_mock_settings(), document_store=store)
        return agent

    @pytest.mark.asyncio
    async def test_dispatches_fix_on_first_failure(self):
        from unittest.mock import AsyncMock
        from unittest.mock import patch as _patch

        with (
            _patch("henchmen.mastermind.agent.extract_ci_errors", new_callable=AsyncMock) as mock_extract,
            _patch("henchmen.mastermind.agent.format_errors_for_operative") as mock_format,
            _patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}),
        ):
            from henchmen.forge.error_extractor import CIError

            mock_extract.return_value = [
                CIError(check_name="lint", file_path="foo.py", line=10, message="err", severity="failure")
            ]
            mock_format.return_value = "## lint\n- `foo.py:10`: err"

            agent = self._make_agent()
            agent.tracker.get_task_by_id_prefix = AsyncMock(
                return_value={
                    "task_id": "full-task-id",
                    "ci_fix_attempts": 0,
                    "ci_fix_in_progress": False,
                }
            )
            agent.tracker.record_ci_fix_attempt = AsyncMock()
            agent.tracker.clear_ci_fix_in_progress = AsyncMock()
            agent.lair_manager.create_lair = AsyncMock(return_value="lair-123")
            agent.lair_manager.wait_for_completion = AsyncMock(return_value={"status": "completed"})

            result = await agent.handle_ci_failure("task-prefix", "org/repo", "henchmen/task-prefix", 999)

            assert result["status"] == "fix_dispatched"
            assert result["attempt"] == 1
            agent.tracker.record_ci_fix_attempt.assert_called_once_with("full-task-id")

    @pytest.mark.asyncio
    async def test_escalates_after_max_retries(self):
        agent = self._make_agent()
        agent.tracker.get_task_by_id_prefix = AsyncMock(
            return_value={
                "task_id": "full-task-id",
                "ci_fix_attempts": 2,
                "ci_fix_in_progress": False,
            }
        )
        agent.tracker.record_ci_result = AsyncMock()

        result = await agent.handle_ci_failure("task-prefix", "org/repo", "henchmen/task-prefix", 999)

        assert result["status"] == "escalated"
        assert "max retries" in result["reason"]
        agent.tracker.record_ci_result.assert_called_once_with("full-task-id", False)

    @pytest.mark.asyncio
    async def test_skips_if_fix_in_progress(self):
        agent = self._make_agent()
        agent.tracker.get_task_by_id_prefix = AsyncMock(
            return_value={
                "task_id": "full-task-id",
                "ci_fix_attempts": 1,
                "ci_fix_in_progress": True,
            }
        )

        result = await agent.handle_ci_failure("task-prefix", "org/repo", "henchmen/task-prefix", 999)

        assert result["status"] == "skipped"
        assert result["reason"] == "fix in progress"

    @pytest.mark.asyncio
    async def test_skips_if_no_errors(self):
        from unittest.mock import patch as _patch

        with (
            _patch("henchmen.mastermind.agent.extract_ci_errors", new_callable=AsyncMock) as mock_extract,
            _patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}),
        ):
            mock_extract.return_value = []

            agent = self._make_agent()
            agent.tracker.get_task_by_id_prefix = AsyncMock(
                return_value={
                    "task_id": "full-task-id",
                    "ci_fix_attempts": 0,
                    "ci_fix_in_progress": False,
                }
            )

            result = await agent.handle_ci_failure("task-prefix", "org/repo", "henchmen/task-prefix", 999)

            assert result["status"] == "skipped"
            assert result["reason"] == "no errors found"

    @pytest.mark.asyncio
    async def test_skips_if_task_not_found(self):
        agent = self._make_agent()
        agent.tracker.get_task_by_id_prefix = AsyncMock(return_value=None)

        result = await agent.handle_ci_failure("nonexistent", "org/repo", "henchmen/nonexistent", 999)

        assert result["status"] == "skipped"
        assert result["reason"] == "task not found"


# ---------------------------------------------------------------------------
# /pubsub/ci-failure endpoint
# ---------------------------------------------------------------------------


class TestCIFailureEndpoint:
    def test_endpoint_exists(self):
        from henchmen.mastermind.server import app

        route_paths = [route.path for route in app.routes]
        assert "/pubsub/ci-failure" in route_paths
