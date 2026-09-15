"""Unit tests for the Mastermind HTTP service (``henchmen.mastermind.server``).

Every endpoint is driven through FastAPI's ``TestClient`` without running the
lifespan, with ``get_agent`` and the Pub/Sub OIDC check patched out, so no
provider client is ever built.
"""

import base64
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from henchmen.models.task import HenchmenTask, TaskContext, TaskSource


def _task(**overrides: Any) -> HenchmenTask:
    defaults: dict[str, Any] = {
        "id": "task-abcdef01",
        "source": TaskSource.SLACK,
        "source_id": "C123/1700000000.1",
        "title": "Fix login crash",
        "description": "Users report a crash when logging in",
        "context": TaskContext(repo="acme/webapp", branch="main"),
        "created_by": "user@test.com",
    }
    defaults.update(overrides)
    return HenchmenTask(**defaults)


def _envelope(payload: dict[str, Any], message_id: str = "msg-1") -> dict[str, Any]:
    data = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return {"message": {"data": data, "messageId": message_id, "attributes": {}}}


def _agent() -> MagicMock:
    store = MagicMock()
    store.get = AsyncMock(return_value=None)
    store.set = AsyncMock()
    agent = MagicMock()
    agent.settings.pubsub_topic_dead_letter = "henchmen-dev-dead-letter"
    agent.settings.dead_letter_subscription = ""
    agent.tracker = MagicMock()
    agent.tracker._store = store
    agent.tracker.get_task = AsyncMock(return_value=None)
    agent.tracker.mark_escalated = AsyncMock()
    agent.lair_manager = MagicMock()
    return agent


class _DictBackedStore:
    """A DocumentStore fake whose ``get`` returns whatever ``set`` last stored.

    The plain ``MagicMock`` store used elsewhere in this file always returns
    ``None`` from ``get`` regardless of prior ``set`` calls, which is fine for
    tests that only inspect *what* was written. A dedup cross-request test
    needs the real read-your-writes behaviour instead -- two requests must
    observe each other's dedup markers -- or it would pass even with the dedup
    keying completely broken.
    """

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict[str, Any]] = {}

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        return self._data.get((collection, doc_id))

    async def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self._data[(collection, doc_id)] = data

    async def delete(self, collection: str, doc_id: str) -> None:
        self._data.pop((collection, doc_id), None)


@pytest.fixture
def agent() -> Iterator[MagicMock]:
    fake = _agent()
    with (
        patch("henchmen.mastermind.server.get_agent", return_value=fake),
        patch("henchmen.mastermind.server.verify_pubsub_oidc", new_callable=AsyncMock),
        patch("henchmen.mastermind.server.verify_operative_report", new_callable=AsyncMock),
    ):
        yield fake


@pytest.fixture
def client(agent: MagicMock) -> TestClient:
    from henchmen.mastermind.server import app

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# task-intake
# ---------------------------------------------------------------------------


