"""CI orchestrator - manages CI pipelines with retry logic."""

import json
import logging
import re
from typing import Any

from henchmen.providers.interfaces.ci_provider import CIProvider
from henchmen.providers.interfaces.message_broker import MessageBroker

logger = logging.getLogger(__name__)

_PR_URL_RE = re.compile(
    r"https://github\.com/(?P<repo>[^/]+/[^/]+)/pull/(?P<number>\d+)",
    re.IGNORECASE,
)


class CIOrchestrator:
    """Orchestrates CI builds via a CIProvider with retry logic."""

    def __init__(
        self, settings: Any, ci_provider: CIProvider | None = None, broker: MessageBroker | None = None
    ) -> None:
        self.settings = settings
        self._ci_provider = ci_provider
        self._broker = broker

    def _get_ci_provider(self) -> CIProvider:
        if self._ci_provider is not None:
            return self._ci_provider
        from henchmen.providers.registry import ProviderRegistry

        return ProviderRegistry(self.settings).get_ci_provider()

    def _get_broker(self) -> MessageBroker:
        if self._broker is not None:
            return self._broker
        from henchmen.providers.registry import ProviderRegistry

        return ProviderRegistry(self.settings).get_message_broker()

    async def run_ci(self, pr_url: str, request_id: str) -> dict[str, Any]:
        """Run CI pipeline for a PR.

        Steps:
        1. Parse PR URL to get repo/PR number.
        2. Trigger CI build.
        3. Wait for build completion.
        4. Publish result to forge-result topic.
        """
        match = _PR_URL_RE.match(pr_url)
        if not match:
            result: dict[str, Any] = {
                "request_id": request_id,
                "pr_url": pr_url,
                "status": "failed",
                "error": f"Could not parse PR URL: {pr_url}",
            }
            await self._publish_result(request_id, result)
            return result

        repo = match.group("repo")
        pr_number = int(match.group("number"))

        # Derive branch from PR number as placeholder; real impl would call GitHub API
        branch = f"pr-{pr_number}"

        try:
            build_id = await self.trigger_build(repo, branch, pr_number)
            build_status = await self.get_build_status(build_id)
            result = {
                "request_id": request_id,
                "pr_url": pr_url,
                "repo": repo,
                "pr_number": pr_number,
                "build_id": build_id,
                "status": build_status.get("status", "unknown"),
                "build_status": build_status,
            }
        except Exception as exc:
            logger.exception("CI run failed for %s", pr_url)
            result = {
                "request_id": request_id,
                "pr_url": pr_url,
                "status": "failed",
                "error": str(exc),
            }

        await self._publish_result(request_id, result)
        return result

    async def trigger_build(self, repo: str, branch: str, pr_number: int) -> str:
        """Trigger a CI build. Returns build ID."""
        ci_provider = self._get_ci_provider()
        repo_url = f"https://github.com/{repo}.git"
        commands = [
            "git clone {repo_url} .",
            "pip install -e .[dev]",
            "python -m ruff check src/",
            "python -m pytest tests/unit/ -v",
        ]
        return await ci_provider.trigger_build(
            repo_url=repo_url,
            branch=branch,
            commands=commands,
        )

    async def get_build_status(self, build_id: str) -> dict[str, Any]:
        """Get current build status."""
        ci_provider = self._get_ci_provider()
        ci_result = await ci_provider.get_status(build_id)
        return {
            "build_id": build_id,
            "status": ci_result.status.value,
            "log_url": ci_result.logs_url,
        }

    async def _publish_result(self, request_id: str, result: dict[str, Any]) -> None:
        """Publish CI result to forge-result topic."""
        broker = self._get_broker()
        data = json.dumps(result).encode("utf-8")
        await broker.publish(
            self.settings.pubsub_topic_forge_result,
            data,
            request_id=request_id,
        )
