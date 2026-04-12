"""DossierBuilder – assembles context packages for operatives before dispatch."""

import asyncio
import logging
import os
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

        Under ``HENCHMEN_PROVIDER=local``, RAG-dependent steps are skipped
        entirely (see L7 fix). This lets developers run the dossier pipeline
        without a Vertex AI RAG Engine corpus, falling back to grep-only
        context. Steps that only require GitHub API access (related PRs,
        related issues, code search) still run when a GitHub token is present.
        """
        dossier = Dossier(task_id=task.id)
        local_mode = (self.settings.provider or "").lower() == "local"
        if local_mode:
            logger.info(
                "DossierBuilder running in local mode — Vertex AI RAG Engine "
                "steps are skipped; context will be grep-only."
            )

        if requirement.fetch_files:
            dossier.relevant_files = await self._fetch_relevant_files(task)

        if requirement.fetch_rules:
            dossier.rule_files = await self._fetch_rule_files(task)

        if requirement.fetch_related_prs:
            dossier.related_prs = await self._fetch_related_prs(task)

        if requirement.fetch_related_issues:
            dossier.related_issues = await self._fetch_related_issues(task)

        if requirement.code_search_symbols and not local_mode:
            dossier.code_search_results = await self._code_search(task, requirement.code_search_symbols)
        elif requirement.code_search_symbols and local_mode:
            logger.info("Skipping code_search in local mode; grep-based context only.")

        # Detect project conventions (runs in its own shallow clone if rules
        # were not already fetched; lightweight — only reads config files)
        dossier.conventions = await self._detect_conventions(task)

        # Serialize and upload to GCS
        dossier.artifact_uri = await self._upload_artifact(dossier)
        return dossier

    # ------------------------------------------------------------------
    # Private fetch methods
    # ------------------------------------------------------------------

    async def _detect_conventions(self, task: HenchmenTask) -> RepoConventions | None:
        """Detect project conventions from a shallow clone of the repo.

        Uses the same clone-and-scan pattern as ``_fetch_rule_files``. Returns
        ``None`` on any failure so the dossier pipeline is never blocked.
        """
        repo = task.context.repo
        if not repo:
            return None

        github_token = _get_github_token(self.settings)
        branch = task.context.branch or "main"

        tmp_dir = tempfile.mkdtemp(prefix="henchmen-conventions-")
        try:
            try:
                await clone_repo(
                    repo,
                    branch,
                    tmp_dir,
                    token=github_token or None,
                    depth=1,
                )
            except RuntimeError as exc:
                logger.warning("Failed to clone repo for convention detection: %s", exc)
                return None

            return detect_conventions(tmp_dir)
        except Exception as exc:
            logger.warning("Convention detection failed: %s", exc)
            return None
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

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

    async def _fetch_rule_files(self, task: HenchmenTask) -> list[RuleFile]:
        """Fetch rule files from a shallow clone of the repo."""
        repo = task.context.repo
        if not repo:
            return []

        github_token = _get_github_token(self.settings)
        branch = task.context.branch or "main"

        tmp_dir = tempfile.mkdtemp(prefix="henchmen-rules-")
        try:
            try:
                await clone_repo(
                    repo,
                    branch,
                    tmp_dir,
                    token=github_token or None,
                    depth=1,
                    no_checkout=True,
                )
            except RuntimeError as exc:
                logger.warning("Failed to clone repo for rules: %s", exc)
                return []

            # Sparse-checkout only rule files
            await (
                await asyncio.create_subprocess_exec(
                    "git",
                    "-C",
                    tmp_dir,
                    "checkout",
                    branch,
                    "--",
                    *RuleFileLoader.RULE_FILE_NAMES,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            ).communicate()

            target_paths: list[str] | None = None
            if task.context.pr_diff:
                target_paths = [
                    line[6:].strip()
                    for line in task.context.pr_diff.splitlines()
                    if line.startswith("+++ b/") and line[6:].strip() != "/dev/null"
                ]

            return await RuleFileLoader.load_rules(tmp_dir, target_paths)
        except Exception as exc:
            logger.warning("Error fetching rule files: %s", exc)
            return []
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _fetch_related_prs(self, task: HenchmenTask) -> list[RelatedPR]:
        """Fetch related pull requests from GitHub."""
        try:
            repo = task.context.repo
            if not repo:
                return []

            github_token = _get_github_token(self.settings)
            if not github_token:
                logger.warning("No GitHub token; cannot fetch related PRs")
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

            github_token = _get_github_token(self.settings)
            if not github_token:
                logger.warning("No GitHub token; cannot fetch related issues")
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

            github_token = _get_github_token(self.settings)
            if not github_token:
                logger.warning("No GitHub token; cannot perform code search")
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

    async def _upload_artifact(self, dossier: Dossier) -> str:
        """Serialise the dossier to JSON and upload via ObjectStore. Returns the GCS URI."""
        bucket_name = self.settings.gcs_bucket_dossier
        if not bucket_name:
            logger.warning("gcs_bucket_dossier not configured; skipping upload")
            return ""

        blob_key = f"dossiers/{dossier.task_id}/dossier.json"
        data = dossier.model_dump_json(indent=2).encode("utf-8")

        object_store = self._get_object_store()
        await object_store.put(bucket_name, blob_key, data)

        uri = f"gs://{bucket_name}/{blob_key}"
        logger.info("Dossier uploaded to %s", uri)
        return uri


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_github_token(settings: Settings) -> str:
    """Return a plain GitHub token from settings (for unauthenticated fallback)."""
    # In production this would be fetched from Secret Manager.
    # Here we accept an optional plain env var for testing convenience.
    return os.environ.get("GITHUB_TOKEN", "")
