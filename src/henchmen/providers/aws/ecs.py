"""AWS ECS Fargate implementation of ContainerOrchestrator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from henchmen.providers.interfaces.container_orchestrator import JobResult, JobStatus

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

# ECS Fargate last_status → JobStatus mapping
_STATUS_MAP: dict[str, JobStatus] = {
    "RUNNING": JobStatus.RUNNING,
    "STOPPED": JobStatus.COMPLETED,
    "PENDING": JobStatus.PROVISIONING,
    "DEACTIVATING": JobStatus.RUNNING,
    "STOPPING": JobStatus.RUNNING,
    "DEPROVISIONING": JobStatus.RUNNING,
    "PROVISIONING": JobStatus.PROVISIONING,
    "ACTIVATING": JobStatus.PROVISIONING,
}


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


class ECSOrchestrator:
    """ContainerOrchestrator backed by AWS ECS Fargate."""

    def __init__(self, settings: Settings) -> None:
        import boto3

        self._region = getattr(settings, "aws_region", "us-east-1")
        self._cluster = getattr(settings, "aws_ecs_cluster", "henchmen")
        subnets_str = getattr(settings, "aws_ecs_subnets", "")
        self._subnets: list[str] = [s.strip() for s in subnets_str.split(",") if s.strip()]
        sgs_str = getattr(settings, "aws_ecs_security_groups", "")
        self._security_groups: list[str] = [s.strip() for s in sgs_str.split(",") if s.strip()]
        self._client: Any = boto3.client("ecs", region_name=self._region)

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
                    "awslogs-group": f"/ecs/henchmen/{job_id}",
                    "awslogs-region": self._region,
                    "awslogs-stream-prefix": "ecs",
                },
            },
        }

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
            raise RuntimeError(f"ECS run_task returned no tasks: {reason}")
        return str(tasks[0]["taskArn"])

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
        status = _STATUS_MAP.get(last_status, JobStatus.RUNNING)

        # If STOPPED, check exit code to distinguish success vs failure
        exit_code: int | None = None
        if last_status == "STOPPED":
            containers = task.get("containers", [])
            if containers:
                exit_code = containers[0].get("exitCode")
            if exit_code is not None and exit_code != 0:
                status = JobStatus.FAILED

        return JobResult(job_id=execution_id, status=status, exit_code=exit_code)

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
