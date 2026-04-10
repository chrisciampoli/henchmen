"""AWS CodeBuild implementation of CIProvider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import yaml

from henchmen.providers.interfaces.ci_provider import CIResult, CIStatus

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

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
    """Generate a CodeBuild buildspec YAML string."""
    spec = {
        "version": "0.2",
        "phases": {
            "install": {
                "commands": [
                    f"git clone -b {branch} {repo_url} .",
                ]
            },
            "build": {
                "commands": commands,
            },
        },
    }
    return str(yaml.dump(spec, default_flow_style=False))


class CodeBuildCIProvider:
    """CIProvider backed by AWS CodeBuild."""

    def __init__(self, settings: Settings | None = None) -> None:
        import boto3

        region = getattr(settings, "aws_region", "us-east-1") if settings else "us-east-1"
        prefix = getattr(settings, "aws_resource_prefix", "henchmen") if settings else "henchmen"
        self._project_name = f"{prefix}-ci"
        self._client: Any = boto3.client("codebuild", region_name=region)

    async def trigger_build(
        self,
        repo_url: str,
        branch: str,
        commands: list[str],
        timeout_seconds: int = 600,
    ) -> str:
        """Start a CodeBuild build with an inline buildspec. Returns build ID."""
        buildspec = _build_buildspec(repo_url, branch, commands)
        response = await asyncio.to_thread(
            self._client.start_build,
            projectName=self._project_name,
            buildspecOverride=buildspec,
            timeoutInMinutesOverride=max(1, timeout_seconds // 60),
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
        status = _STATUS_MAP.get(build_status, CIStatus.PENDING)

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
        """Return the CloudWatch logs URL for a CodeBuild build."""
        result = await self.get_status(build_id)
        return result.logs_url or f"https://console.aws.amazon.com/codesuite/codebuild/builds/{build_id}/view/new"

    async def cancel(self, build_id: str) -> None:
        """Stop a running CodeBuild build."""
        await asyncio.to_thread(self._client.stop_build, id=build_id)
