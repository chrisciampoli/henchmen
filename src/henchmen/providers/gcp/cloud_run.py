"""GCP Cloud Run Jobs implementation of ContainerOrchestrator."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from henchmen.providers.interfaces.container_orchestrator import JobResult, JobStatus

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


class CloudRunOrchestrator:
    """ContainerOrchestrator backed by Google Cloud Run Jobs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._project = settings.gcp_project_id
        self._region = settings.gcp_region
        self._parent = f"projects/{self._project}/locations/{self._region}"
        self._jobs_client: Any = None
        self._exec_client: Any = None

    def _get_jobs_client(self) -> Any:
        if self._jobs_client is None:
            from google.cloud import run_v2

            self._jobs_client = run_v2.JobsAsyncClient()
        return self._jobs_client

    def _get_exec_client(self) -> Any:
        if self._exec_client is None:
            from google.cloud import run_v2

            self._exec_client = run_v2.ExecutionsAsyncClient()
        return self._exec_client

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
        """Create and launch a Cloud Run Job. Returns the execution resource name."""
        from google.cloud.run_v2.types import (
            Container,
            EnvVar,
            ExecutionTemplate,
            Job,
            ResourceRequirements,
            TaskTemplate,
        )

        container = Container(
            image=image,
            env=[EnvVar(name=k, value=v) for k, v in env_vars.items()],
            resources=ResourceRequirements(limits={"cpu": cpu, "memory": memory}),
        )
        task_template = TaskTemplate(
            containers=[container],
            timeout=f"{timeout_seconds}s",
            max_retries=0,
            service_account=service_account or "",
        )
        job = Job(template=ExecutionTemplate(task_template=task_template, task_count=1))
        client = self._get_jobs_client()
        created = await client.create_job(parent=self._parent, job=job, job_id=job_id)
        execution = await client.run_job(name=created.name)
        return str(execution.name)

    async def get_status(self, execution_id: str) -> JobResult:
        """Get current status of a Cloud Run Job execution."""
        client = self._get_exec_client()
        execution = await client.get_execution(name=execution_id)
        status_map = {
            "CONDITION_SUCCEEDED": JobStatus.COMPLETED,
            "CONDITION_FAILED": JobStatus.FAILED,
            "CONDITION_RUNNING": JobStatus.RUNNING,
        }
        status = JobStatus.PROVISIONING
        for condition in execution.conditions:
            if condition.type_ in status_map and condition.state.name == "CONDITION_TRUE":
                status = status_map[condition.type_]
                break
        return JobResult(job_id=execution_id, status=status)

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running Cloud Run Job execution."""
        client = self._get_exec_client()
        await client.cancel_execution(name=execution_id)

    async def stream_logs(self, execution_id: str) -> AsyncIterator[str]:
        """Stream logs from a Cloud Run Job execution (not yet implemented)."""
        return
        yield  # pragma: no cover — makes this an async generator