class TestTaskIntake:
    def test_handle_task_crash_returns_500_and_leaves_marker_in_flight(self, client, agent):
        """The 500/retry path must be reachable — it used to be swallowed twice."""
        agent.handle_task = AsyncMock(side_effect=RuntimeError("tracker down"))

        resp = client.post("/pubsub/task-intake", json=_envelope(_task().model_dump(mode="json")))

        assert resp.status_code == 500
        agent.tracker.mark_escalated.assert_awaited_once()
        statuses = [call.args[2]["status"] for call in agent.tracker._store.set.await_args_list]
        assert statuses == ["in_flight"]

    def test_slack_failure_after_completion_does_not_trigger_redelivery(self, client, agent):
        agent.handle_task = AsyncMock(return_value={"status": "completed", "scheme_id": "bugfix_standard"})

        with patch("henchmen.mastermind.server._notify_slack", new_callable=AsyncMock, side_effect=RuntimeError("x")):
            resp = client.post("/pubsub/task-intake", json=_envelope(_task().model_dump(mode="json")))

        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
        statuses = [call.args[2]["status"] for call in agent.tracker._store.set.await_args_list]
        assert statuses == ["in_flight", "done"]

    @pytest.mark.parametrize(
        ("task_metrics", "expected_model"),
        [
            (
                {
                    "estimated_cost_usd": 0.5,
                    "wall_clock_seconds": 12.0,
                    "node_metrics": {
                        "plan": {"model_name": "gemini-2.5-flash", "model_calls": 1},
                        "implement_fix": {"model_name": "gemini-2.5-pro", "model_calls": 7},
                    },
                },
                "gemini-2.5-pro",
            ),
            (None, "unknown"),
        ],
    )
    def test_task_completed_metric_carries_the_primary_model(self, client, agent, task_metrics, expected_model):
        """Cloud Monitoring breaks spend down by model; an empty label made that impossible."""
        agent.handle_task = AsyncMock(return_value={"status": "completed", "scheme_id": "bugfix_standard"})
        agent.tracker.get_task = AsyncMock(return_value=task_metrics)

        with (
            patch("henchmen.mastermind.server._notify_slack", new_callable=AsyncMock),
            patch("henchmen.observability.structured_logging.emit_task_completed") as emit,
        ):
            resp = client.post("/pubsub/task-intake", json=_envelope(_task().model_dump(mode="json")))

        assert resp.status_code == 200
        assert emit.call_args.kwargs["model_name"] == expected_model


# ---------------------------------------------------------------------------
# operative-complete dedup markers
# ---------------------------------------------------------------------------


class TestOperativeComplete:
    def test_marker_is_upgraded_to_done_with_its_handler_and_processed_at(self, client, agent):
        now = datetime.now(UTC).isoformat()
        report = {
            "task_id": "task-abcdef01",
            "scheme_id": "bugfix_standard",
            "node_id": "implement_fix",
            "operative_id": "lair-1",
            "status": "completed",
            "summary": "done",
            "confidence_score": 0.9,
            "started_at": now,
            "completed_at": now,
        }

        resp = client.post("/pubsub/operative-complete", json=_envelope(report, message_id="op-1"))

        assert resp.status_code == 200
        markers = [
            call.args[2] for call in agent.tracker._store.set.await_args_list if call.args[0] == "processed_messages"
        ]
        assert [m["status"] for m in markers] == ["in_flight", "done"]
        # Cleanup filters on processed_at, so the in_flight marker needs it too.
        assert all("processed_at" in m for m in markers)
        assert all(m["handler"] == "operative-complete" for m in markers)
        agent.lair_manager.notify_operative_complete.assert_called_once()


# ---------------------------------------------------------------------------
# ci-failure
# ---------------------------------------------------------------------------


class TestCIFailure:
    def test_handler_exception_returns_500_for_redelivery(self, client, agent):
        agent.handle_ci_failure = AsyncMock(side_effect=RuntimeError("boom"))

        resp = client.post("/pubsub/ci-failure", json=_envelope({"task_id_prefix": "abcdef01"}))

        assert resp.status_code == 500

    def test_escalation_notifies_the_requester(self, client, agent):
        task = _task()
        agent.handle_ci_failure = AsyncMock(
            return_value={"status": "escalated", "task_id": task.id, "reason": "CI still failing"}
        )
        agent.tracker.get_task = AsyncMock(
            return_value={"task_payload": task.model_dump(mode="json"), "scheme_id": "bugfix_standard"}
        )

        with patch("henchmen.mastermind.server._notify_slack", new_callable=AsyncMock) as notify:
            resp = client.post("/pubsub/ci-failure", json=_envelope({"task_id_prefix": "abcdef01"}))

        assert resp.status_code == 200
        notify.assert_awaited_once()
        notified_task, result = notify.await_args.args
        assert notified_task.id == task.id
        assert result["status"] == "escalated"
        assert result["error"] == "CI still failing"


# ---------------------------------------------------------------------------
# embed-request
# ---------------------------------------------------------------------------


