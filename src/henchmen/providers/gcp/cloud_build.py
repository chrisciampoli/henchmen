"""GCP Cloud Build implementation of CIProvider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from henchmen.providers.interfaces.ci_provider import CIResult, CIStatus

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


class CloudBuildCIProvider:
    """CIProvider backed by Google Cloud Build."""

    def __init__(self, settings: Settings) -> None:
        self._project = settings.gcp_project_id
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import cloudbuild_v1  # type: ignore[attr-defined]

            self._client = cloudbuild_v1.CloudBuildAsyncClient()
        return self._client

    async def trigger_build(
        self,
        repo_url: str,
        branch: str,
        commands: list[str],
        timeout_seconds: int = 600,
    ) -> str:
        """Trigger a Cloud Build build. Returns the build ID."""
        from google.cloud import cloudbuild_v1  # type: ignore[attr-defined]

        steps = [
            cloudbuild_v1.BuildStep(
                name="gcr.io/cloud-builders/git",
                args=["clone", "-b", branch, repo_url, "."],
            )
        ]
        for cmd in commands:
            steps.append(cloudbuild_v1.BuildStep(name="node:20", entrypoint="bash", args=["-c", cmd]))
        build = cloudbuild_v1.Build(steps=steps, timeout=f"{timeout_seconds}s")
        client = self._get_client()
        operation = await client.create_build(project_id=self._project, build=build)
        result = await operation.result()
        return str(result.id)

    async def get_status(self, build_id: str) -> CIResult:
        """Get the current status of a Cloud Build build."""
        from google.cloud import cloudbuild_v1  # type: ignore[attr-defined]

        client = self._get_client()
        build = await client.get_build(project_id=self._project, id=build_id)
        status_map = {
            cloudbuild_v1.Build.Status.SUCCESS: CIStatus.SUCCESS,
            cloudbuild_v1.Build.Status.FAILURE: CIStatus.FAILURE,
            cloudbuild_v1.Build.Status.TIMEOUT: CIStatus.TIMEOUT,
            cloudbuild_v1.Build.Status.CANCELLED: CIStatus.CANCELLED,
            cloudbuild_v1.Build.Status.WORKING: CIStatus.RUNNING,
            cloudbuild_v1.Build.Status.QUEUED: CIStatus.PENDING,
        }
        logs_url = build.log_url if hasattr(build, "log_url") else None
        return CIResult(
            build_id=build_id,
            status=status_map.get(build.status, CIStatus.PENDING),
            logs_url=logs_url,
        )

    async def get_logs(self, build_id: str) -> str:
        """Return the logs URL for a Cloud Build build."""
        result = await self.get_status(build_id)
        return result.logs_url or f"https://console.cloud.google.com/cloud-build/builds/{build_id}"

    async def cancel(self, build_id: str) -> None:
        """Cancel a running Cloud Build build."""
        client = self._get_client()
        await client.cancel_build(project_id=self._project, id=build_id)
