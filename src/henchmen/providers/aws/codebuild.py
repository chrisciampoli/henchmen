"""AWS CodeBuild implementation of CIProvider."""

from __future__ import annotations

import asyncio
import math
import shlex
from typing import TYPE_CHECKING, Any

import yaml

from henchmen.providers.interfaces.ci_provider import CIResult, CIStatus

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

# CodeBuild rejects timeoutInMinutesOverride below 5 with a ValidationException.
_MIN_TIMEOUT_MINUTES = 5

_STATUS_MAP: dict[str, CIStatus] = {
    "SUCCEEDED": CIStatus.SUCCESS,
    "FAILED": CIStatus.FAILURE,
    "FAULT": CIStatus.FAILURE,
    "TIMED_OUT": CIStatus.TIMEOUT,
    "STOPPED": CIStatus.CANCELLED,
    "IN_PROGRESS": CIStatus.RUNNING,
    "QUEUED": CIStatus.PENDING,
}


def _build_buildspec(repo_url: str, branch: str, commands: list[str]) -> str:
    """Generate a CodeBuild buildspec YAML string.

    ``branch`` and ``repo_url`` originate from task metadata, so they are
    shell-quoted before being interpolated into the clone command.
    """
    clone = f"git clone --branch {shlex.quote(branch)} -- {shlex.quote(repo_url)} ."
    spec = {
        "version": "0.2",
        "phases": {
            "install": {"commands": [clone]},
            "build": {
                "commands": commands,
            },
        },
    }
    return str(yaml.dump(spec, default_flow_style=False))


class CodeBuildCIProvider:
    """CIProvider backed by AWS CodeBuild."""

    def __init__(self, settings: Settings) -> None:
        import boto3

        self._project_name = f"{settings.aws_resource_prefix}-ci"
        self._client: Any = boto3.client("codebuild", region_name=settings.aws_region)

    async def trigger_build(
        self,
        repo_url: str,
        branch: str,
        commands: list[str],
        timeout_seconds: int = 600,
    ) -> str:
        """Start a CodeBuild build with an inline buildspec. Returns build ID.

        Returns as soon as the build is queued — poll :meth:`get_status` until
        a terminal ``CIStatus``.
        """
        buildspec = _build_buildspec(repo_url, branch, commands)
        timeout_minutes = max(_MIN_TIMEOUT_MINUTES, math.ceil(timeout_seconds / 60))
        response = await asyncio.to_thread(
            self._client.start_build,
            projectName=self._project_name,
            buildspecOverride=buildspec,
            timeoutInMinutesOverride=timeout_minutes,
        )
        return str(response["build"]["id"])

    async def get_status(self, build_id: str) -> CIResult:
        """Get the current status of a CodeBuild build."""
        response = await asyncio.to_thread(
            self._client.batch_get_builds,
            ids=[build_id],
        )
        builds = response.get("builds", [])
        if not builds:
            return CIResult(
                build_id=build_id,
                status=CIStatus.FAILURE,
                error_message="Build not found",
            )
        build = builds[0]
        build_status: str = build.get("buildStatus", "IN_PROGRESS")
        # Unrecognised statuses fail closed: a PENDING reading would make
        # callers poll for ever instead of surfacing the problem.
        status = _STATUS_MAP.get(build_status, CIStatus.FAILURE)

        logs_url: str | None = None
        logs_info = build.get("logs", {})
        if logs_info.get("deepLink"):
            logs_url = logs_info["deepLink"]

        duration: float | None = None
        start_time = build.get("startTime")
        end_time = build.get("endTime")
        if start_time and end_time:
            duration = (end_time - start_time).total_seconds()

        error_message: str | None = None
        if status == CIStatus.FAILURE:
            phases = build.get("phases", [])
            for phase in phases:
                if phase.get("phaseStatus") == "FAILED":
                    ctx = phase.get("contexts", [])
                    if ctx:
                        error_message = ctx[0].get("message", "")
                    break

        return CIResult(
            build_id=build_id,
            status=status,
            logs_url=logs_url,
            duration_seconds=duration,
            error_message=error_message,
        )

    async def get_logs(self, build_id: str) -> str:
        """Return the CloudWatch logs URL for a CodeBuild build.

        CodeBuild keeps log text in CloudWatch, so this is a URL rather than
        log content (see the CIProvider docstring for the contract).
        """
        result = await self.get_status(build_id)
        return result.logs_url or f"https://console.aws.amazon.com/codesuite/codebuild/builds/{build_id}/view/new"

    async def cancel(self, build_id: str) -> None:
        """Stop a running CodeBuild build."""
        await asyncio.to_thread(self._client.stop_build, id=build_id)
