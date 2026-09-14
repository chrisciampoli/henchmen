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


@pytest.fixture
def agent() -> Iterator[MagicMock]:
    fake = _agent()
    with (
        patch("henchmen.mastermind.server.get_agent", return_value=fake),
        patch("henchmen.mastermind.server.verify_pubsub_oidc", new_callable=AsyncMock),
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
