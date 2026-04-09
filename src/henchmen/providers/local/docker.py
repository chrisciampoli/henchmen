"""Docker implementation of ContainerOrchestrator for local development."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from uuid import uuid4

from henchmen.providers.interfaces.container_orchestrator import JobResult, JobStatus

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)


class DockerOrchestrator:
    """ContainerOrchestrator backed by local Docker."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._processes: dict[str, asyncio.subprocess.Process] = {}

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
        """Launch a Docker container. Returns the container execution ID."""
        exec_id = f"docker-{uuid4().hex[:8]}"
        cmd = ["docker", "run", "--rm", "--name", exec_id]
        for k, v in env_vars.items():
            cmd.extend(["-e", f"{k}={v}"])
        mem = memory.lower().replace("gi", "g").replace("mi", "m")
        cmd.extend(["--memory", mem])
        cmd.append(image)
        logger.info("Starting Docker container %s with image %s", exec_id, image)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._processes[exec_id] = process
        return exec_id

    async def get_status(self, execution_id: str) -> JobResult:
        """Return the current status of a Docker container execution."""
        process = self._processes.get(execution_id)
        if process is None:
            return JobResult(job_id=execution_id, status=JobStatus.FAILED, exit_code=-1)
        if process.returncode is None:
            return JobResult(job_id=execution_id, status=JobStatus.RUNNING)
        status = JobStatus.COMPLETED if process.returncode == 0 else JobStatus.FAILED
        return JobResult(job_id=execution_id, status=status, exit_code=process.returncode)

    async def cancel(self, execution_id: str) -> None:
        """Send a docker kill to a running container."""
        await asyncio.create_subprocess_exec("docker", "kill", execution_id)

    async def stream_logs(self, execution_id: str) -> AsyncIterator[str]:
        """Stream stdout/stderr from a running or completed container."""
        process = await asyncio.create_subprocess_exec(
            "docker",
            "logs",
            "-f",
            execution_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        if process.stdout:
            async for line in process.stdout:
                yield line.decode()
