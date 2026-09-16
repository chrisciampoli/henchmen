"""Tests for the Console's first-task step."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from henchmen.config.settings import Settings
from henchmen.console.app import ConsoleMode
from henchmen.console.state import SetupStep
from henchmen.console.steps.ai_provider import current_estimate
from henchmen.console.steps.first_task import FIRST_TASK_SUBMITTED_AT_CHOICE, SAMPLE_TASKS
from henchmen.console.task_gateway import QUEUE_TIMEOUT_SECONDS
from henchmen.dispatch.api_models import CreateTaskRequest
from henchmen.providers.tiers import TIER_FIELDS
from tests.unit.console_harness import ConsoleHarness, make_harness

BASE = "/console/api/steps/first_task"
TASK_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


class FakeGateway:
    def __init__(self) -> None:
        self.submitted: list[CreateTaskRequest] = []
        self.executions: dict[str, dict[str, Any]] = {}
        self.broken = False
        self.read_broken = False

    async def submit(self, request: CreateTaskRequest) -> str:
        if self.broken:
            raise RuntimeError("broker unavailable at http://user:hunter2@broker/queue")
        self.submitted.append(request)
        return TASK_ID

    async def execution(self, task_id: str) -> dict[str, Any] | None:
        if self.read_broken:
            raise RuntimeError("document store unavailable")
        return self.executions.get(task_id)


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def harness(tmp_path: Path, gateway: FakeGateway) -> ConsoleHarness:
    harness = make_harness(tmp_path, mode=ConsoleMode.RUN)
    harness.app.state.task_gateway = gateway
    harness.config_store.update({"HENCHMEN_GITHUB_DEFAULT_REPO": "acme/webapp"}, section="GitHub")
    return harness


def _save_provider(harness: ConsoleHarness, *, ceiling: str) -> None:
    """A saved Anthropic provider with its ``Settings`` default models and a per-task limit.

    Model ids come from ``Settings``/``TIER_FIELDS`` rather than being spelled
    out here (ruling PM-5), so a tier default that changes never leaves this
    test pricing a model nobody uses.
    """
    values = {"HENCHMEN_LLM_PROVIDER": "anthropic", "HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD": ceiling}
    for field_name in TIER_FIELDS["anthropic"].values():
        values[f"HENCHMEN_{field_name.upper()}"] = str(Settings.model_fields[field_name].default)
    harness.config_store.update(values, section="LLM")


def _raising(error: Exception) -> Callable[..., Any]:
    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    return fail


def _age_the_submission(harness: ConsoleHarness, *, seconds: float) -> None:
    """Rewrite the recorded submit time so the queue bound can be tested without sleeping."""
    stamp = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    harness.setup_store.set_server_choices({FIRST_TASK_SUBMITTED_AT_CHOICE: stamp})


def test_samples_and_default_repo(harness: ConsoleHarness) -> None:
    details = harness.get(f"{BASE}/samples").json()["details"]
    assert details["default_repo"] == "acme/webapp"
    assert [sample["id"] for sample in details["samples"]] == [sample["id"] for sample in SAMPLE_TASKS]


def test_samples_warn_that_the_test_gate_installs_no_dependencies(harness: ConsoleHarness) -> None:
    """Ruling PI-13 (decision C19): the limitation is stated where a sample is chosen."""
    note = harness.get(f"{BASE}/samples").json()["details"]["note"]
    assert "dependencies" in note.lower()


def test_setup_mode_explains_that_henchmen_must_start_first(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    body = harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"], "repo": "acme/webapp"}).json()
    assert body["ok"] is False
    assert "start" in body["problems"][0]["action"].lower()


def test_sample_task_goes_to_the_default_repo(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    body = harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]}).json()
    assert body == {
        "ok": True,
        "step": "first_task",
        "details": {"task_id": TASK_ID, "repo": "acme/webapp", "title": SAMPLE_TASKS[0]["title"]},
    }
    (request,) = gateway.submitted
    assert request.title == SAMPLE_TASKS[0]["title"]
    assert request.description == SAMPLE_TASKS[0]["description"]
    assert request.repo == "acme/webapp"
    assert request.created_by == "console"
    assert harness.setup_store.load().server_choices["first_task_id"] == TASK_ID


def test_creating_a_task_does_not_complete_the_step(harness: ConsoleHarness) -> None:
    """Ruling PB-1: only the progress route, on a successful run, completes this step."""
    harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]})
    assert SetupStep.FIRST_TASK not in harness.setup_store.load().completed_steps


def test_own_task_and_repo(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    body = harness.post(
        BASE, {"title": "Fix the typo in the footer", "description": "It says Copywrite", "repo": "acme/site"}
    )
    assert body.json()["ok"] is True
    assert gateway.submitted[0].repo == "acme/site"


def test_needs_a_description(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    body = harness.post(BASE, {}).json()
    assert body["problems"][0]["field"] == "title"
    assert gateway.submitted == []


def test_unknown_sample(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    assert harness.post(BASE, {"sample_id": "nope"}).json()["problems"][0]["field"] == "sample_id"
    assert gateway.submitted == []


def test_needs_a_repository(tmp_path: Path, gateway: FakeGateway) -> None:
    harness = make_harness(tmp_path, mode=ConsoleMode.RUN)
    harness.app.state.task_gateway = gateway
    body = harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]}).json()
    assert body["problems"][0]["field"] == "repo"


@pytest.mark.parametrize("field", ["title", "description"])
@pytest.mark.parametrize("bad", ["bad\x00value", "bad" + chr(0x2028) + "value"], ids=["nul", "line-separator"])
def test_control_characters_are_refused(harness: ConsoleHarness, gateway: FakeGateway, field: str, bad: str) -> None:
    """Nothing that could split a log line (or a dotenv assignment) gets through."""
    body = {"title": "A title", "description": "d", "repo": "acme/site", field: bad}
    assert harness.post(BASE, body).status_code == 422
    assert gateway.submitted == []


def test_a_multi_line_description_is_accepted(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    """Only *control* characters are refused -- a description is written by a person."""
    body = {"title": "A title", "description": "line one\nline two\tindented", "repo": "acme/site"}
    assert harness.post(BASE, body).json()["ok"] is True
    assert gateway.submitted[0].description.startswith("line one")


def test_submission_failure_is_explained(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    gateway.broken = True
    body = harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]}).json()
    assert body["ok"] is False
    assert "broker unavailable" not in body["problems"][0]["message"]
    assert "hunter2" not in harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]}).text


def test_a_failed_submission_is_logged_without_the_credentials_in_it(
    harness: ConsoleHarness, gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    gateway.broken = True
    with caplog.at_level("WARNING", logger="henchmen.console.steps.first_task"):
        harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]})
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert logged
    assert "hunter2" not in logged


def test_progress_while_queued(harness: ConsoleHarness) -> None:
    details = harness.get(f"{BASE}/tasks/{TASK_ID}").json()["details"]
    assert details["outcome"] == "running"
    assert details["phases"][0] == {"id": "queued", "label": "Getting started", "state": "active"}
    assert details["status_message"]


def test_a_progress_read_that_fails_is_a_clear_problem(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    gateway.read_broken = True
    body = harness.get(f"{BASE}/tasks/{TASK_ID}").json()
    assert body["ok"] is False
    assert "document store" not in body["problems"][0]["message"]
    assert body["problems"][0]["action"]


def test_progress_in_setup_mode_explains_that_henchmen_must_start_first(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    body = harness.get(f"{BASE}/tasks/{TASK_ID}").json()
    assert body["ok"] is False
    assert "start" in body["problems"][0]["action"].lower()


def test_success_completes_the_step_for_the_consoles_task(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]})
    gateway.executions[TASK_ID] = {"final_status": "pr_created", "pr_url": "https://github.com/acme/webapp/pull/9"}
    details = harness.get(f"{BASE}/tasks/{TASK_ID}").json()["details"]
    assert details["outcome"] == "succeeded"
    assert details["pr_url"] == "https://github.com/acme/webapp/pull/9"
    assert SetupStep.FIRST_TASK in harness.setup_store.load().completed_steps


def test_success_of_another_task_does_not_complete_the_step(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    gateway.executions[TASK_ID] = {"final_status": "pr_created", "pr_url": "https://github.com/acme/webapp/pull/9"}
    harness.get(f"{BASE}/tasks/{TASK_ID}")
    assert SetupStep.FIRST_TASK not in harness.setup_store.load().completed_steps


def test_failure_does_not_complete_the_step(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]})
    gateway.executions[TASK_ID] = {"final_status": "escalated", "escalation_reason": "lint failed"}
    details = harness.get(f"{BASE}/tasks/{TASK_ID}").json()["details"]
    assert details["outcome"] == "failed"
    assert details["failure_detail"] == "lint failed"
    assert SetupStep.FIRST_TASK not in harness.setup_store.load().completed_steps


def test_a_timed_out_task_does_not_complete_the_step(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]})
    gateway.executions[TASK_ID] = {"final_status": "timed_out"}
    details = harness.get(f"{BASE}/tasks/{TASK_ID}").json()["details"]
    assert details["outcome"] == "failed"
    assert SetupStep.FIRST_TASK not in harness.setup_store.load().completed_steps


def test_finishing_without_a_pull_request_does_not_complete_the_step(
    harness: ConsoleHarness, gateway: FakeGateway
) -> None:
    """Spec §1: the owner is promised a pull request, so a run that opened none leaves the step open."""
    harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]})
    gateway.executions[TASK_ID] = {"final_status": "completed", "pr_url": None}
    details = harness.get(f"{BASE}/tasks/{TASK_ID}").json()["details"]
    assert details["outcome"] == "finished_without_pr"
    assert details["failure_message"]
    assert SetupStep.FIRST_TASK not in harness.setup_store.load().completed_steps


def test_completing_with_a_pull_request_completes_the_step(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]})
    gateway.executions[TASK_ID] = {"final_status": "completed", "pr_url": "https://github.com/acme/webapp/pull/4"}
    assert harness.get(f"{BASE}/tasks/{TASK_ID}").json()["details"]["outcome"] == "succeeded"
    assert SetupStep.FIRST_TASK in harness.setup_store.load().completed_steps


def test_a_task_nothing_picks_up_stops_being_reported_as_queued(harness: ConsoleHarness) -> None:
    harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]})
    _age_the_submission(harness, seconds=QUEUE_TIMEOUT_SECONDS + 5)
    body = harness.get(f"{BASE}/tasks/{TASK_ID}").json()
    assert body["ok"] is False
    assert "picked" in body["problems"][0]["message"]
    assert body["problems"][0]["action"]


def test_a_recently_submitted_task_is_still_queued(harness: ConsoleHarness) -> None:
    harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]})
    _age_the_submission(harness, seconds=QUEUE_TIMEOUT_SECONDS - 5)
    assert harness.get(f"{BASE}/tasks/{TASK_ID}").json()["details"]["outcome"] == "running"


def test_task_id_must_be_a_uuid(harness: ConsoleHarness) -> None:
    assert harness.get(f"{BASE}/tasks/not-a-uuid").status_code == 422


# ---------------------------------------------------------------------------
# The task id is never lost
# ---------------------------------------------------------------------------


def test_an_unwritable_setup_store_refuses_before_creating_a_task(
    harness: ConsoleHarness, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        harness.setup_store, "set_server_choices", _raising(OSError("read-only file system")), raising=True
    )
    body = harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]}).json()
    assert body["ok"] is False
    assert gateway.submitted == []
    assert "read-only" not in body["problems"][0]["message"]


def test_a_task_that_cannot_be_recorded_still_reports_its_id(
    harness: ConsoleHarness, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe passed, the task is published: hand back the id rather than a bare 500."""
    real = harness.setup_store.set_server_choices

    def only_the_probe_works(values: Mapping[str, str]) -> Any:
        if not values:
            return real(values)
        raise OSError("disk full")

    monkeypatch.setattr(harness.setup_store, "set_server_choices", only_the_probe_works, raising=True)
    response = harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert TASK_ID in body["problems"][0]["message"]
    assert len(gateway.submitted) == 1
    assert "disk full" not in response.text


