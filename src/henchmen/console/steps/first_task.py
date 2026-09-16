"""Console step 5 (optional): run a first task and watch it become a pull request (spec §4).

The user picks a sample or describes a small change; the step submits it
through the run-mode :class:`~henchmen.console.task_gateway.TaskGateway` and
the UI polls ``/tasks/{task_id}`` for the timeline. Each poll is one bounded
read of the task's execution document -- the server never loops waiting for a
task to finish -- and the queued state is itself bounded: a task nothing has
picked up ``QUEUE_TIMEOUT_SECONDS`` after the recorded submit time is reported
as a problem, not as a spinner.

The step completes the first time the task *the Console created* succeeds
(ruling PB-1: :func:`task_progress` is the only route here that completes it,
and only for the id recorded in ``server_choices``). Succeeding means a pull
request exists -- a run that finished without opening one is its own outcome
and leaves the step open, since spec §1's promise is that the owner sees the
first pull request.

The task id is what makes a run followable and what lets it complete the step,
so it is never lost: the setup store is probed before anything is submitted,
and if recording still fails afterwards the response carries the id of the task
that is now running rather than a bare 500.

In setup mode the services are not running, so submitting explains that
Henchmen must start first rather than hanging on a broker nothing is draining
(decision A6); 2C calls apply and waits for the restart before showing this
step.

Before submitting, the step prices the task the way the AI provider step does
and refuses -- with a plain-language problem naming the spending limit --
when the estimate is above the saved per-task limit and the user has not said
"Start anyway" (ruling C2/C2a). Otherwise the executor's own cost gate would
refuse the very first task after it had already started, with a message
nobody asked for.

Nothing the user types is ever interpolated into anything executable: a title
and description are length-capped, rejected outright if they carry control
characters, and travel only as fields of a
:class:`~henchmen.dispatch.api_models.CreateTaskRequest`. Log lines about a
task carry a short, single-line, redacted excerpt of the title and never the
description. Progress responses carry no credentials: only the phase list, the
pull-request URL and a capped, redacted escalation reason.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field, field_validator

from henchmen.console.config_store import ConfigStore
from henchmen.console.deps import get_config_store, get_task_gateway
from henchmen.console.state import SetupStateStore, SetupStep
from henchmen.console.steps import (
    StepFailure,
    StepProblem,
    StepSuccess,
    get_setup_store,
    step_failed,
    step_succeeded,
)
from henchmen.console.steps.ai_provider import current_estimate
from henchmen.console.task_gateway import QUEUE_TIMEOUT_SECONDS, TaskGateway, build_timeline, parse_submitted_at
from henchmen.dispatch.api_models import CreateTaskRequest
from henchmen.utils.redaction import redact

logger = logging.getLogger(__name__)
router = APIRouter()
STEP = SetupStep.FIRST_TASK
DEFAULT_REPO_KEY = "HENCHMEN_GITHUB_DEFAULT_REPO"
FIRST_TASK_CHOICE = "first_task_id"
FIRST_TASK_SUBMITTED_AT_CHOICE = "first_task_submitted_at"
_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# U+2028 LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR, spelled with `chr` so the
# characters themselves never sit invisibly in this file.
_UNICODE_LINE_SEPARATORS = chr(0x2028) + chr(0x2029)

# C0 controls and those two separators, none of which belong in a task title or
# description: they are what turns one logged line into two, or one dotenv
# assignment into two if such a value ever reached a writer.
_CONTROL_CHARACTERS = re.compile(rf"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f{_UNICODE_LINE_SEPARATORS}]")
_TITLE_CONTROL_CHARACTERS = re.compile(rf"[\x00-\x1f\x7f{_UNICODE_LINE_SEPARATORS}]")
_LOG_EXCERPT = 80

# Ruling PI-13 (decision C19): neither the local nor the cloud test gate installs a
# Python project's dependencies yet, so a first task whose tests need them fails the
# test gate. The samples below are documentation-only changes for exactly that reason,
# and the note says so rather than leaving an owner to discover it from a red run.
DEPENDENCY_NOTE = (
    "Henchmen's test step doesn't install a project's dependencies yet, so pick a first "
    "task that doesn't need them -- a documentation or README change is ideal."
)

SAMPLE_TASKS: tuple[dict[str, str], ...] = (
    {
        "id": "readme-quick-start",
        "title": "Add a Quick start section to the README",
        "description": (
            "Add a short 'Quick start' section near the top of README.md that explains how to install and run "
            "the project, based on the existing documentation and build files. Change only README.md."
        ),
    },
    {
        "id": "fix-doc-typos",
        "title": "Fix spelling mistakes in the documentation",
        "description": (
            "Find and fix spelling and grammar mistakes in the Markdown documentation. "
            "Do not change code, links or meaning."
        ),
    },
    {
        "id": "contributing-guide",
        "title": "Add a CONTRIBUTING.md",
        "description": (
            "Add a CONTRIBUTING.md that explains how to set up the project locally, run its tests and open a "
            "pull request, based on the existing build and test configuration."
        ),
    },
)
_SAMPLES_BY_ID = {sample["id"]: sample for sample in SAMPLE_TASKS}

ConfigDep = Annotated[ConfigStore, Depends(get_config_store)]
SetupDep = Annotated[SetupStateStore, Depends(get_setup_store)]
GatewayDep = Annotated[TaskGateway | None, Depends(get_task_gateway)]


class FirstTaskRequest(BaseModel):
    """A sample id, or a title and description; repo defaults to the GitHub step's choice."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sample_id: str = Field(default="", max_length=64, description="One of SAMPLE_TASKS")
    title: str = Field(default="", max_length=200, description="What to change, in a few words")
    description: str = Field(default="", max_length=8000, description="More detail")
    repo: str = Field(
        default="", max_length=200, pattern=r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?$", description="owner/name"
    )
    confirm_over_limit: bool = Field(
        default=False, description="Start even when the estimated cost is above the per-task spending limit"
    )

    @field_validator("title", mode="after")
    @classmethod
    def _plain_title(cls, value: str) -> str:
        if _TITLE_CONTROL_CHARACTERS.search(value):
            raise ValueError("the title cannot contain line breaks or control characters")
        return value

    @field_validator("description", mode="after")
    @classmethod
    def _plain_description(cls, value: str) -> str:
        # Line breaks and tabs are how anyone writes a description; the rest are not.
        if _CONTROL_CHARACTERS.search(value):
            raise ValueError("the description cannot contain control characters")
        return value


