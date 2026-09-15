"""Persisted progress through the Console's setup guide.

The state file holds only non-secret progress (which steps are done, which
provider was chosen). Credentials go to the data directory's henchmen.env and
secrets/ directory, never here.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError


class SetupStep(StrEnum):
    """Steps of the setup guide, in display order."""

    WELCOME = "welcome"
    AI_PROVIDER = "ai_provider"
    GITHUB = "github"
    SLACK = "slack"
    JIRA = "jira"
    FIRST_TASK = "first_task"


REQUIRED_STEPS: frozenset[SetupStep] = frozenset({SetupStep.AI_PROVIDER, SetupStep.GITHUB})
OPTIONAL_STEPS: frozenset[SetupStep] = frozenset({SetupStep.SLACK, SetupStep.JIRA, SetupStep.FIRST_TASK})


class SetupState(BaseModel):
    """Where the user is in the setup guide."""

    current_step: SetupStep = Field(default=SetupStep.WELCOME, description="Step the guide should show")
    completed_steps: list[SetupStep] = Field(default_factory=list, description="Steps finished successfully")
    skipped_steps: list[SetupStep] = Field(default_factory=list, description="Optional steps the user skipped")
    choices: dict[str, str] = Field(default_factory=dict, description="Non-secret selections, e.g. llm_provider")
    server_choices: dict[str, str] = Field(
        default_factory=dict,
        description="Non-secret values only the server writes (e.g. github_app_slug); never client-writable",
    )
    completed: bool = Field(default=False, description="True once setup was applied and run mode enabled")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), description="Last write time (UTC)")

    def missing_required_steps(self) -> list[SetupStep]:
        """Required steps not yet completed, in guide order."""
        done = set(self.completed_steps)
        return [step for step in SetupStep if step in REQUIRED_STEPS and step not in done]


class SetupStateStore:
    """Load and atomically save :class:`SetupState` as JSON."""

    def __init__(self, path: Path) -> None:
        self.path = path
        # Guards the read-modify-write span of set_server_choices/record_step_complete
        # (each other) against each other within one process; save() itself is already
        # atomic on disk via os.replace.
        self._lock = threading.Lock()

    def load(self) -> SetupState:
        """Return the saved state, or a fresh one when no file exists.

        A present-but-unreadable file raises instead of silently restarting
        setup, which would let a corrupted volume re-run the guide over a
        working configuration.
        """
        if not self.path.is_file():
            return SetupState()
        try:
            return SetupState.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"{self.path.name} is unreadable: {exc}") from exc

    def save(self, state: SetupState) -> SetupState:
        """Write ``state`` atomically and return what was written."""
        stamped = state.model_copy(update={"updated_at": datetime.now(UTC)})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(stamped.model_dump_json(indent=2))
            os.replace(tmp_name, self.path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
            raise
        return stamped

    def mark_completed(self) -> SetupState:
        """Mark setup complete; refuses while a required step is missing."""
        with self._lock:
            state = self.load()
            missing = state.missing_required_steps()
            if missing:
                raise ValueError("Setup is missing required steps: " + ", ".join(step.value for step in missing))
            return self.save(state.model_copy(update={"completed": True}))

    def set_server_choices(self, values: Mapping[str, str]) -> SetupState:
        """Merge server-only values into ``server_choices`` (the Console's state PUT can never write them)."""
        with self._lock:
            state = self.load()
            return self.save(state.model_copy(update={"server_choices": {**state.server_choices, **dict(values)}}))

    def record_step_complete(self, step: SetupStep) -> SetupState:
        """Mark ``step`` complete. Called only by that step's own validation route (never by a client)."""
        with self._lock:
            state = self.load()
            done = {*state.completed_steps, step}
            return self.save(
                state.model_copy(
                    update={
                        "completed_steps": [s for s in SetupStep if s in done],
                        "skipped_steps": [s for s in state.skipped_steps if s != step],
                    }
                )
            )

    def record_step_incomplete(self, step: SetupStep) -> SetupState:
        """Mark ``step`` no longer complete (its saved configuration was invalidated).

        Server-only, like :meth:`record_step_complete` and under the same lock:
        called when a step's route replaces what the completion relied on (e.g. a
        re-created GitHub App). The state PUT can never reach it. A step that is
        not complete is left alone and nothing is written.
        """
        with self._lock:
            state = self.load()
            if step not in state.completed_steps:
                return state
            return self.save(
                state.model_copy(update={"completed_steps": [s for s in state.completed_steps if s != step]})
            )

    def update_client_fields(
        self, *, current_step: SetupStep, skipped_steps: list[SetupStep], choices: dict[str, str]
    ) -> SetupState:
        """Apply the client-writable fields of a setup-state PUT atomically.

        Guarded by the same lock as :meth:`record_step_complete` and
        :meth:`set_server_choices`, so a step completed by one of those calls
        while this one is in flight is never lost to a save built from a
        stale, pre-completion snapshot -- this method's own load and save
        happen back to back with nothing else able to interleave. A step
        already in ``completed_steps`` is dropped from ``skipped_steps``:
        completion always wins over an (now stale) earlier skip.
        """
        with self._lock:
            state = self.load()
            completed = set(state.completed_steps)
            return self.save(
                state.model_copy(
                    update={
                        "current_step": current_step,
                        "skipped_steps": [step for step in skipped_steps if step not in completed],
                        "choices": choices,
                    }
                )
            )
