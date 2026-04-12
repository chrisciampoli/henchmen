"""Operative models - represents a running agent instance and its report."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from henchmen.models._base import StrictBase


class OperativeStatus(StrEnum):
    SPAWNING = "spawning"
    INITIALIZING = "initializing"
    EXECUTING = "executing"
    REPORTING = "reporting"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"  # Agent can't proceed, needs human help
    INTERRUPTED = "interrupted"  # Graceful SIGTERM shutdown — partial work preserved


class OperativeConfig(StrictBase):
    """Configuration used to spawn a Cloud Run operative container."""

    task_id: str = Field(..., description="ID of the parent HenchmenTask")
    node_id: str = Field(..., description="Scheme node this operative is executing")
    scheme_id: str = Field(..., description="Scheme definition this operative belongs to")
    model_name: str = Field(default="gemini-2.5-pro", description="Vertex AI model to use")
    cpu: str = Field(default="4", description="vCPU allocation for the Cloud Run container")
    memory: str = Field(default="8Gi", description="Memory allocation for the Cloud Run container")
    timeout_seconds: int = Field(default=1800, description="Maximum execution time in seconds")

    @property
    def branch_name(self) -> str:
        """Canonical Henchmen branch name for this task.

        Mirrors :attr:`henchmen.models.task.HenchmenTask.branch_name` so the
        operative bootstrap (which only has an ``OperativeConfig``) computes
        the same value as the mastermind side.
        """
        return f"henchmen/{self.task_id[:8]}"


class OperativeReport(StrictBase):
    """Report produced by an operative upon completion or failure."""

    task_id: str = Field(..., description="ID of the parent HenchmenTask")
    scheme_id: str = Field(..., description="Scheme definition this operative belongs to")
    node_id: str = Field(..., description="Scheme node that was executed")
    operative_id: str = Field(..., description="Unique Cloud Run job execution ID")
    status: OperativeStatus = Field(..., description="Terminal status of the operative")
    git_diff: str | None = Field(default=None, description="Unified diff of all code changes made")
    summary: str = Field(..., description="Natural-language summary of work performed")
    confidence_score: float = Field(
        ..., ge=0.0, le=1.0, description="Self-assessed confidence in the result (0.0 - 1.0)"
    )
    files_changed: list[str] = Field(default_factory=list, description="List of file paths modified")
    error: str | None = Field(default=None, description="Error message if the operative failed")
    block_reason: str | None = Field(default=None, description="Reason the operative is blocked (if status=BLOCKED)")
    started_at: datetime = Field(..., description="UTC timestamp when the operative began executing")
    completed_at: datetime | None = Field(default=None, description="UTC timestamp when the operative finished")
    # Telemetry
    model_name: str = Field(default="", description="Model that was used for this operative")
    total_input_tokens: int = Field(default=0, description="Total input tokens consumed")
    total_output_tokens: int = Field(default=0, description="Total output tokens consumed")
    cached_input_tokens: int = Field(default=0, description="Input tokens served from context cache (75% discount)")
    model_calls: int = Field(default=0, description="Number of model API calls")
    tool_calls_count: int = Field(default=0, description="Total tool calls made")
    tool_calls_detail: dict[str, int] = Field(default_factory=dict, description="Tool call counts by tool name")
    wall_clock_seconds: float = Field(default=0.0, description="Total execution time in seconds")
    steps_used: int = Field(default=0, description="Number of agentic steps taken during execution")
    context_tokens_at_start: int = Field(default=0, description="Input tokens on the first model call")
    context_tokens_at_end: int = Field(default=0, description="Input tokens on the last model call")