def _for_log(text: str) -> str:
    """A short, single-line, redacted excerpt of user text, safe to put in a log line."""
    collapsed = " ".join(redact(text).split())
    return collapsed if len(collapsed) <= _LOG_EXCERPT else f"{collapsed[:_LOG_EXCERPT]}..."


def _not_running() -> StepProblem:
    return StepProblem(
        message="Henchmen isn't running yet, so it can't take a task.",
        action="Choose Finish setup to start Henchmen, then come back to this step.",
    )


def _not_picked_up() -> StepProblem:
    minutes = QUEUE_TIMEOUT_SECONDS // 60
    return StepProblem(
        message=(f"Henchmen hasn't picked this task up after {minutes} minutes, so nothing is working on it."),
        action="Check that Henchmen's services are running on the dashboard, then start the task again.",
    )


def _cannot_record() -> StepProblem:
    return StepProblem(
        message="Henchmen could not save your setup progress, so it did not start the task.",
        action="Check that the Henchmen data folder is writable and has free space, then try again.",
    )


def _started_but_not_recorded(task_id: str) -> StepProblem:
    """The task is real and running; only the note of *which* task this is could not be written."""
    return StepProblem(
        message=(
            f"Henchmen started the task ({task_id}) but could not save it as your first task, "
            "so this step won't finish on its own."
        ),
        action=(
            "The task is still running -- watch it on the dashboard. Check that the Henchmen data "
            "folder is writable, then start another task to finish this step."
        ),
    )


def _over_limit(estimate: float, ceiling: float) -> StepProblem:
    """Explain a refusal the executor's cost gate would otherwise make mid-task (ruling C2a).

    The estimate is rounded *up* to the cent and the limit *down*, so the two
    figures never print as the same number for a comparison that really did
    come out over -- "$6.44, more than your $6.44 limit" reads like a bug.
    """
    shown_estimate = math.ceil(estimate * 100) / 100
    shown_ceiling = math.floor(ceiling * 100) / 100
    return StepProblem(
        field="task_cost_ceiling_usd",
        message=(
            f"This task may cost up to ${shown_estimate:.2f}, more than your ${shown_ceiling:.2f} limit per task, "
            "so Henchmen would stop before starting it."
        ),
        action=(
            "Raise the spending limit in the AI provider step so the task can finish, or choose Start "
            "anyway -- Henchmen will stop the task partway through when it reaches the limit."
        ),
    )


@router.get("/samples")
async def samples(config: ConfigDep) -> StepSuccess:
    """Suggested first tasks, the default repository and the test-gate caveat. Never completes the step."""
    return StepSuccess(
        step=STEP,
        details={
            "samples": [dict(sample) for sample in SAMPLE_TASKS],
            "default_repo": config.get(DEFAULT_REPO_KEY),
            "note": DEPENDENCY_NOTE,
        },
    )


