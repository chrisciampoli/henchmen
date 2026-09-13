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


TERMINAL_CI_STATUSES: frozenset[CIStatus] = frozenset(
    {CIStatus.SUCCESS, CIStatus.FAILURE, CIStatus.CANCELLED, CIStatus.TIMEOUT}
)


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
        """Submit a CI build and return its build ID.

        Implementations MAY return before the build finishes (Cloud Build and
        CodeBuild both do) — callers must therefore poll :meth:`get_status`
        until it reports a status in :data:`TERMINAL_CI_STATUSES` rather than
        treating the first reading as final. ``timeout_seconds`` bounds the
        build itself, not this call.
        """
        ...

    async def get_status(self, build_id: str) -> CIResult:
        """Get current status of a build."""
        ...

    async def get_logs(self, build_id: str) -> str:
        """Get build logs, or a URL to them.

        Hosted providers (Cloud Build, CodeBuild) stream logs to their own
        log sinks and return a console URL; the local shell provider returns
        the captured stdout/stderr text. Callers that parse log content must
        not assume they received text.
        """
        ...

    async def cancel(self, build_id: str) -> None:
        """Cancel a running build."""
        ...
