"""Task models - represents work items flowing through the Henchmen system."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class TaskSource(StrEnum):
    SLACK = "slack"
    JIRA = "jira"
    GITHUB = "github"
    CLI = "cli"


class TaskPriority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class TaskStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    ESCALATED = "escalated"


class TaskContext(BaseModel):
    """Contextual information gathered from the task source."""

    repo: str = Field(..., description="Repository name (owner/repo)")
    branch: str | None = Field(default=None, description="Target branch")
    thread_messages: list[str] | None = Field(default=None, description="Slack thread messages or comment history")
    issue_fields: dict[str, str] | None = Field(default=None, description="Raw issue/ticket fields from source system")
    pr_diff: str | None = Field(default=None, description="Pull request diff text")


class HenchmenTask(BaseModel):
    """A unit of work to be executed by an operative."""

    id: str = Field(default_factory=lambda: str(uuid4()), description="Unique task identifier (UUID)")
    source: TaskSource = Field(..., description="Origin system that created the task")
    source_id: str = Field(..., description="Identifier of the task in the source system")
    title: str = Field(..., description="Short human-readable task title")
    description: str = Field(..., description="Full task description")
    context: TaskContext = Field(..., description="Contextual information for the operative")
    priority: TaskPriority = Field(default=TaskPriority.NORMAL, description="Task execution priority")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="UTC timestamp of task creation"
    )
    created_by: str = Field(..., description="User or system that created the task")
    status: TaskStatus = Field(default=TaskStatus.PENDING, description="Current lifecycle status")
