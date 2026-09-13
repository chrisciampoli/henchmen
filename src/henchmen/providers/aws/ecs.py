"""AWS ECS Fargate implementation of ContainerOrchestrator."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from henchmen.providers.interfaces.container_orchestrator import JobResult, JobStatus

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

# ECS Fargate last_status → JobStatus mapping. STOPPED is deliberately absent:
# a stopped task is only COMPLETED when its container exited zero, which
# get_status determines from the exit code and stopCode.
_STATUS_MAP: dict[str, JobStatus] = {
    "RUNNING": JobStatus.RUNNING,
    "PENDING": JobStatus.PROVISIONING,
    "DEACTIVATING": JobStatus.RUNNING,
    "STOPPING": JobStatus.RUNNING,
    "DEPROVISIONING": JobStatus.RUNNING,
    "PROVISIONING": JobStatus.PROVISIONING,
    "ACTIVATING": JobStatus.PROVISIONING,
}

_LOG_GROUP = "/ecs/henchmen"


def _cpu_to_fargate_units(cpu: str) -> str:
    """Convert vCPU string (e.g. '4') to Fargate CPU units (e.g. '4096')."""
    try:
        return str(int(float(cpu) * 1024))
    except ValueError:
        return "4096"


def _memory_to_mb(memory: str) -> str:
    """Convert memory string (e.g. '8Gi') to MB integer string (e.g. '8192')."""
    memory = memory.strip()
    if memory.endswith("Gi"):
        return str(int(float(memory[:-2]) * 1024))
    if memory.endswith("Mi"):
        return str(int(float(memory[:-2])))
    if memory.endswith("G"):
        return str(int(float(memory[:-1]) * 1000))
    if memory.endswith("M"):
        return str(int(float(memory[:-1])))
    # Assume already MB
    return str(int(float(memory)))


def _optional_setting(settings: Settings, name: str) -> str:
    """Read an optional string setting that may not exist on this Settings class."""
    value = getattr(settings, name, "")
    return value.strip() if isinstance(value, str) else ""


class ECSOrchestrator:
    """ContainerOrchestrator backed by AWS ECS Fargate."""

    def __init__(self, settings: Settings) -> None:
        import boto3

        self._region = settings.aws_region
        self._cluster = settings.aws_ecs_cluster
        self._subnets: list[str] = [s.strip() for s in settings.aws_ecs_subnets.split(",") if s.strip()]
        self._security_groups: list[str] = [s.strip() for s in settings.aws_ecs_security_groups.split(",") if s.strip()]
        # Fargate refuses a task definition that uses the awslogs driver
        # without an execution role. Optional until Settings grows the field.
        self._execution_role = _optional_setting(settings, "aws_ecs_execution_role_arn")
        self._client: Any = boto3.client("ecs", region_name=self._region)
        # task ARN -> (deadline monotonic seconds, task definition ARN)
        self._deadlines: dict[str, float] = {}
        self._task_definitions: dict[str, str] = {}

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
        """Register an ECS task definition and run a Fargate task. Returns task ARN."""
        cpu_units = _cpu_to_fargate_units(cpu)
        memory_mb = _memory_to_mb(memory)

        container_def: dict[str, Any] = {
            "name": "operative",
            "image": image,
            "essential": True,
            "environment": [{"name": k, "value": v} for k, v in env_vars.items()],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    # One shared group with the job id as the stream prefix:
                    # a per-job group would have to exist before the task
                    # starts or the task dies with ResourceInitializationError.
                    "awslogs-group": _LOG_GROUP,
                    "awslogs-region": self._region,
                    "awslogs-stream-prefix": job_id,
                    "awslogs-create-group": "true",
                },
            },
        }
        if secrets:
            # Values stay in Secrets Manager / SSM; only the ARN is sent.
            container_def["secrets"] = [{"name": k, "valueFrom": v} for k, v in secrets.items()]

        task_def: dict[str, Any] = {
            "family": f"henchmen-{job_id}",
            "networkMode": "awsvpc",
            "requiresCompatibilities": ["FARGATE"],
            "cpu": cpu_units,
            "memory": memory_mb,
            "containerDefinitions": [container_def],
        }
        if service_account:
            task_def["taskRoleArn"] = service_account
        if self._execution_role:
            task_def["executionRoleArn"] = self._execution_role

        reg_response = await asyncio.to_thread(self._client.register_task_definition, **task_def)
        task_def_arn: str = reg_response["taskDefinition"]["taskDefinitionArn"]

        run_kwargs: dict[str, Any] = {
            "cluster": self._cluster,
            "taskDefinition": task_def_arn,
            "launchType": "FARGATE",
            "startedBy": job_id,
            "overrides": {"containerOverrides": []},
        }
        if self._subnets or self._security_groups:
            run_kwargs["networkConfiguration"] = {
                "awsvpcConfiguration": {
                    "subnets": self._subnets,
                    "securityGroups": self._security_groups,
                    "assignPublicIp": "ENABLED",
                }
            }

        run_response = await asyncio.to_thread(self._client.run_task, **run_kwargs)
        tasks = run_response.get("tasks", [])
        if not tasks:
            failures = run_response.get("failures", [])
            reason = failures[0].get("reason", "unknown") if failures else "unknown"
            await self._deregister(task_def_arn)
            raise RuntimeError(f"ECS run_task returned no tasks: {reason}")
        task_arn = str(tasks[0]["taskArn"])
        # ECS has no native task timeout, so we track the deadline ourselves.
        if timeout_seconds > 0:
            self._deadlines[task_arn] = time.monotonic() + timeout_seconds
        self._task_definitions[task_arn] = task_def_arn
        return task_arn

    async def get_status(self, execution_id: str) -> JobResult:
        """Describe an ECS task and map its status to JobResult."""
        response = await asyncio.to_thread(
            self._client.describe_tasks,
            cluster=self._cluster,
            tasks=[execution_id],
        )
        tasks = response.get("tasks", [])
        if not tasks:
            return JobResult(job_id=execution_id, status=JobStatus.FAILED, exit_code=-1)

        task = tasks[0]
        last_status: str = task.get("lastStatus", "UNKNOWN")

        if last_status == "STOPPED":
            return await self._stopped_result(execution_id, task)

        status = _STATUS_MAP.get(last_status, JobStatus.RUNNING)
        deadline = self._deadlines.get(execution_id)
        if deadline is not None and time.monotonic() > deadline:
            logger.warning("ECS task %s exceeded its timeout — stopping it", execution_id)
            await self.cancel(execution_id)
            self._deadlines.pop(execution_id, None)
            await self._deregister(self._task_definitions.pop(execution_id, ""))
            return JobResult(job_id=execution_id, status=JobStatus.TIMED_OUT)
        return JobResult(job_id=execution_id, status=status)

    async def _stopped_result(self, execution_id: str, task: dict[str, Any]) -> JobResult:
        """Classify a STOPPED task, failing closed when there is no exit code."""
        self._deadlines.pop(execution_id, None)
        await self._deregister(self._task_definitions.pop(execution_id, ""))

        containers = task.get("containers", [])
        exit_code: int | None = containers[0].get("exitCode") if containers else None
        stop_code = str(task.get("stopCode", "") or "")
        stopped_reason = str(task.get("stoppedReason", "") or "") or None

        if stop_code == "UserInitiated":
            status = JobStatus.CANCELLED
        elif exit_code == 0:
            status = JobStatus.COMPLETED
        else:
            # No exit code means the container never ran (image pull failure,
            # ResourceInitializationError, capacity loss). Treating that as
            # COMPLETED would let the pipeline fabricate a successful report.
            status = JobStatus.FAILED
        return JobResult(job_id=execution_id, status=status, exit_code=exit_code, logs=stopped_reason)

    async def _deregister(self, task_definition_arn: str) -> None:
        """Deregister a per-job task definition so revisions do not accumulate."""
        if not task_definition_arn:
            return
        try:
            await asyncio.to_thread(
                self._client.deregister_task_definition,
                taskDefinition=task_definition_arn,
            )
        except Exception as exc:  # noqa: BLE001 — cleanup must never fail the caller
            logger.warning("Failed to deregister ECS task definition %s: %s", task_definition_arn, exc)

    async def cancel(self, execution_id: str) -> None:
        """Stop a running ECS task."""
        await asyncio.to_thread(
            self._client.stop_task,
            cluster=self._cluster,
            task=execution_id,
            reason="Cancelled by Henchmen",
        )

    async def stream_logs(self, execution_id: str) -> AsyncIterator[str]:
        """Log streaming is not implemented for ECS (use CloudWatch directly)."""
        return
        yield  # pragma: no cover — makes this an async generator
