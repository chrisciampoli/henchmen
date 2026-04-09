"""Unit tests for Forge: CIOrchestrator, MergeQueue, PRBuilder."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.providers.interfaces.ci_provider import CIResult, CIStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings():
    s = MagicMock()
    s.gcp_project_id = "test-project"
    s.pubsub_topic_forge_result = "henchmen-forge-result"
    s.firestore_database = "(default)"
    return s


def _mock_broker():
    broker = AsyncMock()
    broker.publish = AsyncMock(return_value="msg-id-1")
    return broker


def _mock_ci_provider():
    provider = AsyncMock()
    provider.trigger_build = AsyncMock(return_value="build-abc-123")
    provider.get_status = AsyncMock(
        return_value=CIResult(build_id="build-abc-123", status=CIStatus.SUCCESS, logs_url="https://logs/url")
    )
    return provider


def _mock_document_store():
    store = AsyncMock()
    store.set = AsyncMock()
    store.get = AsyncMock(return_value=None)
    store.update = AsyncMock()
    store.delete = AsyncMock()
    store.query = AsyncMock(return_value=[])
    return store


# ===========================================================================
# CIOrchestrator
# ===========================================================================


class TestCIOrchestratorTriggerBuild:
    @pytest.mark.asyncio
    async def test_trigger_build_calls_ci_provider(self):
        from henchmen.forge.ci_orchestrator import CIOrchestrator

        settings = _mock_settings()
        ci_provider = _mock_ci_provider()
        orchestrator = CIOrchestrator(settings, ci_provider=ci_provider)

        build_id = await orchestrator.trigger_build("acme/backend", "feature-x", 42)

        assert build_id == "build-abc-123"
        ci_provider.trigger_build.assert_called_once()
        call_kwargs = ci_provider.trigger_build.call_args.kwargs
        assert "acme/backend" in call_kwargs["repo_url"]
        assert call_kwargs["branch"] == "feature-x"

    @pytest.mark.asyncio
    async def test_trigger_build_includes_pr_branch(self):
        from henchmen.forge.ci_orchestrator import CIOrchestrator

        settings = _mock_settings()
        ci_provider = _mock_ci_provider()
        orchestrator = CIOrchestrator(settings, ci_provider=ci_provider)

        await orchestrator.trigger_build("acme/repo", "pr-10", 10)

        ci_provider.trigger_build.assert_called_once()
        call_kwargs = ci_provider.trigger_build.call_args.kwargs
        assert call_kwargs["branch"] == "pr-10"


class TestCIOrchestratorPublishResult:
    @pytest.mark.asyncio
    async def test_publishes_to_correct_topic(self):
        from henchmen.forge.ci_orchestrator import CIOrchestrator

        settings = _mock_settings()
        broker = _mock_broker()
        orchestrator = CIOrchestrator(settings, broker=broker)

        result = {"request_id": "req-1", "status": "passed"}
        await orchestrator._publish_result("req-1", result)

        broker.publish.assert_called_once()
        call_args = broker.publish.call_args
        topic = call_args.args[0]
        assert topic == "henchmen-forge-result"

        data_bytes = call_args.args[1]
        published = json.loads(data_bytes.decode("utf-8"))
        assert published["status"] == "passed"
        assert published["request_id"] == "req-1"

    @pytest.mark.asyncio
    async def test_publishes_with_request_id_attribute(self):
        from henchmen.forge.ci_orchestrator import CIOrchestrator

        settings = _mock_settings()
        broker = _mock_broker()
        orchestrator = CIOrchestrator(settings, broker=broker)

        await orchestrator._publish_result("req-42", {"request_id": "req-42", "status": "failed"})

        call_kwargs = broker.publish.call_args.kwargs
        assert call_kwargs.get("request_id") == "req-42"

    @pytest.mark.asyncio
    async def test_run_ci_publishes_result_on_success(self):
        from henchmen.forge.ci_orchestrator import CIOrchestrator

        settings = _mock_settings()
        ci_provider = _mock_ci_provider()
        broker = _mock_broker()
        orchestrator = CIOrchestrator(settings, ci_provider=ci_provider, broker=broker)

        result = await orchestrator.run_ci("https://github.com/acme/backend/pull/5", "req-99")

        broker.publish.assert_called_once()
        assert result["status"] == CIStatus.SUCCESS.value
        assert result["build_id"] == "build-abc-123"

    @pytest.mark.asyncio
    async def test_run_ci_fails_on_invalid_url(self):
        from henchmen.forge.ci_orchestrator import CIOrchestrator

        settings = _mock_settings()
        broker = _mock_broker()
        orchestrator = CIOrchestrator(settings, broker=broker)

        result = await orchestrator.run_ci("not-a-url", "req-bad")

        assert result["status"] == "failed"
        assert "parse" in result["error"].lower() or "not-a-url" in result["error"]
        broker.publish.assert_called_once()


# ===========================================================================
# MergeQueue
# ===========================================================================


class TestMergeQueueEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_writes_to_document_store(self):
        from henchmen.forge.merge_queue import MergeQueue

        settings = _mock_settings()
        store = _mock_document_store()
        queue = MergeQueue(settings, document_store=store)

        entry_id = await queue.enqueue("https://github.com/acme/repo/pull/1", "task-abc")

        assert entry_id != ""
        store.set.assert_called_once()
        call_args = store.set.call_args
        collection = call_args.args[0]
        written_id = call_args.args[1]
        written = call_args.args[2]
        assert collection == "merge_queue"
        assert written_id == entry_id
        assert written["pr_url"] == "https://github.com/acme/repo/pull/1"
        assert written["task_id"] == "task-abc"
        assert written["status"] == "pending"

    @pytest.mark.asyncio
    async def test_enqueue_returns_unique_ids(self):
        from henchmen.forge.merge_queue import MergeQueue

        settings = _mock_settings()
        store = _mock_document_store()
        queue = MergeQueue(settings, document_store=store)

        id1 = await queue.enqueue("https://github.com/acme/repo/pull/1", "t1")
        id2 = await queue.enqueue("https://github.com/acme/repo/pull/2", "t2")

        assert id1 != id2


class TestMergeQueueDequeue:
    @pytest.mark.asyncio
    async def test_dequeue_returns_none_when_merge_in_progress(self):
        from henchmen.forge.merge_queue import MergeQueue

        settings = _mock_settings()
        store = _mock_document_store()
        queue = MergeQueue(settings, document_store=store)

        merging_entry = {
            "id": "e1",
            "pr_url": "url",
            "task_id": "t1",
            "status": "merging",
            "created_at": None,
            "priority": 0,
            "error": None,
        }

        # _expire_stale_merging query returns empty (no stale entries),
        # then the merging-check query returns a result → should return None
        store.query = AsyncMock(side_effect=[[], [merging_entry]])

        result = await queue.dequeue()

        assert result is None

    @pytest.mark.asyncio
    async def test_dequeue_returns_none_when_queue_empty(self):
        from henchmen.forge.merge_queue import MergeQueue

        settings = _mock_settings()
        store = _mock_document_store()
        queue = MergeQueue(settings, document_store=store)

        # _expire_stale_merging query: no stale, merging-check: empty, pending-check: empty
        store.query = AsyncMock(return_value=[])

        result = await queue.dequeue()

        assert result is None

    @pytest.mark.asyncio
    async def test_dequeue_claims_pending_entry(self):
        from henchmen.forge.merge_queue import MergeQueue

        settings = _mock_settings()
        store = _mock_document_store()
        queue = MergeQueue(settings, document_store=store)

        pending_entry = {
            "id": "e-pending",
            "pr_url": "https://github.com/acme/repo/pull/5",
            "task_id": "t5",
            "status": "pending",
            "created_at": None,
            "priority": 0,
            "error": None,
        }

        # expire stale: empty, merging check: empty, pending check: one result
        store.query = AsyncMock(side_effect=[[], [], [pending_entry]])

        result = await queue.dequeue()

        assert result is not None
        assert result["id"] == "e-pending"
        assert result["status"] == "merging"
        store.update.assert_called_once()
        update_args = store.update.call_args.args
        assert update_args[0] == "merge_queue"
        assert update_args[1] == "e-pending"
        assert update_args[2]["status"] == "merging"


class TestMergeQueueMarkMergedAndFailed:
    @pytest.mark.asyncio
    async def test_mark_merged_updates_status(self):
        from henchmen.forge.merge_queue import MergeQueue

        settings = _mock_settings()
        store = _mock_document_store()
        queue = MergeQueue(settings, document_store=store)

        await queue.mark_merged("entry-001")

        store.update.assert_called_once_with("merge_queue", "entry-001", {"status": "merged"})

    @pytest.mark.asyncio
    async def test_mark_failed_updates_status_and_error(self):
        from henchmen.forge.merge_queue import MergeQueue

        settings = _mock_settings()
        store = _mock_document_store()
        queue = MergeQueue(settings, document_store=store)

        await queue.mark_failed("entry-002", "Merge conflict")

        store.update.assert_called_once_with(
            "merge_queue", "entry-002", {"status": "failed", "error": "Merge conflict"}
        )


class TestMergeQueueGetQueue:
    @pytest.mark.asyncio
    async def test_get_queue_length_counts_pending(self):
        from henchmen.forge.merge_queue import MergeQueue

        settings = _mock_settings()
        store = _mock_document_store()
        queue = MergeQueue(settings, document_store=store)

        entries = [{"id": f"e{i}", "status": "pending"} for i in range(3)]
        store.query = AsyncMock(return_value=entries)

        length = await queue.get_queue_length()

        assert length == 3
        store.query.assert_called_once()
        call_kwargs = store.query.call_args
        assert call_kwargs.kwargs.get("filters") == [("status", "==", "pending")] or (
            len(call_kwargs.args) > 1 and ("status", "==", "pending") in call_kwargs.args[1]
        )

    @pytest.mark.asyncio
    async def test_get_queue_returns_all_entries(self):
        from henchmen.forge.merge_queue import MergeQueue

        settings = _mock_settings()
        store = _mock_document_store()
        queue = MergeQueue(settings, document_store=store)

        entries = [{"id": f"e{i}", "status": "pending", "pr_url": f"url-{i}"} for i in range(2)]
        store.query = AsyncMock(return_value=entries)

        result = await queue.get_queue()

        assert len(result) == 2
        assert result[0]["pr_url"] == "url-0"


# ===========================================================================
# PRBuilder
# ===========================================================================


class TestPRBuilderCreatePR:
    @pytest.mark.asyncio
    async def test_create_pr_with_correct_labels(self):
        from henchmen.forge.pr_builder import PRBuilder

        settings = _mock_settings()
        builder = PRBuilder(settings)
        builder._get_token = MagicMock(return_value="fake-token")

        mock_pr = MagicMock()
        mock_pr.html_url = "https://github.com/acme/repo/pull/10"
        mock_pr.number = 10
        mock_pr.title = "Fix bug"

        mock_github_repo = MagicMock()
        mock_github_repo.create_pull = MagicMock(return_value=mock_pr)

        mock_g = MagicMock()
        mock_g.get_repo = MagicMock(return_value=mock_github_repo)

        with patch("github.Github", return_value=mock_g):
            result = await builder.create_pr(
                repo="acme/repo",
                head_branch="feature-x",
                base_branch="main",
                title="Fix bug",
                body="Bug fix description",
                task_id="task-999",
            )

        assert result["pr_url"] == "https://github.com/acme/repo/pull/10"
        assert result["pr_number"] == 10
        mock_pr.add_to_labels.assert_called_once_with("henchmen-operative")

    @pytest.mark.asyncio
    async def test_create_pr_passes_correct_args(self):
        from henchmen.forge.pr_builder import PRBuilder

        settings = _mock_settings()
        builder = PRBuilder(settings)
        builder._get_token = MagicMock(return_value="fake-token")

        mock_pr = MagicMock()
        mock_pr.html_url = "https://github.com/acme/repo/pull/11"
        mock_pr.number = 11
        mock_pr.title = "New feature"

        mock_github_repo = MagicMock()
        mock_github_repo.create_pull = MagicMock(return_value=mock_pr)

        mock_g = MagicMock()
        mock_g.get_repo = MagicMock(return_value=mock_github_repo)

        with patch("github.Github", return_value=mock_g):
            await builder.create_pr(
                repo="acme/repo",
                head_branch="feat-branch",
                base_branch="develop",
                title="New feature",
                body="Feature body",
                task_id="task-888",
            )

        mock_github_repo.create_pull.assert_called_once_with(
            title="New feature",
            body=builder._build_body("Feature body", "task-888"),
            head="feat-branch",
            base="develop",
        )


class TestPRBuilderBuildBody:
    def test_build_body_includes_task_id(self):
        from henchmen.forge.pr_builder import PRBuilder

        settings = _mock_settings()
        builder = PRBuilder(settings)

        body = builder._build_body("My description", "task-42")
        assert "task-42" in body
        assert "My description" in body

    def test_build_body_includes_henchmen_attribution(self):
        from henchmen.forge.pr_builder import PRBuilder

        settings = _mock_settings()
        builder = PRBuilder(settings)

        body = builder._build_body("Desc", "task-1")
        assert "Henchmen" in body

    def test_build_body_appends_to_original(self):
        from henchmen.forge.pr_builder import PRBuilder

        settings = _mock_settings()
        builder = PRBuilder(settings)

        body = builder._build_body("Original body content", "task-5")
        assert body.startswith("Original body content")


# ===========================================================================
# Process Queue Endpoint
# ===========================================================================


class TestProcessQueueEndpoint:
    def test_process_queue_returns_ok(self):
        from fastapi.testclient import TestClient

        from henchmen.forge.server import app

        client = TestClient(app)
        resp = client.post("/api/v1/process-queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "processed" in data
