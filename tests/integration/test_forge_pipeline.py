"""Integration tests for the Forge CI pipeline, merge queue, and PR creation.

Uses constructor injection for the ``MessageBroker`` and ``DocumentStore``
dependencies — mirrors the pattern in ``tests/unit/test_forge.py`` and the
``dispatch_client`` integration fixture. Tests no longer reach into the
private ``MergeQueue._client`` attribute that was removed in the E8
provider-abstraction refactor.
"""

import base64
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from henchmen.forge.ci_orchestrator import CIOrchestrator
from henchmen.forge.merge_queue import MergeQueue
from henchmen.forge.pr_builder import PRBuilder
from henchmen.forge.server import app as forge_app

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _mock_broker() -> AsyncMock:
    """Build a minimal async MessageBroker double."""
    broker = AsyncMock()
    broker.publish = AsyncMock(return_value="mock-msg-id")
    return broker


def _mock_document_store() -> AsyncMock:
    """Build a minimal async DocumentStore double with common methods stubbed."""
    store = AsyncMock()
    store.get = AsyncMock(return_value=None)
    store.set = AsyncMock()
    store.update = AsyncMock()
    store.delete = AsyncMock()
    store.query = AsyncMock(return_value=[])
    store.increment = AsyncMock()
    # Default update_if to success so MergeQueue CAS claims return the entry.
    store.update_if = AsyncMock(return_value=True)
    return store


# ---------------------------------------------------------------------------
# TestCIOrchestratorIntegration
# ---------------------------------------------------------------------------


