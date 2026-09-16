"""How the Console hands a task to the running services and follows it (spec §4, step 5).

Only run mode has services: ``build_serve_app`` injects a
:class:`ServiceTaskGateway` over the process's shared message broker and
document store. In setup mode there is no gateway and the first-task step says
Henchmen must be started first (decision A6).

The gateway reuses Dispatch's own CLI path (``handle_cli_request``: normalise,
publish to the task-intake topic) in-process, authorised by the Console session
rather than a bearer token -- decision A10 allows this for a desktop install,
where the Console and Dispatch are the same process -- so a Console task is
indistinguishable from a ``henchmen chat`` task. It builds its own
``TaskNormalizer`` (the normaliser is stateless) and lets the normaliser apply
Dispatch's own default-repository fallback rather than duplicating it here
(note M-16).

Progress comes from the tracker's ``task_executions`` document, read through
:meth:`~henchmen.observability.tracker.TaskTracker.get_task` -- the one reader
every other consumer of task state uses (ruling C1), never a second lookup path
of its own -- and is mapped onto five plain-language phases. Scheme checkpoints
are written after each node finishes, so the active phase is the phase of the
latest checkpointed node.

Nothing a timeline carries is a secret: the phase list, the pull-request URL
and a length-capped, redacted escalation reason. Raw operative output never
reaches it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, Field

from henchmen.observability.tracker import SUCCESS_STATUSES, TaskTracker
from henchmen.utils.redaction import redact

if TYPE_CHECKING:
    from henchmen.config.settings import Settings
    from henchmen.dispatch.api_models import CreateTaskRequest
    from henchmen.providers.interfaces.document_store import DocumentStore
    from henchmen.providers.interfaces.message_broker import MessageBroker

PhaseState = Literal["done", "active", "pending", "failed"]
Outcome = Literal["running", "succeeded", "failed"]

PHASES: tuple[tuple[str, str], ...] = (
    ("queued", "Getting started"),
    ("reading_code", "Reading your code"),
    ("writing_code", "Writing the change"),
    ("testing", "Testing the change"),
    ("opening_pr", "Opening the pull request"),
)
_PHASE_IDS: tuple[str, ...] = tuple(phase_id for phase_id, _ in PHASES)

NODE_PHASES: dict[str, str] = {
    "create_branch": "reading_code",
    "prefetch_context": "reading_code",
    "analyze_goal": "reading_code",
    "implement_fix": "writing_code",
    "implement_feature": "writing_code",
    "report_plan": "writing_code",
    "verify_changes": "testing",
    "run_lint": "testing",
    "fix_lint": "testing",
    "run_lint_retry": "testing",
    "run_tests": "testing",
    "fix_tests": "testing",
    "run_tests_retry": "testing",
    "create_pr": "opening_pr",
}

FAILURE_MESSAGE = "Henchmen stopped before finishing and needs a person to take a look."
QUEUED_MESSAGE = "Henchmen hasn't picked this task up yet."
RUNNING_MESSAGE = "Henchmen is working on this task."
SUCCESS_MESSAGE = "Henchmen finished this task."

#: The escalation reason is written by the scheme executor and can quote a tool's
#: output; it is redacted and cut to this many characters before a browser sees it.
MAX_FAILURE_DETAIL = 500


class TaskGateway(Protocol):
    """What the first-task step needs from the running services."""

    async def submit(self, request: CreateTaskRequest) -> str:
        """Create the task; return its id."""
        ...

    async def execution(self, task_id: str) -> dict[str, Any] | None:
        """The tracker's execution document, or ``None`` before Mastermind picks the task up."""
        ...


class ServiceTaskGateway:
    """:class:`TaskGateway` over the in-process broker and document store."""

    def __init__(self, *, settings: Settings, broker: MessageBroker, store: DocumentStore) -> None:
        self._settings = settings
        self._broker = broker
        # The tracker is the single reader of `task_executions` (ruling C1); the store
        # handed in here is the one `build_serve_app` gives every service, so this
        # reads exactly the documents Mastermind writes.
        self._tracker = TaskTracker(settings, store)

    async def submit(self, request: CreateTaskRequest) -> str:
        # Imported lazily: setup mode builds no Dispatch app, and importing this
        # module must not drag the service in.
        from henchmen.dispatch.handlers.cli import handle_cli_request
        from henchmen.dispatch.normalizer import TaskNormalizer

        result = await handle_cli_request(request, TaskNormalizer(), self._settings, broker=self._broker)
        return str(result["task_id"])

    async def execution(self, task_id: str) -> dict[str, Any] | None:
        return await self._tracker.get_task(task_id)


