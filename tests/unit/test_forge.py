"""Unit tests for Forge: MergeQueue and the Forge HTTP/Pub/Sub server.

``CIOrchestrator`` and ``PRBuilder`` were deleted as dead code (nothing in
``src/`` imported them); the live CI path is covered by
``tests/unit/test_ci_runner.py`` plus the server tests below.
"""

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.forge.server import ForgeCIError, app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def forge_settings(mock_settings):
    """Project-wide ``mock_settings`` re-exported as ``forge_settings``.

    Kept as a distinct fixture so that test code reads as ``settings = forge_settings``
    without confusion when someone later adds forge-specific overrides here.
    """
    return mock_settings


def _mock_broker():
    broker = AsyncMock()
    broker.publish = AsyncMock(return_value="msg-id-1")
    return broker


def _mock_document_store():
    store = AsyncMock()
    store.set = AsyncMock()
    store.get = AsyncMock(return_value=None)
    store.update = AsyncMock()
    store.delete = AsyncMock()
    store.query = AsyncMock(return_value=[])
    # D1/D2: new CAS + atomic increment primitives default to success
    store.update_if = AsyncMock(return_value=True)
    store.increment = AsyncMock()
    return store


@pytest.fixture
def forge_app(forge_settings):
    """Install mock providers on ``app.state`` (lifespan does not run in tests)."""
    broker = _mock_broker()
    store = _mock_document_store()
    app.state.message_broker = broker
    app.state.document_store = store
    yield broker, store
    for attr in ("message_broker", "document_store"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def _published(broker):
    """Return the decoded payloads published through a mock broker."""
    return [json.loads(call.args[1].decode("utf-8")) for call in broker.publish.call_args_list]


def _pubsub_envelope(payload: dict, message_id: str = "msg-1") -> dict:
    data = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    return {"message": {"data": data, "messageId": message_id}}


# ===========================================================================
# MergeQueue
# ===========================================================================


class TestMergeQueueEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_writes_to_document_store(self, forge_settings):
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)

        entry_id = await queue.enqueue("https://github.com/acme/repo/pull/1", "task-abc")

        assert entry_id != ""
        store.set.assert_called_once()
        collection, written_id, written = store.set.call_args.args
        assert collection == "merge_queue"
        assert written_id == entry_id
        assert written["pr_url"] == "https://github.com/acme/repo/pull/1"
        assert written["task_id"] == "task-abc"
        assert written["status"] == "pending"

    @pytest.mark.asyncio
    async def test_enqueue_stores_created_at_as_iso_string(self, forge_settings):
        """Datetimes round-trip as ISO strings in SQLite/DynamoDB; store them that way."""
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)

        await queue.enqueue("https://github.com/acme/repo/pull/1", "task-abc")

        created_at = store.set.call_args.args[2]["created_at"]
        assert isinstance(created_at, str)
        assert created_at.endswith("+00:00")

    @pytest.mark.asyncio
    async def test_enqueue_returns_unique_ids(self, forge_settings):
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)

        assert await queue.enqueue("url-1", "t1") != await queue.enqueue("url-2", "t2")