class TestCIOrchestratorIntegration:
    """CI orchestrator exercised with mocked CI provider and broker."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings):
        self.settings = integration_settings
        self.broker = _mock_broker()

    # 1. Parse PR URL correctly
    @pytest.mark.asyncio
    async def test_run_ci_parses_pr_url_correctly(self):
        """run_ci extracts repo='acme-org/sample-repo' and pr_number=42."""
        orchestrator = CIOrchestrator(self.settings, broker=self.broker)
        orchestrator.trigger_build = AsyncMock(return_value="build-42")
        orchestrator.get_build_status = AsyncMock(return_value={"status": "success", "log_url": ""})

        result = await orchestrator.run_ci("https://github.com/acme-org/sample-repo/pull/42", "req-123")

        assert result["repo"] == "acme-org/sample-repo"
        assert result["pr_number"] == 42

    # 2. Trigger CI build with correct repo slug
    @pytest.mark.asyncio
    async def test_run_ci_triggers_cloud_build(self):
        """run_ci calls trigger_build with the correct repo."""
        orchestrator = CIOrchestrator(self.settings, broker=self.broker)
        orchestrator.trigger_build = AsyncMock(return_value="build-007")
        orchestrator.get_build_status = AsyncMock(return_value={"status": "success", "log_url": ""})

        await orchestrator.run_ci("https://github.com/acme-org/sample-repo/pull/7", "req-007")

        orchestrator.trigger_build.assert_called_once()
        call_args = orchestrator.trigger_build.call_args
        assert call_args.args[0] == "acme-org/sample-repo"

    # 3. Publish result to forge-result topic via injected broker
    @pytest.mark.asyncio
    async def test_run_ci_publishes_result_to_forge_result_topic(self):
        """run_ci publishes a message with the correct request_id via the broker."""
        orchestrator = CIOrchestrator(self.settings, broker=self.broker)
        orchestrator.trigger_build = AsyncMock(return_value="build-pub-test")
        orchestrator.get_build_status = AsyncMock(return_value={"status": "success", "log_url": ""})

        await orchestrator.run_ci("https://github.com/acme-org/sample-repo/pull/10", "req-pub-test")

        self.broker.publish.assert_called_once()
        call_args = self.broker.publish.call_args
        topic = call_args.args[0]
        assert "forge-result" in topic
        published = json.loads(call_args.args[1].decode("utf-8"))
        assert published["request_id"] == "req-pub-test"

    # 4. Handle build trigger failure
    @pytest.mark.asyncio
    async def test_run_ci_handles_build_failure(self):
        """When trigger_build raises, run_ci returns status='failed' and still publishes."""
        orchestrator = CIOrchestrator(self.settings, broker=self.broker)
        orchestrator.trigger_build = AsyncMock(side_effect=RuntimeError("CI quota exceeded"))

        result = await orchestrator.run_ci("https://github.com/acme-org/sample-repo/pull/99", "req-fail")

        assert result["status"] == "failed"
        assert "CI quota exceeded" in result["error"]
        self.broker.publish.assert_called_once()

    # 5. Handle invalid PR URL
    @pytest.mark.asyncio
    async def test_run_ci_handles_invalid_pr_url(self):
        """Malformed PR URL returns status='failed' and does not call trigger_build."""
        orchestrator = CIOrchestrator(self.settings, broker=self.broker)
        orchestrator.trigger_build = AsyncMock(return_value="should-not-be-called")

        result = await orchestrator.run_ci("not-a-valid-url", "req-invalid")

        assert result["status"] == "failed"
        assert "request_id" in result
        orchestrator.trigger_build.assert_not_called()


# ---------------------------------------------------------------------------
# TestMergeQueueIntegration
# ---------------------------------------------------------------------------


class TestMergeQueueIntegration:
    """Merge queue exercised with a mocked async DocumentStore."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings):
        self.settings = integration_settings

    # 1. Enqueue writes to the DocumentStore
    @pytest.mark.asyncio
    async def test_enqueue_writes_document(self):
        """enqueue calls DocumentStore.set with pr_url, task_id, and status='pending'."""
        store = _mock_document_store()
        queue = MergeQueue(self.settings, document_store=store)

        entry_id = await queue.enqueue("https://github.com/acme-org/sample-repo/pull/1", "task-enqueue-1")

        assert entry_id != ""
        store.set.assert_called_once()
        call_args = store.set.call_args
        assert call_args.args[0] == "merge_queue"
        assert call_args.args[1] == entry_id
        written = call_args.args[2]
        assert written["pr_url"] == "https://github.com/acme-org/sample-repo/pull/1"
        assert written["task_id"] == "task-enqueue-1"
        assert written["status"] == "pending"

    # 2. FIFO ordering — dequeue returns entries in their enqueue order
    @pytest.mark.asyncio
    async def test_fifo_ordering(self):
        """Dequeue returns pending entries in the order the store returns them (FIFO)."""
        store = _mock_document_store()
        queue = MergeQueue(self.settings, document_store=store)

        pending_entries = [
            {
                "id": f"entry-{i}",
                "pr_url": f"https://github.com/acme-org/sample-repo/pull/{i + 1}",
                "task_id": f"task-fifo-{i}",
                "status": "pending",
                "created_at": datetime(2026, 1, 1, i, 0, 0, tzinfo=UTC),
                "priority": 0,
                "error": None,
            }
            for i in range(3)
        ]

        dequeued_urls: list[str] = []
        for entry in pending_entries:
            # Each dequeue call: expire-stale → [], merging-check → [], pending-check → [entry]
            store.query = AsyncMock(side_effect=[[], [], [entry]])
            result = await queue.dequeue()
            assert result is not None
            dequeued_urls.append(result["pr_url"])

        assert dequeued_urls == [e["pr_url"] for e in pending_entries]

    # 3. Serialization guard blocks parallel merges
    @pytest.mark.asyncio
    async def test_serialization_guard_blocks_parallel_merges(self):
        """If an entry is already 'merging', dequeue returns None."""
        store = _mock_document_store()
        queue = MergeQueue(self.settings, document_store=store)

        merging_entry = {
            "id": "entry-merging",
            "pr_url": "https://github.com/acme-org/sample-repo/pull/1",
            "task_id": "task-m1",
            "status": "merging",
            "created_at": datetime.now(UTC),
            "priority": 0,
            "error": None,
        }

        # expire-stale: empty, merging-check: returns merging entry
        store.query = AsyncMock(side_effect=[[], [merging_entry]])

        result = await queue.dequeue()

        assert result is None

    # 4. mark_merged allows next dequeue
    @pytest.mark.asyncio
    async def test_mark_merged_allows_next_dequeue(self):
        """After mark_merged the store is updated with status='merged'."""
        store = _mock_document_store()
        queue = MergeQueue(self.settings, document_store=store)

        await queue.mark_merged("entry-001")

        store.update.assert_called_once()
        call_args = store.update.call_args
        assert call_args.args[0] == "merge_queue"
        assert call_args.args[1] == "entry-001"
        assert call_args.args[2] == {"status": "merged"}

    # 5. mark_failed allows next dequeue
    @pytest.mark.asyncio
    async def test_mark_failed_records_error(self):
        """mark_failed updates the document with status='failed' and the error message."""
        store = _mock_document_store()
        queue = MergeQueue(self.settings, document_store=store)

        await queue.mark_failed("entry-001", "CI failed: test suite red")

        store.update.assert_called_once()
        call_args = store.update.call_args
        assert call_args.args[0] == "merge_queue"
        assert call_args.args[1] == "entry-001"
        assert call_args.args[2] == {"status": "failed", "error": "CI failed: test suite red"}

    # 6. get_queue_length counts pending docs
    @pytest.mark.asyncio
    async def test_get_queue_length_counts_pending(self):
        """get_queue_length returns the count of pending entries from the store."""
        store = _mock_document_store()
        queue = MergeQueue(self.settings, document_store=store)

        pending_entries = [
            {
                "id": f"entry-{i}",
                "pr_url": f"https://github.com/acme-org/sample-repo/pull/{i}",
                "task_id": f"task-len-{i}",
                "status": "pending",
                "created_at": datetime.now(UTC),
                "priority": 0,
                "error": None,
            }
            for i in range(2)
        ]
        store.query = AsyncMock(return_value=pending_entries)

        length = await queue.get_queue_length()

        assert length == 2

    # 7. Empty queue dequeue returns None
    @pytest.mark.asyncio
    async def test_empty_queue_dequeue_returns_none(self):
        """Dequeue from an empty queue returns None."""
        store = _mock_document_store()
        queue = MergeQueue(self.settings, document_store=store)

        # expire-stale: empty, merging-check: empty, pending-check: empty
        store.query = AsyncMock(return_value=[])

        result = await queue.dequeue()

        assert result is None