class TestEmbedRequest:
    _PIPELINE = "henchmen.dossier.embed_pipeline.run_embedding_pipeline"

    def test_completed_run_is_acknowledged(self, client):
        pipeline = AsyncMock(return_value={"status": "completed", "chunks_upserted": 4, "commit_sha": "abc"})
        with patch(self._PIPELINE, pipeline):
            resp = client.post(
                "/pubsub/embed-request",
                json=_envelope({"repo": "acme/webapp", "commit_sha": "abc123", "mode": "incremental"}),
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
        repo, mode, _settings = pipeline.await_args.args
        assert (repo, mode) == ("acme/webapp", "incremental")
        # The pushed head is not a diff base: incremental runs diff from the last indexed commit.
        assert "commit_sha" not in pipeline.await_args.kwargs

    def test_failed_run_is_not_acknowledged(self, client):
        """A partial index must be redelivered (and finally dead-lettered), never acked."""
        pipeline = AsyncMock(return_value={"status": "failed", "error": "3 of 9 chunks failed to upload"})
        with patch(self._PIPELINE, pipeline):
            resp = client.post("/pubsub/embed-request", json=_envelope({"repo": "acme/webapp"}))

        assert resp.status_code == 500
        assert "3 of 9 chunks failed" in resp.json()["detail"]

    def test_pipeline_exception_is_not_acknowledged(self, client):
        with patch(self._PIPELINE, AsyncMock(side_effect=RuntimeError("git missing"))):
            resp = client.post("/pubsub/embed-request", json=_envelope({"repo": "acme/webapp"}))

        assert resp.status_code == 500

    @pytest.mark.parametrize(
        "envelope",
        [
            {"message": {"data": "!!not base64!!"}},
            _envelope({"mode": "full"}),
            _envelope({"repo": "acme/webapp", "mode": "sideways"}),
        ],
    )
    def test_malformed_message_is_rejected_without_running(self, client, envelope):
        pipeline = AsyncMock()
        with patch(self._PIPELINE, pipeline):
            resp = client.post("/pubsub/embed-request", json=envelope)

        assert resp.status_code == 400
        pipeline.assert_not_awaited()

    def test_requires_pubsub_oidc(self):
        from fastapi import HTTPException

        from henchmen.mastermind.server import app

        pipeline = AsyncMock()
        with (
            patch(
                "henchmen.mastermind.server.verify_pubsub_oidc",
                new=AsyncMock(side_effect=HTTPException(status_code=401, detail="unauthorized")),
            ),
            patch(self._PIPELINE, pipeline),
        ):
            resp = TestClient(app).post("/pubsub/embed-request", json=_envelope({"repo": "acme/webapp"}))

        assert resp.status_code == 401
        pipeline.assert_not_awaited()


# ---------------------------------------------------------------------------
# check-dlq
# ---------------------------------------------------------------------------


class TestCheckDLQ:
    def test_dead_lettered_tasks_are_escalated_before_ack(self, client, agent):
        broker = MagicMock()
        broker.pull_dlq = AsyncMock(
            return_value=[
                {"data": json.dumps({"id": "task-from-payload"}), "attributes": {}},
                {"data": "{}", "attributes": {"task_id": "task-from-attribute"}},
                {"data": "not json", "attributes": {}},
            ]
        )
        agent._get_broker = MagicMock(return_value=broker)

        resp = client.post("/api/v1/check-dlq")

        assert resp.status_code == 200
        assert resp.json() == {"dead_letter_count": 3, "escalated": 2}
        broker.pull_dlq.assert_awaited_once_with("henchmen-dev-dead-letter-sub", max_messages=10)
        escalated_ids = [call.args[0] for call in agent.tracker.mark_escalated.await_args_list]
        assert escalated_ids == ["task-from-payload", "task-from-attribute"]

    def test_explicit_subscription_setting_wins(self, client, agent):
        agent.settings.dead_letter_subscription = "custom-dlq-sub"
        broker = MagicMock()
        broker.pull_dlq = AsyncMock(return_value=[])
        agent._get_broker = MagicMock(return_value=broker)

        resp = client.post("/api/v1/check-dlq")

        assert resp.status_code == 200
        broker.pull_dlq.assert_awaited_once_with("custom-dlq-sub", max_messages=10)

    def test_failed_pull_is_reported_not_counted(self, client, agent):
        broker = MagicMock()
        broker.pull_dlq = AsyncMock(side_effect=RuntimeError("permission denied"))
        agent._get_broker = MagicMock(return_value=broker)

        resp = client.post("/api/v1/check-dlq")

        assert resp.status_code == 503
        assert "permission denied" in resp.json()["detail"]["error"]


# ---------------------------------------------------------------------------
# shared providers
# ---------------------------------------------------------------------------


class TestSharedProviders:
    @pytest.fixture
    def fresh_server(self, monkeypatch):
        """Reset the agent singleton and app.state providers around each test."""
        from henchmen.mastermind import server

        for name in ("message_broker", "document_store", "container_orchestrator"):
            if hasattr(server.app.state, name):
                monkeypatch.delattr(server.app.state, name)
        monkeypatch.setattr(server, "_agent", None)
        settings = MagicMock()
        settings.pubsub_topic_dead_letter = "henchmen-dev-dead-letter"
        settings.dead_letter_subscription = ""
        monkeypatch.setattr(server, "get_settings", lambda: settings)
        monkeypatch.setattr(server, "MastermindAgent", _FakeAgent)
        registry_cls = MagicMock()
        registry_cls.return_value.get_message_broker.side_effect = lambda: _broker()
        monkeypatch.setattr("henchmen.providers.registry.ProviderRegistry", registry_cls)
        yield server, registry_cls
        for name in ("message_broker", "document_store", "container_orchestrator"):
            if hasattr(server.app.state, name):
                delattr(server.app.state, name)

    def test_requests_share_one_message_broker(self, fresh_server):
        server, registry_cls = fresh_server
        client = TestClient(server.app, raise_server_exceptions=False)

        first = client.post("/api/v1/check-dlq")
        second = client.post("/api/v1/check-dlq")

        assert first.status_code == second.status_code == 200
        assert registry_cls.return_value.get_message_broker.call_count == 1
        assert server.app.state.message_broker.pull_dlq.await_count == 2

    @pytest.mark.asyncio
    async def test_close_providers_closes_the_broker(self, fresh_server):
        server, _registry_cls = fresh_server
        broker = _broker()
        server.app.state.message_broker = broker

        await server._close_providers()

        broker.aclose.assert_awaited_once()


def _broker() -> MagicMock:
    broker = MagicMock()
    broker.pull_dlq = AsyncMock(return_value=[])
    broker.aclose = AsyncMock()
    return broker


class _FakeAgent:
    """Stands in for MastermindAgent: keeps the injected providers, builds nothing."""

    def __init__(self, settings: Any, broker: Any, document_store: Any, container_orchestrator: Any) -> None:
        self.settings = settings
        self._broker = broker
        self.tracker = MagicMock()
        self.tracker.mark_escalated = AsyncMock()

    def _get_broker(self) -> Any:
        return self._broker


# ---------------------------------------------------------------------------
# watchdog lease identity
# ---------------------------------------------------------------------------


def test_instance_id_is_unique_per_process_not_per_revision():
    """K_REVISION is shared by every replica, so it cannot identify a lease holder on its own."""
    from henchmen.mastermind import server

    prefix, _, suffix = server._INSTANCE_ID.rpartition("-")
    assert prefix
    assert len(suffix) == 8
    int(suffix, 16)


# ---------------------------------------------------------------------------
# watchdog
# ---------------------------------------------------------------------------


class TestWatchdog:
    def test_failed_stalled_query_is_reported_not_counted_as_zero(self, client, agent):
        """A failed query (e.g. a missing Firestore index) must not read as 'nothing stalled'."""
        agent.tracker.get_stalled_tasks = AsyncMock(side_effect=RuntimeError("FailedPrecondition: index required"))

        resp = client.post("/api/v1/watchdog")

        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert detail["status"] == "error"
        assert detail["stalled_found"] is None
        assert "index required" in detail["error"]

    def test_no_stalled_tasks_reports_zero(self, client, agent):
        agent.tracker.get_stalled_tasks = AsyncMock(return_value=[])

        resp = client.post("/api/v1/watchdog")

        assert resp.status_code == 200
        assert resp.json() == {"stalled_found": 0, "recovered": 0, "escalated": 0}


# ---------------------------------------------------------------------------
# maintenance routes on a desktop install (amendment A8, ruling P4)
# ---------------------------------------------------------------------------


class TestMaintenanceRoutesOnDesktop:
    """Amendment A8: routes Cloud Scheduler calls in the cloud need the internal token on a desktop install."""

    def test_watchdog_without_the_internal_token_is_refused(self, client, agent, monkeypatch, tmp_path):
        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
        agent.tracker.get_stalled_tasks = AsyncMock(return_value=[])
        for path in ("/api/v1/watchdog", "/api/v1/check-dlq", "/api/v1/cleanup"):
            assert client.post(path).status_code == 401
        agent.tracker.get_stalled_tasks.assert_not_awaited()

    def test_watchdog_with_the_internal_token_runs(self, client, agent, monkeypatch, tmp_path):
        from henchmen.config.internal_auth import load_internal_auth

        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
        token = load_internal_auth(tmp_path / "secrets").push_token
        agent.tracker.get_stalled_tasks = AsyncMock(return_value=[])
        resp = client.post("/api/v1/watchdog", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    def test_watchdog_without_the_token_is_refused_even_off_the_local_broker(
        self, client, agent, monkeypatch, tmp_path
    ):
        """Ruling P4: the guard is based on ``desktop_internal_auth()`` directly, so it applies to
        every desktop install regardless of what the message broker provider resolves to."""
        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("HENCHMEN_MESSAGE_BROKER_PROVIDER", "gcp")
        agent.tracker.get_stalled_tasks = AsyncMock(return_value=[])
        resp = client.post("/api/v1/watchdog")
        assert resp.status_code == 401
        agent.tracker.get_stalled_tasks.assert_not_awaited()

    def test_forge_process_queue_without_the_internal_token_is_refused(self, monkeypatch, tmp_path):
        from henchmen.forge.server import app as forge_app

        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
        resp = TestClient(forge_app, raise_server_exceptions=False).post("/api/v1/process-queue")
        assert resp.status_code == 401


class TestOperativeReportOnDesktop:
    """Amendment A2: an operative authenticates its report with its own task token, not the push token."""

    @staticmethod
    def _report(task_id: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        return {
            "task_id": task_id,
            "scheme_id": "bugfix_standard",
            "node_id": "implement_fix",
            "operative_id": "lair-1",
            "status": "completed",
            "summary": "done",
            "confidence_score": 0.9,
            "started_at": now,
            "completed_at": now,
        }

    @pytest.fixture
    def desktop_client(self, monkeypatch, tmp_path):
        from henchmen.config.internal_auth import load_internal_auth
        from henchmen.mastermind.server import app

        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
        fake = _agent()
        with patch("henchmen.mastermind.server.get_agent", return_value=fake):
            yield TestClient(app, raise_server_exceptions=False), fake, load_internal_auth(tmp_path / "secrets")

    def test_own_task_token_delivers_the_report(self, desktop_client):
        client, fake, internal = desktop_client
        token = internal.task_token("task-abcdef01")
        resp = client.post(
            "/pubsub/operative-complete",
            json=_envelope(self._report("task-abcdef01")),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        fake.lair_manager.notify_operative_complete.assert_called_once()

    @pytest.mark.parametrize("header_for", ["other-task", None])
    def test_foreign_or_missing_token_is_refused(self, desktop_client, header_for):
        client, fake, internal = desktop_client
        headers = {"Authorization": f"Bearer {internal.task_token(header_for)}"} if header_for else {}
        resp = client.post("/pubsub/operative-complete", json=_envelope(self._report("task-abcdef01")), headers=headers)
        assert resp.status_code == 401
        fake.lair_manager.notify_operative_complete.assert_not_called()

    def test_task_token_cannot_publish_a_task(self, desktop_client):
        client, fake, internal = desktop_client
        resp = client.post(
            "/pubsub/task-intake",
            json=_envelope({"id": "task-abcdef01"}),
            headers={"Authorization": f"Bearer {internal.task_token('task-abcdef01')}"},
        )
        assert resp.status_code == 401

    def test_malformed_envelope_with_a_valid_looking_task_token_is_refused(self, desktop_client):
        """A body that doesn't decode to the expected envelope shape is 401, whatever the bearer looks like."""
        client, fake, internal = desktop_client
        token = internal.task_token("task-abcdef01")
        resp = client.post(
            "/pubsub/operative-complete",
            content=b"not an envelope",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
        )
        assert resp.status_code == 401
        fake.lair_manager.notify_operative_complete.assert_not_called()

    def test_push_token_delivers_the_report(self, desktop_client):
        """The internal push token remains a valid credential for this endpoint (e.g. a resend path)."""
        client, fake, internal = desktop_client
        resp = client.post(
            "/pubsub/operative-complete",
            json=_envelope(self._report("task-abcdef01")),
            headers={"Authorization": f"Bearer {internal.push_token}"},
        )
        assert resp.status_code == 200
        fake.lair_manager.notify_operative_complete.assert_called_once()

    def test_dedup_key_is_scoped_to_the_verified_task(self, desktop_client):
        """Ruling: the operative-complete dedup key is prefixed with the verified task id on the
        task-token path, so a colliding message_id cannot be replayed across tasks."""
        client, fake, internal = desktop_client
        token = internal.task_token("task-abcdef01")
        resp = client.post(
            "/pubsub/operative-complete",
            json=_envelope(self._report("task-abcdef01"), message_id="op-dedup-1"),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        keys = [
            call.args[1] for call in fake.tracker._store.set.await_args_list if call.args[0] == "processed_messages"
        ]
        assert "task-abcdef01:op-dedup-1" in keys, (
            "the task-token path must record a dedup marker scoped to the verified task id"
        )
        assert "op-dedup-1" not in keys, (
            "the plain message_id must never be claimed on the task-token path -- another task's "
            "operative could reuse the same Pub/Sub message_id and suppress this one's report"
        )

    def test_same_message_id_from_two_different_tasks_is_not_a_duplicate(self, monkeypatch, tmp_path):
        """The scoped dedup key must not let task A's report suppress task B's -- even when both
        happen to carry the same Pub/Sub message_id, which is not scoped to a task at all."""
        from henchmen.config.internal_auth import load_internal_auth
        from henchmen.mastermind.server import app

        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
        internal = load_internal_auth(tmp_path / "secrets")
        fake = _agent()
        fake.tracker._store = _DictBackedStore()
        with patch("henchmen.mastermind.server.get_agent", return_value=fake):
            client = TestClient(app, raise_server_exceptions=False)

            resp_a = client.post(
                "/pubsub/operative-complete",
                json=_envelope(self._report("task-a"), message_id="op-x"),
                headers={"Authorization": f"Bearer {internal.task_token('task-a')}"},
            )
            resp_b = client.post(
                "/pubsub/operative-complete",
                json=_envelope(self._report("task-b"), message_id="op-x"),
                headers={"Authorization": f"Bearer {internal.task_token('task-b')}"},
            )

        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        assert resp_b.json()["status"] != "duplicate"
        assert fake.lair_manager.notify_operative_complete.call_count == 2


class TestOperativeReportTaskIdCrossCheck:
    """Ruling: request.state.operative_task_id is authoritative even if a future change to
    verify_operative_report ever disagreed with the report body -- exercises the
    ``except HTTPException: raise`` path added alongside it."""

    def test_mismatch_between_verified_token_and_report_task_id_is_refused(self, client, agent):
        import henchmen.mastermind.server as server_module

        async def _claims_a_different_task(request, settings):
            request.state.operative_task_id = "some-other-task"

        server_module.verify_operative_report.side_effect = _claims_a_different_task

        report = TestOperativeReportOnDesktop._report("task-abcdef01")
        resp = client.post("/pubsub/operative-complete", json=_envelope(report, message_id="op-mismatch"))

        assert resp.status_code == 401
        agent.lair_manager.notify_operative_complete.assert_not_called()
