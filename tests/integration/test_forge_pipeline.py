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
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from henchmen.forge.merge_queue import MergeQueue
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
            # Each dequeue call queries four times: expire-stale → [],
            # merging-check → [], pending-check → [entry], and finally the
            # post-claim confirmation that no rival replica also claimed → [].
            store.query = AsyncMock(side_effect=[[], [], [entry], []])
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