# ---------------------------------------------------------------------------
# TestPRBuilderIntegration
# ---------------------------------------------------------------------------


class TestPRBuilderIntegration:
    """PR creation with mocked PyGithub."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings):
        self.settings = integration_settings

    def _make_mock_github(self, pr_url, pr_number, title):
        mock_pr = MagicMock()
        mock_pr.html_url = pr_url
        mock_pr.number = pr_number
        mock_pr.title = title

        mock_repo = MagicMock()
        mock_repo.create_pull = MagicMock(return_value=mock_pr)

        mock_g = MagicMock()
        mock_g.get_repo = MagicMock(return_value=mock_repo)

        return mock_g, mock_repo, mock_pr

    # 1. create_pr returns correct structure
    @pytest.mark.asyncio
    async def test_create_pr_returns_correct_structure(self):
        """create_pr returns a dict with pr_url, pr_number, and title."""
        builder = PRBuilder(self.settings)
        builder._get_token = MagicMock(return_value="fake-token")

        mock_g, _, _ = self._make_mock_github(
            pr_url="https://github.com/acme-org/sample-repo/pull/42",
            pr_number=42,
            title="Fix auth bug",
        )

        with patch("github.Github", return_value=mock_g):
            result = await builder.create_pr(
                repo="acme-org/sample-repo",
                head_branch="fix/auth-bug",
                base_branch="main",
                title="Fix auth bug",
                body="Fixes the 500 error on login.",
                task_id="task-struct-test",
            )

        assert result["pr_url"] == "https://github.com/acme-org/sample-repo/pull/42"
        assert result["pr_number"] == 42
        assert result["title"] == "Fix auth bug"

    # 2. create_pr adds henchmen label
    @pytest.mark.asyncio
    async def test_create_pr_adds_henchmen_label(self):
        """create_pr calls add_to_labels('henchmen-operative') on the created PR."""
        builder = PRBuilder(self.settings)
        builder._get_token = MagicMock(return_value="fake-token")

        mock_g, _, mock_pr = self._make_mock_github(
            pr_url="https://github.com/acme-org/sample-repo/pull/43",
            pr_number=43,
            title="Add feature",
        )

        with patch("github.Github", return_value=mock_g):
            await builder.create_pr(
                repo="acme-org/sample-repo",
                head_branch="feature/new-thing",
                base_branch="main",
                title="Add feature",
                body="Adds a new thing.",
                task_id="task-label-test",
            )

        mock_pr.add_to_labels.assert_called_once_with("henchmen-operative")

    # 3. PR body includes task ID
    @pytest.mark.asyncio
    async def test_pr_body_includes_task_id(self):
        """The PR body submitted to GitHub contains the task ID and Henchmen attribution."""
        builder = PRBuilder(self.settings)
        builder._get_token = MagicMock(return_value="fake-token")

        mock_g, mock_repo, _ = self._make_mock_github(
            pr_url="https://github.com/acme-org/sample-repo/pull/44",
            pr_number=44,
            title="Some PR",
        )

        with patch("github.Github", return_value=mock_g):
            await builder.create_pr(
                repo="acme-org/sample-repo",
                head_branch="feature/task-id-test",
                base_branch="main",
                title="Some PR",
                body="Original body.",
                task_id="task-body-id-check",
            )

        submitted_body = mock_repo.create_pull.call_args.kwargs["body"]
        assert "task-body-id-check" in submitted_body
        assert "Henchmen" in submitted_body

    # 4. PR body preserves the original content verbatim
    @pytest.mark.asyncio
    async def test_pr_body_includes_original_content(self):
        """The original body text is preserved verbatim in the final PR body."""
        builder = PRBuilder(self.settings)
        builder._get_token = MagicMock(return_value="fake-token")

        mock_g, mock_repo, _ = self._make_mock_github(
            pr_url="https://github.com/acme-org/sample-repo/pull/45",
            pr_number=45,
            title="Preserve body test",
        )

        original_body = "This is the original description. It must survive."

        with patch("github.Github", return_value=mock_g):
            await builder.create_pr(
                repo="acme-org/sample-repo",
                head_branch="feature/body-preserve",
                base_branch="main",
                title="Preserve body test",
                body=original_body,
                task_id="task-preserve",
            )

        submitted_body = mock_repo.create_pull.call_args.kwargs["body"]
        assert original_body in submitted_body


# ---------------------------------------------------------------------------
# TestForgeServerIntegration
# ---------------------------------------------------------------------------


class TestForgeServerIntegration:
    """Forge FastAPI server smoke tests.

    ``httpx.AsyncClient`` with ``ASGITransport`` does not run FastAPI lifespan
    hooks, so we wire a minimal mock broker onto ``forge_app.state`` by hand.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings):
        self.settings = integration_settings
        self.broker = _mock_broker()
        forge_app.state.message_broker = self.broker
        yield
        if hasattr(forge_app.state, "message_broker"):
            del forge_app.state.message_broker

    # 1. Health endpoint
    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        """GET /health returns 200 with {'status': 'ok'}."""
        async with AsyncClient(transport=ASGITransport(app=forge_app), base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    # 2. forge-request endpoint accepts a valid Pub/Sub push envelope
    @pytest.mark.asyncio
    async def test_forge_request_endpoint_accepts_pubsub_message(self):
        """POST /pubsub/forge-request with a valid envelope returns 200."""
        payload = {
            "pr_url": "https://github.com/acme-org/sample-repo/pull/55",
            "task_id": "task-server-test",
            "request_id": "req-server-test",
        }
        data_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
        envelope = {
            "message": {
                "data": data_b64,
                "message_id": "msg-server-test",
            },
            "subscription": "projects/test-project/subscriptions/forge-request-sub",
        }

        # Stub out verify_pubsub_oidc + the CI runner so the handler doesn't
        # need GitHub or any real CI.
        with (
            patch("henchmen.forge.server.verify_pubsub_oidc", new_callable=AsyncMock),
            patch("henchmen.forge.server._run_ci_for_pr", new_callable=AsyncMock),
        ):
            async with AsyncClient(transport=ASGITransport(app=forge_app), base_url="http://test") as client:
                response = await client.post("/pubsub/forge-request", json=envelope)

        assert response.status_code == 200
        assert response.json().get("status") == "accepted"