class TestMergeQueueDequeue:
    @pytest.mark.asyncio
    async def test_dequeue_returns_none_when_merge_in_progress(self, forge_settings):
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)
        merging_entry = {"id": "e1", "status": "merging"}

        # expire-stale query: empty, merging-check: one result -> None
        store.query = AsyncMock(side_effect=[[], [merging_entry]])

        assert await queue.dequeue() is None

    @pytest.mark.asyncio
    async def test_dequeue_returns_none_when_queue_empty(self, forge_settings):
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)
        store.query = AsyncMock(return_value=[])

        assert await queue.dequeue() is None

    @pytest.mark.asyncio
    async def test_dequeue_claims_pending_entry_via_update_if(self, forge_settings):
        """Happy path: pending entry found, CAS succeeds, entry returned with status=merging."""
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)
        pending_entry = {
            "id": "e-pending",
            "status": "pending",
            "priority": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
        }

        # expire stale, merging check, pending candidates, sole-claim confirmation
        store.query = AsyncMock(side_effect=[[], [], [pending_entry], [{"id": "e-pending"}]])
        store.update_if = AsyncMock(return_value=True)

        result = await queue.dequeue()

        assert result is not None
        assert result["id"] == "e-pending"
        assert result["status"] == "merging"

        store.update_if.assert_called_once()
        call = store.update_if.call_args
        assert call.args[0] == "merge_queue"
        assert call.args[1] == "e-pending"
        assert call.args[2] == "status"
        assert call.args[3] == "pending"
        new_values = call.args[4]
        assert new_values["status"] == "merging"
        assert isinstance(new_values["merging_started_at"], str)

    @pytest.mark.asyncio
    async def test_dequeue_returns_none_when_cas_conflict_loses(self, forge_settings):
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)
        pending_entry = {"id": "e-contested", "status": "pending", "priority": 0}
        store.query = AsyncMock(side_effect=[[], [], [pending_entry]])
        store.update_if = AsyncMock(return_value=False)

        assert await queue.dequeue() is None
        store.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_dequeue_prefers_higher_priority(self, forge_settings):
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)
        low = {"id": "low", "status": "pending", "priority": 0, "created_at": "2026-01-01T00:00:00+00:00"}
        high = {"id": "high", "status": "pending", "priority": 10, "created_at": "2026-01-02T00:00:00+00:00"}
        store.query = AsyncMock(side_effect=[[], [], [low, high], [{"id": "high"}]])

        result = await queue.dequeue()

        assert result is not None and result["id"] == "high"
        assert store.update_if.call_args.args[1] == "high"

    @pytest.mark.asyncio
    async def test_dequeue_releases_claim_when_another_replica_claimed_first(self, forge_settings):
        """Two replicas must not both hold a merge claim, even on different entries."""
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)
        pending_entry = {"id": "mine", "status": "pending", "priority": 0}
        rival = {"id": "theirs", "status": "merging", "merging_started_at": "2000-01-01T00:00:00+00:00"}
        store.query = AsyncMock(side_effect=[[], [], [pending_entry], [rival, {"id": "mine"}]])

        result = await queue.dequeue()

        assert result is None
        store.update.assert_called_once()
        assert store.update.call_args.args[1] == "mine"
        assert store.update.call_args.args[2]["status"] == "pending"


class TestMergeQueueExpiry:
    @pytest.mark.asyncio
    async def test_expire_uses_iso_string_cutoff(self, forge_settings):
        """A datetime filter value would raise TypeError against ISO-string fields."""
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)
        store.query = AsyncMock(return_value=[])

        await queue.expire_stale_merging()

        filters = store.query.call_args.kwargs["filters"]
        cutoff = next(value for field, op, value in filters if field == "merging_started_at")
        assert isinstance(cutoff, str)

    @pytest.mark.asyncio
    async def test_expire_marks_stale_entries_failed(self, forge_settings):
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)
        store.query = AsyncMock(return_value=[{"id": "stale"}])

        expired = await queue.expire_stale_merging()

        assert expired == 1
        assert store.update.call_args.args[2]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_dequeue_twice_against_real_sqlite_store(self, forge_settings, tmp_path):
        """Regression: the second dequeue used to raise TypeError comparing str < datetime."""
        from henchmen.forge.merge_queue import MergeQueue
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(forge_settings, db_path=str(tmp_path / "queue.db"))
        queue = MergeQueue(forge_settings, document_store=store)

        await queue.enqueue("https://github.com/acme/repo/pull/1", "t1")
        first = await queue.dequeue()
        second = await queue.dequeue()

        assert first is not None and first["status"] == "merging"
        assert second is None  # serialization guard: one merge at a time


