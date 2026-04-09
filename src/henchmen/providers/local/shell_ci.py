"""Shell-based CIProvider for local development."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from henchmen.providers.interfaces.ci_provider import CIResult, CIStatus

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)


class ShellCIProvider:
    """CIProvider that runs commands locally via subprocess."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._builds: dict[str, dict[str, Any]] = {}

    async def trigger_build(
        self,
        repo_url: str,
        branch: str,
        commands: list[str],
        timeout_seconds: int = 600,
    ) -> str:
        """Run all commands sequentially. Returns a build ID with accumulated logs."""
        build_id = f"shell-{uuid4().hex[:8]}"
        self._builds[build_id] = {"status": CIStatus.RUNNING, "logs": "", "start": time.time()}
        all_logs: list[str] = []
        final_status = CIStatus.SUCCESS
        for cmd in commands:
            logger.info("Running CI command: %s", cmd)
            try:
                proc = await asyncio.wait_for(
                    asyncio.create_subprocess_shell(
                        cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    ),
                    timeout=timeout_seconds,
                )
                stdout, _ = await proc.communicate()
                output = stdout.decode() if stdout else ""
                all_logs.append(f"$ {cmd}\n{output}")
                if proc.returncode != 0:
                    final_status = CIStatus.FAILURE
                    break
            except TimeoutError:
                all_logs.append(f"$ {cmd}\nTIMEOUT after {timeout_seconds}s")
                final_status = CIStatus.TIMEOUT
                break
        elapsed = time.time() - self._builds[build_id]["start"]
        self._builds[build_id] = {"status": final_status, "logs": "\n".join(all_logs), "duration": elapsed}
        return build_id

    async def get_status(self, build_id: str) -> CIResult:
        """Return the status and duration for a completed build."""
        build = self._builds.get(build_id)
        if build is None:
            return CIResult(build_id=build_id, status=CIStatus.FAILURE, error_message="Build not found")
        return CIResult(build_id=build_id, status=build["status"], duration_seconds=build.get("duration"))

    async def get_logs(self, build_id: str) -> str:
        """Return the combined stdout/stderr for a completed build."""
        build = self._builds.get(build_id)
        if build is None:
            return ""
        logs: str = build.get("logs", "")
        return logs

    async def cancel(self, build_id: str) -> None:
        """Mark a build as cancelled (no-op if the build already completed)."""
        self._builds[build_id] = {"status": CIStatus.CANCELLED, "logs": "Cancelled", "duration": 0}
