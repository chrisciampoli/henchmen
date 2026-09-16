"""Tests for how the Console submits a task to the running services and follows it."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from henchmen.config.settings import Settings
from henchmen.console.task_gateway import (
    MAX_FAILURE_DETAIL,
    PHASES,
    ServiceTaskGateway,
    TaskTimeline,
    build_timeline,
)

TASK_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def _settings() -> Settings:
    return Settings(**{"_env_file": None, "provider": "local"})


@pytest.mark.asyncio
async def test_submit_normalizes_and_publishes_to_task_intake() -> None:
    from henchmen.dispatch.api_models import CreateTaskRequest

    settings = _settings()
    broker = MagicMock()
    broker.publish = AsyncMock(return_value="local-1")
    gateway = ServiceTaskGateway(settings=settings, broker=broker, store=MagicMock())

    task_id = await gateway.submit(
        CreateTaskRequest(title="Add a Quick start section", description="d", repo="acme/webapp", created_by="console")
    )

    topic, data = broker.publish.await_args.args[:2]
    assert topic == settings.pubsub_topic_task_intake
    payload = json.loads(data)
    assert payload["id"] == task_id
    assert payload["source"] == "cli"
    assert payload["created_by"] == "console"
    assert payload["context"]["repo"] == "acme/webapp"


@pytest.mark.asyncio
async def test_execution_reads_the_tracker_document() -> None:
    store = MagicMock()
    store.get = AsyncMock(return_value={"task_id": TASK_ID, "final_status": None})
    gateway = ServiceTaskGateway(settings=_settings(), broker=MagicMock(), store=store)
    assert await gateway.execution(TASK_ID) == {"task_id": TASK_ID, "final_status": None}
    store.get.assert_awaited_once_with("task_executions", TASK_ID)


def _states(timeline: TaskTimeline) -> list[str]:
    return [phase.state for phase in timeline.phases]


def test_phases_are_in_order() -> None:
    assert [phase_id for phase_id, _ in PHASES] == ["queued", "reading_code", "writing_code", "testing", "opening_pr"]


def test_not_started_yet() -> None:
    timeline = build_timeline(TASK_ID, None)
    assert timeline.outcome == "running"
    assert _states(timeline) == ["active", "pending", "pending", "pending", "pending"]
    assert "picked" in timeline.status_message


def test_running_follows_the_latest_node() -> None:
    timeline = build_timeline(
        TASK_ID,
        {
            "nodes_executed": ["create_branch", "prefetch_context"],
            "current_node_id": "implement_feature",
            "final_status": None,
        },
    )
    assert timeline.outcome == "running"
    assert _states(timeline) == ["done", "done", "active", "pending", "pending"]


def test_started_without_checkpoints_is_reading_code() -> None:
    timeline = build_timeline(TASK_ID, {"nodes_executed": [], "current_node_id": None, "final_status": None})
    assert _states(timeline) == ["done", "active", "pending", "pending", "pending"]


def test_success_ends_on_the_pull_request() -> None:
    timeline = build_timeline(
        TASK_ID, {"final_status": "pr_created", "pr_url": "https://github.com/acme/webapp/pull/9", "nodes_executed": []}
    )
    assert timeline.outcome == "succeeded"
    assert _states(timeline) == ["done"] * 5
    assert timeline.pr_url == "https://github.com/acme/webapp/pull/9"
    assert timeline.failure_message is None


def test_escalation_marks_the_phase_that_failed() -> None:
    timeline = build_timeline(
        TASK_ID,
        {
            "final_status": "escalated",
            "nodes_executed": ["create_branch", "implement_fix", "run_tests"],
            "escalation_node": "run_tests",
            "escalation_reason": "tests failed after 2 fix attempts",
        },
    )
    assert timeline.outcome == "failed"
    assert _states(timeline) == ["done", "done", "done", "failed", "pending"]
    assert timeline.failure_message
    assert timeline.failure_detail == "tests failed after 2 fix attempts"


def test_a_timed_out_task_is_never_reported_as_success() -> None:
    timeline = build_timeline(TASK_ID, {"final_status": "timed_out", "nodes_executed": ["create_branch"]})
    assert timeline.outcome == "failed"
    assert timeline.failure_detail == "timed_out"
    assert timeline.pr_url is None


def test_failure_detail_is_redacted_and_capped() -> None:
    timeline = build_timeline(
        TASK_ID,
        {
            "final_status": "escalated",
            "escalation_reason": "push refused for https://x-access-token:ghs_" + "a" * 36 + "@github.com/acme/w.git",
        },
    )
    assert timeline.failure_detail is not None
    assert "ghs_" not in timeline.failure_detail

    long = build_timeline(TASK_ID, {"final_status": "escalated", "escalation_reason": "detail " * 500})
    assert long.failure_detail is not None
    assert len(long.failure_detail) == MAX_FAILURE_DETAIL + 3