class TestMergeQueueMarkMergedAndFailed:
    @pytest.mark.asyncio
    async def test_mark_merged_updates_status(self, forge_settings):
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)

        await queue.mark_merged("entry-001")

        store.update.assert_called_once_with("merge_queue", "entry-001", {"status": "merged"})

    @pytest.mark.asyncio
    async def test_mark_failed_updates_status_and_error(self, forge_settings):
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)

        await queue.mark_failed("entry-002", "Merge conflict")

        store.update.assert_called_once_with(
            "merge_queue", "entry-002", {"status": "failed", "error": "Merge conflict"}
        )


class TestMergeQueueGetQueue:
    @pytest.mark.asyncio
    async def test_get_queue_length_counts_pending(self, forge_settings):
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)
        store.query = AsyncMock(return_value=[{"id": f"e{i}", "status": "pending"} for i in range(3)])

        assert await queue.get_queue_length() == 3

    @pytest.mark.asyncio
    async def test_get_queue_returns_all_entries(self, forge_settings):
        from henchmen.forge.merge_queue import MergeQueue

        store = _mock_document_store()
        queue = MergeQueue(forge_settings, document_store=store)
        store.query = AsyncMock(return_value=[{"id": f"e{i}", "pr_url": f"url-{i}"} for i in range(2)])

        result = await queue.get_queue()

        assert len(result) == 2
        assert result[0]["pr_url"] == "url-0"


# ===========================================================================
# Forge server: failure publishing
# ===========================================================================


class TestPublishCIFailure:
    @pytest.mark.asyncio
    async def test_publishes_to_configured_topic(self, forge_settings, forge_app):
        """Regression: the literal 'forge-result' topic never reached Mastermind."""
        from henchmen.forge.server import _publish_ci_failure

        broker, _store = forge_app

        await _publish_ci_failure("https://github.com/a/b/pull/1", "task-1", "req-1", reason="clone-failed")

        broker.publish.assert_called_once()
        assert broker.publish.call_args.args[0] == forge_settings.pubsub_topic_forge_result
        assert broker.publish.call_args.args[0] != "forge-result"
        payload = _published(broker)[0]
        assert payload["status"] == "failed"
        assert payload["reason"] == "clone-failed"


class TestRunCIForPRFailurePaths:
    @pytest.mark.asyncio
    async def test_unparseable_pr_url_publishes_failure(self, forge_settings, forge_app):
        from henchmen.forge.server import _run_ci_for_pr

        broker, _store = forge_app

        with pytest.raises(ForgeCIError) as exc_info:
            await _run_ci_for_pr("https://github.com/acme/pull/", "task-1", "req-1")

        assert exc_info.value.published is True
        assert exc_info.value.retriable is False
        payload = _published(broker)[0]
        assert payload["status"] == "failed"
        assert payload["reason"] == "parse-error"

    @pytest.mark.asyncio
    async def test_missing_github_token_outside_dev_fails_closed(self, forge_app):
        from henchmen.config.settings import Environment, Settings
        from henchmen.forge.server import _run_ci_for_pr

        broker, _store = forge_app
        # Explicit instance: a developer's .env.local must not make this test pass.
        staging = Settings(environment=Environment.STAGING, github_token="", gcp_project_id="test-project")

        with (
            patch("henchmen.forge.server.get_settings", return_value=staging),
            pytest.raises(ForgeCIError) as exc_info,
        ):
            await _run_ci_for_pr("https://github.com/acme/repo/pull/7", "task-1", "req-1")

        assert exc_info.value.retriable is False
        assert _published(broker)[0]["reason"] == "missing-github-token"

    @pytest.mark.asyncio
    async def test_github_lookup_failure_publishes_retriable_failure(self, forge_settings, forge_app, monkeypatch):
        from henchmen.forge.server import _run_ci_for_pr

        broker, _store = forge_app
        monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", "gh-token")

        failing_client = MagicMock()
        failing_client.get_repo.side_effect = RuntimeError("404 Not Found")

        with patch("github.Github", return_value=failing_client), pytest.raises(ForgeCIError) as exc_info:
            await _run_ci_for_pr("https://github.com/acme/repo/pull/7", "task-1", "req-1")

        assert exc_info.value.retriable is True
        assert _published(broker)[0]["reason"] == "github-api-error"

    @pytest.mark.asyncio
    async def test_clone_failure_publishes_failure(self, forge_settings, forge_app, monkeypatch):
        from henchmen.forge.server import _run_ci_for_pr

        broker, _store = forge_app
        monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", "gh-token")

        with (
            patch("github.Github", return_value=_github_client_stub()),
            patch(
                "henchmen.forge.server.clone_repo",
                new=AsyncMock(side_effect=RuntimeError("git clone failed: ***")),
            ),
            pytest.raises(ForgeCIError) as exc_info,
        ):
            await _run_ci_for_pr("https://github.com/acme/repo/pull/7", "task-1", "req-1")

        assert exc_info.value.published is True
        assert _published(broker)[0]["reason"] == "clone-failed"

    @pytest.mark.asyncio
    async def test_ci_runner_exception_publishes_failure(self, forge_settings, forge_app, monkeypatch):
        from henchmen.forge.server import _run_ci_for_pr

        broker, _store = forge_app
        monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", "gh-token")
        runner = MagicMock()
        runner.run = AsyncMock(side_effect=OSError("disk full"))

        with (
            patch("github.Github", return_value=_github_client_stub()),
            patch("henchmen.forge.server.clone_repo", new=AsyncMock()),
            patch("henchmen.forge.ci_runner.CIRunner", return_value=runner),
            pytest.raises(ForgeCIError),
        ):
            await _run_ci_for_pr("https://github.com/acme/repo/pull/7", "task-1", "req-1")

        assert _published(broker)[0]["reason"] == "ci-error"