# ---------------------------------------------------------------------------
# C2/C2a: warn before starting a task the cost gate would refuse
# ---------------------------------------------------------------------------


def test_warns_before_starting_when_the_estimate_exceeds_the_limit(
    harness: ConsoleHarness, gateway: FakeGateway
) -> None:
    _save_provider(harness, ceiling="0.5")
    body = harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]}).json()
    assert body["ok"] is False
    problem = body["problems"][0]
    assert problem["field"] == "task_cost_ceiling_usd"
    assert "$0.50" in problem["message"]
    # Ruling F7: both choices spelled out, including what "Start anyway" costs the user.
    assert "Start anyway" in problem["action"]
    assert "Raise the spending limit" in problem["action"]
    assert "partway" in problem["action"]
    assert gateway.submitted == []


def test_confirmed_start_submits_anyway(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    _save_provider(harness, ceiling="0.5")
    body = harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"], "confirm_over_limit": True}).json()
    assert body["ok"] is True
    assert len(gateway.submitted) == 1


def test_a_limit_above_the_estimate_starts_without_confirmation(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    _save_provider(harness, ceiling="1000")
    assert harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]}).json()["ok"] is True
    assert len(gateway.submitted) == 1


def test_a_config_with_no_saved_limit_is_measured_against_the_settings_default(
    harness: ConsoleHarness, gateway: FakeGateway
) -> None:
    """A `henchmen init` config saves no ceiling; the executor still enforces the Settings default."""
    values = {"HENCHMEN_LLM_PROVIDER": "anthropic"}
    for field_name in TIER_FIELDS["anthropic"].values():
        values[f"HENCHMEN_{field_name.upper()}"] = str(Settings.model_fields[field_name].default)
    harness.config_store.update(values, section="LLM")
    default = float(Settings.model_fields["operative_task_cost_ceiling_usd"].default)
    priced = current_estimate(harness.config_store)
    assert priced is not None
    estimate, ceiling = priced
    assert ceiling == default
    assert estimate > default, "the default Anthropic tiers must cost more than the default ceiling"

    body = harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]}).json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "task_cost_ceiling_usd"
    assert gateway.submitted == []


def test_the_warning_quotes_the_ai_steps_own_estimate(harness: ConsoleHarness) -> None:
    """One estimate, shared with the AI provider step -- never a second cost model."""
    _save_provider(harness, ceiling="0.5")
    priced = current_estimate(harness.config_store)
    assert priced is not None
    estimate, ceiling = priced
    assert ceiling == 0.5
    assert estimate > ceiling
    message = harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]}).json()["problems"][0]["message"]
    assert f"${math.ceil(estimate * 100) / 100:.2f}" in message


def test_without_a_saved_provider_the_task_still_starts(harness: ConsoleHarness, gateway: FakeGateway) -> None:
    """Nothing to price against: the executor's own cost gate stays the enforcement point."""
    assert harness.post(BASE, {"sample_id": SAMPLE_TASKS[0]["id"]}).json()["ok"] is True
    assert len(gateway.submitted) == 1
