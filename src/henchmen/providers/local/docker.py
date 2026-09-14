"""Docker implementation of ContainerOrchestrator for local development."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING
from uuid import uuid4

from henchmen.providers.interfaces.container_orchestrator import JobResult, JobStatus

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

# Containers run with --rm, so `docker logs` is unavailable once they exit.
# Keep the last N drained lines per execution instead.
_LOG_BUFFER_LINES = 2000
# Finished executions whose status and logs stay queryable after they exit.
_FINISHED_RETAINED = 50


class DockerOrchestrator:
    """ContainerOrchestrator backed by local Docker."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._drain_tasks: dict[str, asyncio.Task[None]] = {}
        self._timeout_tasks: dict[str, asyncio.Task[None]] = {}
        self._logs: dict[str, deque[str]] = {}
        self._timed_out: set[str] = set()
        self._finished: deque[str] = deque()

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
        # Allow container to reach host services (Ollama, Henchmen server)
        cmd.extend(["--add-host=host.docker.internal:host-gateway"])
        if self._settings.local_docker_network:
            cmd.extend(["--network", self._settings.local_docker_network])
        for k, v in env_vars.items():
            cmd.extend(["-e", f"{k}={v}"])
        mem = memory.lower().replace("gi", "g").replace("mi", "m")
        cmd.extend(["--memory", mem])
        cpu_limit = _cpu_limit(cpu)
        if cpu_limit:
            cmd.extend(["--cpus", cpu_limit])
        cmd.append(image)
        logger.info("Starting Docker container %s with image %s", exec_id, image)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._processes[exec_id] = process
        buffer: deque[str] = deque(maxlen=_LOG_BUFFER_LINES)
        self._logs[exec_id] = buffer

        # Drain stdout in the background so the pipe buffer never fills up
        # (a full pipe would block the Docker CLI and hang the container).
        async def _drain(proc: asyncio.subprocess.Process, eid: str) -> None:
            if proc.stdout is None:
                return
            async for line in proc.stdout:
                text = line.decode(errors="replace").rstrip()
                if text:
                    buffer.append(text)
                    logger.info("[operative:%s] %s", eid[:12], text)

        task = asyncio.create_task(_drain(process, exec_id))
        self._drain_tasks[exec_id] = task
        self._timeout_tasks[exec_id] = asyncio.create_task(self._enforce_timeout(exec_id, timeout_seconds))
        return exec_id

    async def _enforce_timeout(self, exec_id: str, timeout_seconds: int) -> None:
        """Kill the container once ``timeout_seconds`` elapses and mark it TIMED_OUT."""
        process = self._processes.get(exec_id)
        if process is None or timeout_seconds <= 0:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            return
        except TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
        logger.warning("Docker container %s exceeded %ss — killing it", exec_id, timeout_seconds)
        self._timed_out.add(exec_id)
        await self._docker_kill(exec_id)

    @staticmethod
    async def _docker_kill(execution_id: str) -> None:
        """Run `docker kill` and wait for it, logging a non-zero exit."""
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "kill",
            execution_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "docker kill %s exited %s: %s",
                execution_id,
                proc.returncode,
                (stdout or b"").decode(errors="replace").strip(),
            )

    async def get_status(self, execution_id: str) -> JobResult:
        """Return the current status of a Docker container execution."""
        process = self._processes.get(execution_id)
        if process is None:
            return JobResult(job_id=execution_id, status=JobStatus.FAILED, exit_code=-1)
        if process.returncode is None:
            return JobResult(job_id=execution_id, status=JobStatus.RUNNING)
        self._cleanup(execution_id)
        if execution_id in self._timed_out:
            # A killed-on-timeout container must never be reported as completed:
            # its verification work never ran.
            return JobResult(
                job_id=execution_id,
                status=JobStatus.TIMED_OUT,
                exit_code=process.returncode,
                logs=self._buffered_logs(execution_id),
            )
        status = JobStatus.COMPLETED if process.returncode == 0 else JobStatus.FAILED
        return JobResult(
            job_id=execution_id,
            status=status,
            exit_code=process.returncode,
            logs=self._buffered_logs(execution_id),
        )

    def _cleanup(self, execution_id: str) -> None:
        """Drop finished bookkeeping for a terminated execution.

        The process handle and log buffer stay available for the most recent
        :data:`_FINISHED_RETAINED` executions (callers re-poll ``get_status``
        and read ``stream_logs`` after completion); older ones are evicted so
        a long-running ``henchmen serve`` does not grow without bound.
        """
        timeout_task = self._timeout_tasks.pop(execution_id, None)
        if timeout_task is not None and not timeout_task.done():
            timeout_task.cancel()
        drain_task = self._drain_tasks.get(execution_id)
        if drain_task is not None and drain_task.done():
            self._drain_tasks.pop(execution_id, None)
        process = self._processes.get(execution_id)
        if process is not None and process.returncode is not None and execution_id not in self._finished:
            self._finished.append(execution_id)
        while len(self._finished) > _FINISHED_RETAINED:
            evicted = self._finished.popleft()
            self._processes.pop(evicted, None)
            self._logs.pop(evicted, None)
            self._timed_out.discard(evicted)
            self._drain_tasks.pop(evicted, None)

    def _buffered_logs(self, execution_id: str) -> str | None:
        buffer = self._logs.get(execution_id)
        if not buffer:
            return None
        return "\n".join(buffer)

    async def cancel(self, execution_id: str) -> None:
        """Send a docker kill to a running container and wait for it."""
        self._cleanup(execution_id)
        await self._docker_kill(execution_id)

    async def stream_logs(self, execution_id: str) -> AsyncIterator[str]:
        """Yield the drained stdout/stderr captured for an execution.

        Containers are started with ``--rm``, so once one exits Docker has
        already removed it and ``docker logs`` would fail; the drain task's
        buffer is the only surviving copy.
        """
        buffer = self._logs.get(execution_id)
        if buffer is None:
            return
        for line in list(buffer):
            yield line


def _cpu_limit(cpu: str) -> str:
    """Return a `--cpus` value for a numeric vCPU string, else ''."""
    try:
        value = float(cpu)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return f"{value:g}"