def _github_client_stub(pr=None):
    """A PyGithub client stub whose PR exposes head/base refs."""
    pr = pr or MagicMock()
    pr.head.ref = "feature-branch"
    pr.base.ref = "main"
    repo = MagicMock()
    repo.get_pull.return_value = pr
    client = MagicMock()
    client.get_repo.return_value = repo
    return client


class TestRunCIForPRSuccess:
    @pytest.mark.asyncio
    async def test_skipped_checks_are_reported_not_swallowed(self, forge_settings, forge_app, monkeypatch):
        from henchmen.forge.server import _run_ci_for_pr

        broker, _store = forge_app
        monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", "gh-token")
        pr = MagicMock()
        client = _github_client_stub(pr)

        runner = MagicMock()
        runner.run = AsyncMock(
            return_value={
                "passed": False,
                "incomplete": True,
                "failed": [],
                "skipped": ["tests"],
                "summary": "SKIP: tests",
                "checks": [
                    {"name": "tests", "status": "skipped", "passed": False, "output": "", "error": "no npm"},
                ],
            }
        )

        with (
            patch("github.Github", return_value=client),
            patch("henchmen.forge.server.clone_repo", new=AsyncMock()) as mock_clone,
            patch("henchmen.forge.ci_runner.CIRunner", return_value=runner),
        ):
            await _run_ci_for_pr("https://github.com/acme/repo/pull/7", "task-1", "req-1")

        # The clone must be deep enough for a merge base with the PR base branch.
        assert mock_clone.call_args.kwargs["depth"] > 1
        # The base ref drives the changed-file scope.
        assert runner.run.call_args.kwargs["base_ref"] == "main"

        payload = _published(broker)[0]
        # A check that never ran must never be published as a CI pass.
        assert payload["status"] == "incomplete"
        assert payload["skipped"] == ["tests"]

        comment = pr.create_issue_comment.call_args.args[0]
        assert "skipped" in comment.lower()
        assert "INCOMPLETE" in comment
        assert "PASSED" not in comment


