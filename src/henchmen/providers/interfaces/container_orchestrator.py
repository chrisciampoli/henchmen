"""ContainerOrchestrator interface — ephemeral container job execution."""

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    PROVISIONING = "provisioning"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class JobResult(BaseModel):
    """Status and metadata for a container job execution."""

    job_id: str = Field(..., description="Job identifier")
    status: JobStatus = Field(..., description="Current job status")
    exit_code: int | None = Field(default=None, description="Process exit code if completed")
    logs: str | None = Field(default=None, description="Job output logs")


@runtime_checkable
class ContainerOrchestrator(Protocol):
    """Abstraction over container job execution (Cloud Run Jobs, ECS Fargate, Docker)."""

    async def run_job(
        self,
        job_id: str,
        image: str,
        env_vars: dict[str, str],
        cpu: str = "4",
        memory: str = "8Gi",
        timeout_seconds: int = 1800,
        service_account: str | None = None,
        secrets: dict[str, str] | None = None,
    ) -> str:
        """Launch a container job. Returns execution ID."""
        ...

    async def get_status(self, execution_id: str) -> JobResult:
        """Get current status of a job execution."""
        ...

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running job."""
        ...

    def stream_logs(self, execution_id: str) -> AsyncIterator[str]:
        """Stream logs from a running or completed job."""
        ...
