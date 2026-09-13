"""DossierBuilder – assembles context packages for operatives before dispatch."""

import asyncio
import logging
import shutil
import tempfile

import httpx

from henchmen.config.settings import Settings
from henchmen.dossier.convention_detector import RepoConventions, detect_conventions
from henchmen.dossier.rules import RuleFileLoader
from henchmen.models.dossier import CodeSearchResult, Dossier, RelatedIssue, RelatedPR, RuleFile
from henchmen.models.scheme import DossierRequirement
from henchmen.models.task import HenchmenTask
from henchmen.providers.interfaces.object_store import ObjectStore
from henchmen.utils.git import clone_repo

logger = logging.getLogger(__name__)


class DossierBuilder:
    """Builds context dossiers for operatives by prefetching relevant information."""

    def __init__(self, settings: Settings, object_store: ObjectStore | None = None) -> None:
        self.settings = settings
        self._object_store = object_store

    def _get_object_store(self) -> ObjectStore:
        """Lazy-create ObjectStore via ProviderRegistry if not injected."""
        if self._object_store is None:
            from henchmen.providers.registry import ProviderRegistry

            self._object_store = ProviderRegistry(self.settings).get_object_store()
        return self._object_store

    async def build(self, task: HenchmenTask, requirement: DossierRequirement) -> Dossier:
        """Build a dossier based on task and requirements.

        Every fetch step degrades gracefully: a missing GitHub token, a clone
        failure or an API error yields empty context rather than an exception,
        so a partial dossier is always better than none.
        """
        dossier = Dossier(task_id=task.id)

        if requirement.fetch_files:
            dossier.relevant_files = await self._fetch_relevant_files(task)

        # One shallow clone serves both rule-file discovery and convention
        # detection (this used to clone the repo twice per task).
        dossier.rule_files, dossier.conventions = await self._scan_repo(task, fetch_rules=requirement.fetch_rules)

        if requirement.fetch_related_prs:
            dossier.related_prs = await self._fetch_related_prs(task)

        if requirement.fetch_related_issues:
            dossier.related_issues = await self._fetch_related_issues(task)

        if requirement.code_search_symbols:
            dossier.code_search_results = await self._code_search(task, requirement.code_search_symbols)

        dossier.artifact_uri = await self.upload_artifact(dossier)
        return dossier

    # ------------------------------------------------------------------
    # Private fetch methods
    # ------------------------------------------------------------------

    async def _scan_repo(self, task: HenchmenTask, fetch_rules: bool) -> tuple[list[RuleFile], RepoConventions | None]:
        """Clone the repo once and extract rule files plus project conventions.

        Returns ``([], None)`` on any failure so the dossier pipeline is never
        blocked.
        """
        repo = task.context.repo
        if not repo:
            return [], None

        github_token = self.settings.github_token
        branch = task.context.branch or "main"

        tmp_dir = tempfile.mkdtemp(prefix="henchmen-dossier-")
        try:
            try:
                await clone_repo(repo, branch, tmp_dir, token=github_token or None, depth=1)
            except RuntimeError as exc:
                logger.warning("Failed to clone repo for dossier scan: %s", exc)
                return [], None

            rule_files: list[RuleFile] = []
            if fetch_rules:
                rule_files = await RuleFileLoader.load_rules(tmp_dir, self._rule_target_paths(task))

            # detect_conventions walks the tree synchronously — keep it off
            # the event loop.
            conventions = await asyncio.to_thread(detect_conventions, tmp_dir)
            return rule_files, conventions
        except Exception as exc:
            logger.warning("Repo scan for dossier failed: %s", exc)
            return [], None
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _rule_target_paths(task: HenchmenTask) -> list[str] | None:
        """Paths touched by the task, used to pick up directory-scoped rules."""
        if not task.context.pr_diff:
            return None
        paths = [
            line[6:].strip()
            for line in task.context.pr_diff.splitlines()
            if line.startswith("+++ b/") and line[6:].strip() != "/dev/null"
        ]
        return paths or None

    async def _fetch_relevant_files(self, task: HenchmenTask) -> list[str]:
        """Identify file paths relevant to the task from context."""
        # The task's context may carry pr_diff or issue_fields with file hints.
        # For now, return files from pr_diff if available, or an empty list.
        relevant: list[str] = []

        if task.context.pr_diff:
            for line in task.context.pr_diff.splitlines():
                if line.startswith("--- a/") or line.startswith("+++ b/"):
                    path = line[6:].strip()
                    if path and path != "/dev/null" and path not in relevant:
                        relevant.append(path)

        return relevant

    async def _fetch_related_prs(self, task: HenchmenTask) -> list[RelatedPR]:
        """Fetch related pull requests from GitHub."""
        try:
            repo = task.context.repo
            if not repo:
                return []

            github_token = self.settings.github_token
            if not github_token:
                logger.warning("No GitHub token (HENCHMEN_GITHUB_TOKEN); cannot fetch related PRs")
                return []

            query = task.title
            url = "https://api.github.com/search/issues"
            params: dict[str, str | int] = {"q": f"{query} repo:{repo} is:pr", "per_page": 5}
            headers = {"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json"}

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code != 200:
                    logger.warning("GitHub PR search returned %d", resp.status_code)
                    return []
                items = resp.json().get("items", [])
                return [
                    RelatedPR(
                        number=item.get("number", 0),
                        title=item.get("title", ""),
                        url=item.get("html_url", ""),
                        state=item.get("state", "unknown"),
                    )
                    for item in items
                ]
        except Exception as exc:
            logger.warning("Failed to fetch related PRs: %s", exc)
            return []

    async def _fetch_related_issues(self, task: HenchmenTask) -> list[RelatedIssue]:
        """Fetch related issues from GitHub or Jira."""
        try:
            repo = task.context.repo
            if not repo:
                return []

            github_token = self.settings.github_token
            if not github_token:
                logger.warning("No GitHub token (HENCHMEN_GITHUB_TOKEN); cannot fetch related issues")
                return []

            url = "https://api.github.com/search/issues"
            params: dict[str, str | int] = {"q": f"{task.title} repo:{repo} is:issue", "per_page": 5}
            headers = {"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json"}

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code != 200:
                    logger.warning("GitHub issue search returned %d", resp.status_code)
                    return []
                items = resp.json().get("items", [])
                return [
                    RelatedIssue(
                        number=item.get("number", 0),
                        title=item.get("title", ""),
                        url=item.get("html_url", ""),
                        state=item.get("state", "unknown"),
                    )
                    for item in items
                ]
        except Exception as exc:
            logger.warning("Failed to fetch related issues: %s", exc)
            return []

    async def _code_search(self, task: HenchmenTask, symbols: list[str]) -> list[CodeSearchResult]:
        """Perform code search for symbol names in GitHub."""
        try:
            repo = task.context.repo
            if not repo:
                return []

            github_token = self.settings.github_token
            if not github_token:
                logger.warning("No GitHub token (HENCHMEN_GITHUB_TOKEN); cannot perform code search")
                return []

            results: list[CodeSearchResult] = []
            headers = {"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json"}

            async with httpx.AsyncClient(timeout=15.0) as client:
                for symbol in symbols[:10]:  # Limit to avoid rate limits
                    url = "https://api.github.com/search/code"
                    params: dict[str, str | int] = {"q": f"{symbol} repo:{repo}", "per_page": 3}
                    resp = await client.get(url, params=params, headers=headers)
                    if resp.status_code == 200:
                        items = resp.json().get("items", [])
                        for item in items:
                            results.append(
                                CodeSearchResult(
                                    file_path=item.get("path", ""),
                                    matches=[symbol],
                                    context=f"Found in {repo} — {item.get('html_url', '')}",
                                )
                            )

            return results
        except Exception as exc:
            logger.warning("Code search failed: %s", exc)
            return []

    def _artifact_scheme(self) -> str:
        """URI scheme for dossier artifacts, matching the object-store provider."""
        name = (self.settings.object_store_provider or self.settings.provider or "").lower()
        return "s3" if name == "aws" else "gs"

    async def upload_artifact(self, dossier: Dossier) -> str | None:
        """Serialise the dossier to JSON and upload via ObjectStore.

        Returns the artifact URI, or ``None`` when no bucket is configured or
        the upload failed. Upload failures must never discard the context that
        was already gathered, so they are logged rather than raised.
        """
        bucket_name = self.settings.gcs_bucket_dossier
        if not bucket_name:
            logger.info("No dossier bucket configured (HENCHMEN_GCS_BUCKET_DOSSIER); skipping dossier upload")
            return None

        blob_key = f"dossiers/{dossier.task_id}/dossier.json"
        data = dossier.model_dump_json(indent=2).encode("utf-8")

        try:
            object_store = self._get_object_store()
            await object_store.put(bucket_name, blob_key, data)
        except Exception as exc:
            logger.warning("Dossier upload failed (non-fatal): %s", exc)
            return None

        uri = f"{self._artifact_scheme()}://{bucket_name}/{blob_key}"
        logger.info("Dossier uploaded to %s", uri)
        return uri
