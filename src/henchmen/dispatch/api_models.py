"""Request models for the Dispatch HTTP API.

Dispatch validates every request body against a Pydantic model before it
reaches a handler, so malformed input returns 422 from FastAPI rather than
surfacing as a 500 from deep inside the normalizer.
"""

from pydantic import BaseModel, ConfigDict, Field

from henchmen.models.task import TaskPriority


class CreateTaskRequest(BaseModel):
    """Body of ``POST /api/v1/tasks`` (the CLI / REST intake path)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=200, description="Short human-readable task title")
    description: str = Field(default="", description="Full task description")
    repo: str = Field(default="", description="Target repository as 'owner/name'")
    branch: str | None = Field(default=None, description="Target branch")
    priority: TaskPriority = Field(default=TaskPriority.NORMAL, description="Task execution priority")
    created_by: str = Field(default="cli", description="User or system that created the task")
    id: str | None = Field(default=None, description="Caller-supplied source identifier")