@router.post("")
async def create_first_task(
    body: FirstTaskRequest, config: ConfigDep, setup: SetupDep, gateway: GatewayDep
) -> StepSuccess | StepFailure:
    """Submit the first task. Never completes the step -- only a successful run does."""
    if gateway is None:
        return step_failed(STEP, _not_running())
    title, description = body.title, body.description
    if body.sample_id:
        sample = _SAMPLES_BY_ID.get(body.sample_id)
        if sample is None:
            problem = StepProblem(
                field="sample_id", message="That sample task does not exist.", action="Pick one from the list."
            )
            return step_failed(STEP, problem)
        title, description = sample["title"], sample["description"]
    if not title:
        problem = StepProblem(
            field="title", message="Describe a small change for Henchmen to make.", action="Or pick a sample task."
        )
        return step_failed(STEP, problem)
    repo = body.repo or config.get(DEFAULT_REPO_KEY)
    if not repo:
        problem = StepProblem(
            field="repo", message="Choose the repository to change.", action="Finish the GitHub step first."
        )
        return step_failed(STEP, problem)

    priced = current_estimate(config)
    if priced is not None and not body.confirm_over_limit:
        estimate, ceiling = priced
        if estimate > ceiling:
            return step_failed(STEP, _over_limit(estimate, ceiling))

    try:
        # A no-op write first: the task id is what makes the run followable and lets it
        # complete the step, so a setup store that cannot be written is found *before*
        # a task exists rather than after, when there is nothing useful left to do.
        setup.set_server_choices({})
    except (OSError, ValueError) as exc:
        logger.warning("Refusing to start a first task: the setup state is not writable (%s)", type(exc).__name__)
        return step_failed(STEP, _cannot_record())

    request = CreateTaskRequest(title=title, description=description, repo=repo, created_by="console")
    try:
        task_id = await gateway.submit(request)
    except Exception as exc:
        # The exception can quote a broker URL or a provider error; it is logged
        # redacted and never shown, since nothing in it helps the person reading it.
        logger.warning("First task %r could not be submitted: %s", _for_log(title), redact(str(exc)))
        problem = StepProblem(
            message="Henchmen could not start the task.",
            action="Choose Try again. If it keeps failing, copy diagnostics from the Henchmen app.",
        )
        return step_failed(STEP, problem)
    logger.info("Console submitted first task %s (%r) for %s", task_id, _for_log(title), repo)
    try:
        # The submit time bounds the queued phase (`QUEUE_TIMEOUT_SECONDS`), so it is
        # written together with the id in one merge -- never one without the other.
        setup.set_server_choices(
            {FIRST_TASK_CHOICE: task_id, FIRST_TASK_SUBMITTED_AT_CHOICE: datetime.now(UTC).isoformat()}
        )
    except (OSError, ValueError) as exc:
        # The probe above passed, so this is a failure that appeared in between. The task
        # is already published and cannot be recalled: say so, and hand back the id it
        # runs under rather than a bare 500 that loses it.
        logger.warning("First task %s started but could not be recorded (%s)", task_id, type(exc).__name__)
        return step_failed(STEP, _started_but_not_recorded(task_id))
    return StepSuccess(step=STEP, details={"task_id": task_id, "repo": repo, "title": title})


@router.get("/tasks/{task_id}")
async def task_progress(
    task_id: Annotated[str, PathParam(pattern=_UUID_PATTERN)], setup: SetupDep, gateway: GatewayDep
) -> StepSuccess | StepFailure:
    """The timeline for ``task_id``; completes the step when the Console's own task succeeds.

    One read per request (ruling: bounded reads). A task Mastermind has not
    picked up yet has no execution document and reads as the queued phase --
    but only for as long as that is plausible: past
    ``QUEUE_TIMEOUT_SECONDS`` from the recorded submit time nothing is
    consuming the task-intake topic, and the answer is a problem rather than a
    spinner that never resolves. A read that fails outright is likewise a
    problem, never a silent "still queued".

    Only ``succeeded`` -- a finished run with a pull request -- completes the
    step; ``finished_without_pr`` does not, because spec §1's promise is that
    the owner sees the first pull request.
    """
    if gateway is None:
        return step_failed(STEP, _not_running())
    try:
        execution = await gateway.execution(task_id)
    except Exception as exc:
        logger.warning("Could not read the progress of task %s: %s", task_id, redact(str(exc)))
        problem = StepProblem(
            message="Henchmen could not check how the task is going.",
            action="Choose Try again. If it keeps failing, copy diagnostics from the Henchmen app.",
        )
        return step_failed(STEP, problem)
    try:
        choices = setup.load().server_choices
    except ValueError as exc:
        # A corrupted setup-state file must read as a plain problem, not a 500 --
        # the task itself is still running regardless of whether this file is intact.
        logger.warning("Could not read setup progress while polling task %s: %s", task_id, redact(str(exc)))
        problem = StepProblem(
            message="Henchmen could not read your setup progress.",
            action="Check that the Henchmen data folder is intact, then try again.",
        )
        return step_failed(STEP, problem)
    is_own_task = choices.get(FIRST_TASK_CHOICE) == task_id
    # The submit time is known only for the task this Console submitted; any other id
    # keeps the unbounded queued reading, since there is nothing to measure against.
    submitted_at = parse_submitted_at(choices.get(FIRST_TASK_SUBMITTED_AT_CHOICE, "")) if is_own_task else None
    timeline = build_timeline(task_id, execution, submitted_at=submitted_at)
    if execution is None and timeline.outcome == "failed":
        # The only way a *missing* document is a failed timeline: it waited out the bound.
        return step_failed(STEP, _not_picked_up())
    details = timeline.model_dump(mode="json")
    if timeline.outcome == "succeeded" and is_own_task:
        # The only route here that completes the step (ruling PB-1), and only for the
        # task this Console created: watching somebody else's finished task is not setup.
        # `record_step_complete` is idempotent, so repeated polls are harmless.
        return step_succeeded(setup, STEP, details)
    return StepSuccess(step=STEP, details=details)