class TimelinePhase(BaseModel):
    """One step of the first-task timeline."""

    id: str = Field(..., description="Phase id")
    label: str = Field(..., description="Plain-language label")
    state: PhaseState = Field(..., description="done, active, pending or failed")


class TaskTimeline(BaseModel):
    """What the Console shows while the first task runs."""

    task_id: str = Field(..., description="Task id")
    outcome: Outcome = Field(..., description="running, succeeded or failed")
    status_message: str = Field(..., description="Plain-language sentence for the current outcome")
    phases: list[TimelinePhase] = Field(..., description="Phases in order")
    pr_url: str | None = Field(default=None, description="Pull request, once opened")
    failure_message: str | None = Field(default=None, description="Plain-language failure")
    failure_detail: str | None = Field(default=None, description="Technical detail behind Show details")


def _phase_index(node_id: object) -> int | None:
    phase = NODE_PHASES.get(str(node_id or ""))
    return None if phase is None else _PHASE_IDS.index(phase)


def _phases(active: int, *, failed: bool = False, all_done: bool = False) -> list[TimelinePhase]:
    phases: list[TimelinePhase] = []
    for index, (phase_id, label) in enumerate(PHASES):
        state: PhaseState
        if all_done or index < active:
            state = "done"
        elif index == active:
            state = "failed" if failed else "active"
        else:
            state = "pending"
        phases.append(TimelinePhase(id=phase_id, label=label, state=state))
    return phases


def _detail(execution: Mapping[str, Any], final_status: str) -> str:
    """The technical reason behind a failure -- redacted and capped, never raw output."""
    detail = str(execution.get("escalation_reason") or "").strip() or final_status
    detail = redact(detail)
    return detail if len(detail) <= MAX_FAILURE_DETAIL else f"{detail[:MAX_FAILURE_DETAIL]}..."


def build_timeline(task_id: str, execution: Mapping[str, Any] | None) -> TaskTimeline:
    """Map a ``task_executions`` document (or its absence) onto the timeline.

    A document that is not there yet is the queued state: Mastermind writes one
    as soon as it picks the task up off the task-intake topic, so between the
    Console's ``submit`` and that moment there is nothing to read.

    Any terminal status that is not in
    :data:`~henchmen.observability.tracker.SUCCESS_STATUSES` is a failure --
    ``timed_out`` included, which is never reported as success.
    """
    if execution is None:
        return TaskTimeline(task_id=task_id, outcome="running", status_message=QUEUED_MESSAGE, phases=_phases(0))

    final_status = str(execution.get("final_status") or "").strip().lower()
    nodes = [*list(execution.get("nodes_executed") or []), execution.get("current_node_id")]
    reached = [index for index in (_phase_index(node) for node in nodes) if index is not None]
    current = max(reached, default=_PHASE_IDS.index("reading_code"))

    if final_status in SUCCESS_STATUSES:
        return TaskTimeline(
            task_id=task_id,
            outcome="succeeded",
            status_message=SUCCESS_MESSAGE,
            phases=_phases(len(PHASES), all_done=True),
            pr_url=str(execution.get("pr_url") or "") or None,
        )
    if final_status:
        escalated_at = _phase_index(execution.get("escalation_node"))
        return TaskTimeline(
            task_id=task_id,
            outcome="failed",
            status_message=FAILURE_MESSAGE,
            phases=_phases(escalated_at if escalated_at is not None else current, failed=True),
            failure_message=FAILURE_MESSAGE,
            failure_detail=_detail(execution, final_status),
        )
    return TaskTimeline(task_id=task_id, outcome="running", status_message=RUNNING_MESSAGE, phases=_phases(current))
