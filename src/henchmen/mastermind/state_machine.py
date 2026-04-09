"""Task state machine - manages task lifecycle with crash recovery."""

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskState(StrEnum):
    RECEIVED = "received"
    SCHEME_SELECTED = "scheme_selected"
    LAIR_PROVISIONED = "lair_provisioned"
    DOSSIER_BUILT = "dossier_built"
    EXECUTING = "executing"
    CI_RUNNING = "ci_running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    CI_RETRY = "ci_retry"
    ESCALATED = "escalated"


class StateTransition(BaseModel):
    from_state: TaskState
    to_state: TaskState
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


# Valid transitions map: from_state -> list of allowed to_states
VALID_TRANSITIONS: dict[TaskState, list[TaskState]] = {
    TaskState.RECEIVED: [TaskState.SCHEME_SELECTED, TaskState.ESCALATED],
    TaskState.SCHEME_SELECTED: [TaskState.LAIR_PROVISIONED, TaskState.ESCALATED],
    TaskState.LAIR_PROVISIONED: [TaskState.DOSSIER_BUILT, TaskState.ESCALATED],
    TaskState.DOSSIER_BUILT: [TaskState.EXECUTING, TaskState.ESCALATED],
    TaskState.EXECUTING: [TaskState.CI_RUNNING, TaskState.AWAITING_REVIEW, TaskState.ESCALATED],
    TaskState.CI_RUNNING: [TaskState.AWAITING_REVIEW, TaskState.CI_RETRY, TaskState.ESCALATED],
    TaskState.CI_RETRY: [TaskState.CI_RUNNING, TaskState.ESCALATED],
    TaskState.AWAITING_REVIEW: [TaskState.COMPLETED, TaskState.ESCALATED],
}


class TaskStateMachine:
    """Manages task lifecycle state with crash recovery."""

    def __init__(self, task_id: str, initial_state: TaskState = TaskState.RECEIVED):
        self.task_id = task_id
        self.current_state = initial_state
        self.history: list[StateTransition] = []
        self._acceptance_checks: dict[TaskState, Callable[[dict[str, Any]], bool]] = {}

    def transition(self, to_state: TaskState, metadata: dict[str, Any] | None = None) -> StateTransition:
        """Attempt state transition. Raises ValueError if invalid."""
        if not self.can_transition(to_state):
            raise ValueError(
                f"Invalid transition from {self.current_state.value} to {to_state.value} for task {self.task_id}"
            )

        transition = StateTransition(
            from_state=self.current_state,
            to_state=to_state,
            metadata=metadata or {},
        )
        self.current_state = to_state
        self.history.append(transition)
        return transition

    def register_acceptance_check(self, state: TaskState, check: Callable[[dict[str, Any]], bool]) -> None:
        """Register a function that verifies a state was properly reached."""
        self._acceptance_checks[state] = check

    def run_acceptance_check(self, state: TaskState, context: dict[str, Any]) -> bool:
        """Run the acceptance check for a state. Returns True if no check registered."""
        check = self._acceptance_checks.get(state)
        if check is None:
            return True
        return check(context)

    def can_transition(self, to_state: TaskState) -> bool:
        """Check if a transition to the given state is valid from current state."""
        allowed = VALID_TRANSITIONS.get(self.current_state, [])
        return to_state in allowed

    def get_recovery_state(self) -> TaskState:
        """On crash recovery, determine the last state that passed acceptance check.

        Walks history backwards and finds the last state with a passing acceptance
        check. Falls back to the initial RECEIVED state if no state passes.
        """
        # Walk history backwards, find last state with passing acceptance check
        for transition in reversed(self.history):
            state = transition.to_state
            if self.run_acceptance_check(state, transition.metadata):
                return state
        return TaskState.RECEIVED

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence (Firestore/session state)."""
        return {
            "task_id": self.task_id,
            "current_state": self.current_state.value,
            "history": [
                {
                    "from_state": t.from_state.value,
                    "to_state": t.to_state.value,
                    "timestamp": t.timestamp.isoformat(),
                    "metadata": t.metadata,
                }
                for t in self.history
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskStateMachine":
        """Deserialize from persistence."""
        sm = cls(
            task_id=data["task_id"],
            initial_state=TaskState(data["current_state"]),
        )
        sm.history = [
            StateTransition(
                from_state=TaskState(h["from_state"]),
                to_state=TaskState(h["to_state"]),
                timestamp=datetime.fromisoformat(h["timestamp"]),
                metadata=h.get("metadata", {}),
            )
            for h in data.get("history", [])
        ]
        return sm
