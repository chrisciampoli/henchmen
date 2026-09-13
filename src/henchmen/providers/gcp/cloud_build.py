"""GCP Cloud Build implementation of CIProvider."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any

from henchmen.providers.interfaces.ci_provider import CIResult, CIStatus

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

# Image the CI commands run in. Cloud Build's own `node:20` default could not
# run the Python commands the CI orchestrator emits. Override per deployment
# with the (optional) ``ci_builder_image`` setting.
_DEFAULT_BUILDER_IMAGE = "python:3.12"

_GIT_IMAGE = "gcr.io/cloud-builders/git"
_TOKEN_ENV = "GITHUB_TOKEN"


def _optional_setting(settings: Settings, name: str) -> str:
    """Read an optional string setting that may not exist on this Settings class."""
    value = getattr(settings, name, "")
    return value.strip() if isinstance(value, str) else ""


class CloudBuildCIProvider:
    """CIProvider backed by Google Cloud Build."""

    def __init__(self, settings: Settings) -> None:
        self._project = settings.gcp_project_id
        # Optional settings: read defensively so this provider keeps working
        # against a Settings class that has not grown the fields yet.
        self._builder_image = _optional_setting(settings, "ci_builder_image") or _DEFAULT_BUILDER_IMAGE
        self._token_secret = _optional_setting(settings, "ci_github_token_secret")
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import cloudbuild_v1  # type: ignore[attr-defined]

            self._client = cloudbuild_v1.CloudBuildAsyncClient()
        return self._client

    def _clone_step(self, cloudbuild_v1: Any, repo_url: str, branch: str) -> Any:
        """Build the git-clone step, authenticated when a token secret is configured.

        Without ``ci_github_token_secret`` the clone is anonymous (public
        repositories only). With it, the token is injected as a Cloud Build
        ``secret_env`` and only dereferenced inside the step's shell, so the
        secret value never appears in the build config or in our logs.
        """
        if not self._token_secret or not repo_url.startswith("https://"):
            return cloudbuild_v1.BuildStep(
                name=_GIT_IMAGE,
                args=["clone", "--branch", branch, "--", repo_url, "."],
            )
        authed_url = repo_url.replace("https://", f"https://x-access-token:$${_TOKEN_ENV}@", 1)
        # $$ is Cloud Build's escape for a literal $, so the shell — not the
        # build-config substitution engine — expands the token.
        script = f"git clone --branch {shlex.quote(branch)} -- {shlex.quote(authed_url)} ."
        return cloudbuild_v1.BuildStep(
            name=_GIT_IMAGE,
            entrypoint="bash",
            args=["-c", script],
            secret_env=[_TOKEN_ENV],
        )

    def _available_secrets(self, cloudbuild_v1: Any) -> Any:
        """Return the Secret Manager binding for the clone token, or None."""
        if not self._token_secret:
            return None
        version_name = f"projects/{self._project}/secrets/{self._token_secret}/versions/latest"
        return cloudbuild_v1.Secrets(
            secret_manager=[cloudbuild_v1.SecretManagerSecret(version_name=version_name, env=_TOKEN_ENV)]
        )

    async def trigger_build(
        self,
        repo_url: str,
        branch: str,
        commands: list[str],
        timeout_seconds: int = 600,
    ) -> str:
        """Submit a Cloud Build build and return its ID without waiting for it.

        Callers must poll :meth:`get_status` until a terminal ``CIStatus``.
        """
        from google.cloud import cloudbuild_v1  # type: ignore[attr-defined]

        steps = [self._clone_step(cloudbuild_v1, repo_url, branch)]
        for cmd in commands:
            steps.append(cloudbuild_v1.BuildStep(name=self._builder_image, entrypoint="bash", args=["-c", cmd]))
        build_kwargs: dict[str, Any] = {"steps": steps, "timeout": f"{timeout_seconds}s"}
        secrets = self._available_secrets(cloudbuild_v1)
        if secrets is not None:
            build_kwargs["available_secrets"] = secrets
        build = cloudbuild_v1.Build(**build_kwargs)

        client = self._get_client()
        operation = await client.create_build(project_id=self._project, build=build)
        # The LRO's metadata carries the queued Build; awaiting .result() would
        # block for the whole build, which trigger_build must not do.
        build_id = str(getattr(getattr(getattr(operation, "metadata", None), "build", None), "id", "") or "")
        if not build_id:
            build_id = str((await operation.result()).id)
        return build_id

    def _status_map(self, cloudbuild_v1: Any) -> dict[Any, CIStatus]:
        """Map every Cloud Build status to a CIStatus (unknown ones fail closed)."""
        status_enum = cloudbuild_v1.Build.Status
        return {
            status_enum.SUCCESS: CIStatus.SUCCESS,
            status_enum.FAILURE: CIStatus.FAILURE,
            status_enum.INTERNAL_ERROR: CIStatus.FAILURE,
            status_enum.STATUS_UNKNOWN: CIStatus.FAILURE,
            status_enum.TIMEOUT: CIStatus.TIMEOUT,
            status_enum.EXPIRED: CIStatus.TIMEOUT,
            status_enum.CANCELLED: CIStatus.CANCELLED,
            status_enum.WORKING: CIStatus.RUNNING,
            status_enum.QUEUED: CIStatus.PENDING,
            status_enum.PENDING: CIStatus.PENDING,
        }

    async def get_status(self, build_id: str) -> CIResult:
        """Get the current status of a Cloud Build build."""
        from google.cloud import cloudbuild_v1  # type: ignore[attr-defined]

        client = self._get_client()
        build = await client.get_build(project_id=self._project, id=build_id)
        # Anything we do not recognise is terminal-failed rather than
        # forever-pending: a PENDING result would make callers poll for ever.
        status = self._status_map(cloudbuild_v1).get(build.status, CIStatus.FAILURE)
        logs_url = build.log_url if hasattr(build, "log_url") else None
        return CIResult(
            build_id=build_id,
            status=status,
            logs_url=logs_url,
            duration_seconds=_duration_seconds(build),
            error_message=str(getattr(build, "status_detail", "") or "") or None,
        )

    async def get_logs(self, build_id: str) -> str:
        """Return the logs URL for a Cloud Build build.

        Cloud Build streams logs to GCS/Cloud Logging rather than returning
        them inline, so this is a URL, not log text (see the CIProvider
        docstring for the cross-provider contract).
        """
        result = await self.get_status(build_id)
        return result.logs_url or f"https://console.cloud.google.com/cloud-build/builds/{build_id}"

    async def cancel(self, build_id: str) -> None:
        """Cancel a running Cloud Build build."""
        client = self._get_client()
        await client.cancel_build(project_id=self._project, id=build_id)


def _duration_seconds(build: Any) -> float | None:
    """Return the build wall-clock duration when both timestamps are present."""
    start = getattr(build, "start_time", None)
    finish = getattr(build, "finish_time", None)
    if start is None or finish is None:
        return None
    try:
        return float((finish - start).total_seconds())
    except (TypeError, AttributeError):
        return None
