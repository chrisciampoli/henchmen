"""CIProvider interface — CI pipeline triggering and monitoring."""

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class CIStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class CIResult(BaseModel):
    """Result of a CI build."""

    build_id: str = Field(..., description="Build identifier")
    status: CIStatus = Field(..., description="Current build status")
    logs_url: str | None = Field(default=None, description="URL to build logs")
    duration_seconds: float | None = Field(default=None, description="Build duration")
    error_message: str | None = Field(default=None, description="Error details if failed")


@runtime_checkable
class CIProvider(Protocol):
    """Abstraction over CI systems (Cloud Build, CodeBuild, shell commands)."""

    async def trigger_build(
        self,
        repo_url: str,
        branch: str,
        commands: list[str],
        timeout_seconds: int = 600,
    ) -> str:
        """Trigger a CI build. Returns build ID."""
        ...

    async def get_status(self, build_id: str) -> CIResult:
        """Get current status of a build."""
        ...

    async def get_logs(self, build_id: str) -> str:
        """Get build logs."""
        ...

    async def cancel(self, build_id: str) -> None:
        """Cancel a running build."""
        ...