class TestResultStatus:
    def test_passed_only_when_everything_ran_and_passed(self):
        from henchmen.forge.server import _result_status

        assert _result_status({"passed": True, "failed": [], "skipped": []}) == "passed"

    def test_skip_only_is_incomplete(self):
        from henchmen.forge.server import _result_status

        assert _result_status({"passed": False, "incomplete": True, "failed": [], "skipped": ["tests"]}) == "incomplete"

    def test_failure_wins_over_skip(self):
        from henchmen.forge.server import _result_status

        result = {"passed": False, "incomplete": False, "failed": ["lint"], "skipped": ["tests"]}
        assert _result_status(result) == "failed"


# ===========================================================================
# Forge server: Pub/Sub handler
# ===========================================================================


class TestForgeRequestHandler:
    def test_non_retriable_failure_is_acked_without_double_publish(self, forge_settings, forge_app):
        from fastapi.testclient import TestClient

        broker, _store = forge_app
        error = ForgeCIError("parse-error: bad url", published=True, retriable=False)

        with (
            patch("henchmen.forge.server.verify_pubsub_oidc", new=AsyncMock()),
            patch("henchmen.forge.server._run_ci_for_pr", new=AsyncMock(side_effect=error)),
        ):
            resp = TestClient(app).post(
                "/pubsub/forge-request",
                json=_pubsub_envelope({"pr_url": "https://github.com/a/b/pull/1", "task_id": "t1"}),
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"
        broker.publish.assert_not_called()

    def test_unexpected_exception_publishes_failure_and_500s(self, forge_settings, forge_app):
        from fastapi.testclient import TestClient

        broker, _store = forge_app

        with (
            patch("henchmen.forge.server.verify_pubsub_oidc", new=AsyncMock()),
            patch("henchmen.forge.server._run_ci_for_pr", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            resp = TestClient(app).post(
                "/pubsub/forge-request",
                json=_pubsub_envelope({"pr_url": "https://github.com/a/b/pull/1", "task_id": "t1"}),
            )

        assert resp.status_code == 500
        assert _published(broker)[0]["reason"] == "forge-exception"

    def test_request_id_falls_back_to_local_broker_message_id(self, forge_settings, forge_app):
        """The in-memory broker spells it `messageId`; correlation must not read 'unknown'."""
        from fastapi.testclient import TestClient

        seen = {}

        async def _capture(pr_url, task_id, request_id):
            seen["request_id"] = request_id

        with (
            patch("henchmen.forge.server.verify_pubsub_oidc", new=AsyncMock()),
            patch("henchmen.forge.server._run_ci_for_pr", new=AsyncMock(side_effect=_capture)),
        ):
            resp = TestClient(app).post(
                "/pubsub/forge-request",
                json=_pubsub_envelope({"pr_url": "https://github.com/a/b/pull/1", "task_id": "t1"}, "local-42"),
            )

        assert resp.status_code == 200
        assert seen["request_id"] == "local-42"

    def test_build_complete_requires_oidc(self, forge_settings, forge_app):
        """Every /pubsub/* handler is OIDC-verified; this one used to be open."""
        from fastapi import HTTPException
        from fastapi.testclient import TestClient

        with patch(
            "henchmen.forge.server.verify_pubsub_oidc",
            new=AsyncMock(side_effect=HTTPException(status_code=401, detail="unauthorized")),
        ):
            resp = TestClient(app).post("/pubsub/build-complete", json=_pubsub_envelope({"id": "b1"}))

        assert resp.status_code == 401


# ===========================================================================
# Process Queue Endpoint
# ===========================================================================


class TestProcessQueueEndpoint:
    def test_process_queue_reports_real_counts(self, forge_settings, forge_app):
        from fastapi.testclient import TestClient

        _broker, store = forge_app
        # expire_stale_merging finds one stale entry; get_queue_length finds two pending.
        store.query = AsyncMock(side_effect=[[{"id": "stale"}], [{"id": "p1"}, {"id": "p2"}]])

        resp = TestClient(app).post("/api/v1/process-queue")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["processed"] == 1
        assert data["pending"] == 2
