"""GCP Cloud Run Jobs implementation of ContainerOrchestrator."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from henchmen.providers.interfaces.container_orchestrator import JobResult, JobStatus

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

# Knative-style condition names reported on a Cloud Run Execution. The SDK
# exposes ``Condition.type_`` as one of these strings and ``Condition.state``
# as a ``Condition.State`` enum (CONDITION_PENDING / CONDITION_RECONCILING /
# CONDITION_FAILED / CONDITION_SUCCEEDED) — there is no "CONDITION_TRUE".
_COMPLETED_CONDITION = "Completed"
_STARTED_CONDITION = "Started"

_STATE_SUCCEEDED = "CONDITION_SUCCEEDED"
_STATE_FAILED = "CONDITION_FAILED"

# Condition.ExecutionReason values meaning the execution was deliberately stopped.
_CANCELLED_REASONS = frozenset({"CANCELLED", "CANCELLING", "DELETED"})
# Condition.CommonReason value emitted when an execution blew its deadline.
_TIMEOUT_REASONS = frozenset({"PROGRESS_DEADLINE_EXCEEDED"})


def _int_field(obj: Any, name: str) -> int:
    """Read an integer counter off a proto message, tolerating absent fields."""
    value = getattr(obj, name, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _find_condition(execution: Any, type_name: str) -> Any:
    """Return the condition whose ``type_`` matches, or None."""
    for condition in getattr(execution, "conditions", None) or []:
        if getattr(condition, "type_", "") == type_name:
            return condition
    return None


def _state_name(condition: Any) -> str:
    """Return the ``Condition.State`` member name, or '' when unavailable."""
    if condition is None:
        return ""
    return str(getattr(getattr(condition, "state", None), "name", "") or "")


def _enum_name(condition: Any, field: str) -> str:
    """Return the member name of an enum field on a condition, or ''."""
    return str(getattr(getattr(condition, field, None), "name", "") or "")


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

    def job_name(self, job_id: str) -> str:
        """Return the fully-qualified Cloud Run Job resource name for ``job_id``."""
        if job_id.startswith("projects/"):
            return job_id
        return f"{self._parent}/jobs/{job_id}"

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
        """Create (or update) a Cloud Run Job and launch it.

        ``secrets`` maps an environment variable name to a Secret Manager
        secret resource (``projects/<p>/secrets/<name>``); each becomes a
        ``secret_key_ref`` env var so the value never transits our logs.

        Lair job ids are deterministic (``lair-<task>-<node>``), so a node
        retry or a CI-fix pass reuses one. Cloud Run rejects a duplicate
        ``create_job`` with ALREADY_EXISTS, so we fall back to updating the
        existing job in place rather than failing the dispatch.

        Returns the execution resource name.
        """
        from google.api_core import exceptions as api_exceptions
        from google.cloud.run_v2.types import (
            Container,
            EnvVar,
            EnvVarSource,
            ExecutionTemplate,
            Job,
            ResourceRequirements,
            SecretKeySelector,
            TaskTemplate,
        )

        env = [EnvVar(name=k, value=v) for k, v in env_vars.items()]
        for name, secret_ref in (secrets or {}).items():
            env.append(
                EnvVar(
                    name=name,
                    value_source=EnvVarSource(secret_key_ref=SecretKeySelector(secret=secret_ref, version="latest")),
                )
            )

        container = Container(
            image=image,
            env=env,
            resources=ResourceRequirements(limits={"cpu": cpu, "memory": memory}),
        )
        task_template = TaskTemplate(
            containers=[container],
            timeout=f"{timeout_seconds}s",
            max_retries=0,
            service_account=service_account or "",
        )
        # ExecutionTemplate's TaskTemplate field is named `template`, not
        # `task_template` — the wrong name raises ValueError at construction.
        job = Job(template=ExecutionTemplate(template=task_template, task_count=1))
        client = self._get_jobs_client()
        job_name = self.job_name(job_id)
        try:
            create_op = await client.create_job(parent=self._parent, job=job, job_id=job_id)
            await create_op.result()
        except api_exceptions.AlreadyExists:
            logger.info("Cloud Run job %s already exists — updating it in place", job_name)
            job.name = job_name
            update_op = await client.update_job(job=job)
            await update_op.result()

        # ``run_job`` returns a long-running operation whose *metadata* is the
        # Execution. Awaiting ``.result()`` would block until the whole job
        # finished, so read the metadata and fall back only if it is absent.
        operation = await client.run_job(name=job_name)
        execution_name = str(getattr(getattr(operation, "metadata", None), "name", "") or "")
        if not execution_name:
            execution_name = str((await operation.result()).name)
        return execution_name

    async def delete_job(self, job_id: str) -> None:
        """Delete a per-lair Cloud Run Job so job resources do not accumulate."""
        from google.api_core import exceptions as api_exceptions

        client = self._get_jobs_client()
        name = self.job_name(job_id)
        try:
            operation = await client.delete_job(name=name)
            await operation.result()
        except api_exceptions.NotFound:
            logger.debug("Cloud Run job %s already deleted", name)

    async def get_status(self, execution_id: str) -> JobResult:
        """Get current status of a Cloud Run Job execution."""
        client = self._get_exec_client()
        execution = await client.get_execution(name=execution_id)

        completed = _find_condition(execution, _COMPLETED_CONDITION)
        state = _state_name(completed)
        message = str(getattr(completed, "message", "") or "") or None

        if state == _STATE_SUCCEEDED:
            return JobResult(job_id=execution_id, status=JobStatus.COMPLETED, exit_code=0, logs=message)
        if state == _STATE_FAILED:
            return JobResult(
                job_id=execution_id,
                status=self._failure_status(execution, completed),
                logs=message,
            )

        started = _find_condition(execution, _STARTED_CONDITION)
        if _state_name(started) == _STATE_SUCCEEDED or _int_field(execution, "running_count") > 0:
            return JobResult(job_id=execution_id, status=JobStatus.RUNNING)
        return JobResult(job_id=execution_id, status=JobStatus.PROVISIONING)

    @staticmethod
    def _failure_status(execution: Any, condition: Any) -> JobStatus:
        """Distinguish cancellation and timeout from a plain failure."""
        if _enum_name(condition, "execution_reason") in _CANCELLED_REASONS:
            return JobStatus.CANCELLED
        if _int_field(execution, "cancelled_count") > 0:
            return JobStatus.CANCELLED
        if _enum_name(condition, "reason") in _TIMEOUT_REASONS:
            return JobStatus.TIMED_OUT
        return JobStatus.FAILED

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running Cloud Run Job execution."""
        client = self._get_exec_client()
        await client.cancel_execution(name=execution_id)

    async def stream_logs(self, execution_id: str) -> AsyncIterator[str]:
        """Stream logs from a Cloud Run Job execution (not yet implemented)."""
        return
        yield  # pragma: no cover — makes this an async generator
