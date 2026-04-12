"""Detect when an operative agent is stuck and generate corrective nudge messages.

Replaces the ad-hoc nudge logic previously scattered across agent_builder.py
with a centralized detector that tracks behavioral patterns and produces
actionable messages tailored to the specific stuck state.
"""

from __future__ import annotations

import logging
from enum import StrEnum

logger = logging.getLogger(__name__)


class StuckState(StrEnum):
    """Detected patterns indicating the agent is stuck."""

    REPEATED_EDIT = "repeated_edit"
    SEARCH_LOOP = "search_loop"
    HIGH_BUDGET_NO_COMMIT = "high_budget_no_commit"
    REPEATED_TOOL_ERROR = "repeated_tool_error"
    TEXT_ONLY_LOOP = "text_only_loop"
    READ_ONLY_LOOP = "read_only_loop"


class NudgeDetector:
    """Track tool call history and detect when the agent is stuck."""

    def __init__(self, max_steps: int) -> None:
        self.max_steps = max_steps
        self._tool_history: list[str] = []
        self._edit_count: int = 0
        self._search_count: int = 0
        self._commit_count: int = 0
        self._consecutive_text_only: int = 0
        self._read_only_steps: int = 0
        self._has_edited: bool = False
        self._consecutive_errors: int = 0
        self._last_error_tool: str = ""

    def record_tool_call(self, tool_name: str, success: bool = True) -> None:
        """Record a tool call for pattern detection."""
        self._tool_history.append(tool_name)

        if tool_name in ("file_edit", "file_write"):
            self._edit_count += 1
            self._has_edited = True
            self._read_only_steps = 0
        elif tool_name == "git_commit":
            self._commit_count += 1
        elif tool_name in ("grep_search", "file_read", "code_search", "find_file"):
            self._search_count += 1
            self._read_only_steps += 1
        else:
            self._read_only_steps = 0

        if not success:
            if tool_name == self._last_error_tool:
                self._consecutive_errors += 1
            else:
                self._consecutive_errors = 1
                self._last_error_tool = tool_name
        else:
            self._consecutive_errors = 0
            self._last_error_tool = ""

        # Reset text-only counter when tools are called
        self._consecutive_text_only = 0

    def record_text_only_response(self) -> None:
        """Record that the model returned text without tool calls."""
        self._consecutive_text_only += 1

    def check_stuck(self, current_step: int) -> StuckState | None:
        """Check if the agent appears stuck. Returns the stuck state or None."""
        # Check repeated tool errors
        if self._consecutive_errors >= 3:
            return StuckState.REPEATED_TOOL_ERROR

        # Check text-only loop (model keeps talking instead of using tools)
        if self._has_edited and self._consecutive_text_only >= 3:
            return StuckState.TEXT_ONLY_LOOP

        if not self._has_edited and self._consecutive_text_only >= 2 and current_step >= 5:
            return StuckState.TEXT_ONLY_LOOP

        # Check search loop (repeated searches without progress) — before read_only_loop
        # because search_loop is more specific
        recent = self._tool_history[-6:] if len(self._tool_history) >= 6 else []
        search_tools = {"grep_search", "file_read", "code_search", "find_file"}
        if len(recent) >= 6 and all(t in search_tools for t in recent):
            return StuckState.SEARCH_LOOP

        # Check read-only loop (only reading files, not editing)
        nudge_threshold = max(3, self.max_steps // 8)
        if not self._has_edited and self._read_only_steps >= nudge_threshold and current_step >= nudge_threshold:
            return StuckState.READ_ONLY_LOOP

        # Check high budget usage without commit
        budget_fraction = current_step / self.max_steps if self.max_steps > 0 else 0
        if budget_fraction >= 0.7 and self._has_edited and self._commit_count == 0:
            return StuckState.HIGH_BUDGET_NO_COMMIT

        # Check repeated edits to the same area
        recent_edits = [t for t in self._tool_history[-8:] if t in ("file_edit", "file_write")]
        if len(recent_edits) >= 5:
            return StuckState.REPEATED_EDIT

        return None

    def get_nudge_message(self, state: StuckState, current_step: int) -> str:
        """Return an actionable nudge message for the detected stuck state."""
        remaining = self.max_steps - current_step

        messages: dict[StuckState, str] = {
            StuckState.REPEATED_EDIT: (
                f"You have edited files {self._edit_count} times recently without committing. "
                f"You have {remaining} steps left. "
                f"STOP editing. Review your changes, then call git_commit NOW."
            ),
            StuckState.SEARCH_LOOP: (
                f"You have been searching/reading files for the last 6+ tool calls without making changes. "
                f"You have {remaining} steps left. "
                f"Based on what you've learned, make the code change NOW with file_edit, then git_commit."
            ),
            StuckState.HIGH_BUDGET_NO_COMMIT: (
                f"WARNING: You have used {current_step} of {self.max_steps} steps "
                f"and edited files but have NOT committed. "
                f"You have {remaining} steps left. "
                f"Call git_commit(message, files) IMMEDIATELY."
            ),
            StuckState.REPEATED_TOOL_ERROR: (
                f"The same tool has failed {self._consecutive_errors} times in a row. "
                f"STOP retrying the same approach. Try a completely different strategy: "
                f"different file, different search query, or different edit approach."
            ),
            StuckState.TEXT_ONLY_LOOP: (
                f"You have returned text without calling any tool "
                f"{self._consecutive_text_only} times. "
                + (
                    "Call git_commit(message, files) NOW. Do NOT return text."
                    if self._has_edited
                    else f"You have {remaining} steps left. "
                    f"Use file_edit or file_write to make the change, then git_commit. "
                    f"Do NOT return text — call a tool."
                )
            ),
            StuckState.READ_ONLY_LOOP: (
                f"You have spent {current_step} steps reading files. "
                f"You have {remaining} steps remaining. "
                f"STOP READING. Based on what you've learned, make the code change NOW "
                f"using file_edit or file_write, then call git_commit."
            ),
        }

        return messages.get(state, f"You have {remaining} steps remaining. Make progress now.")
