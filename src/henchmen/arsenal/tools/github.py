"""GitHub tools - pull requests, issues, labels, assignments."""

import asyncio
import os
from typing import Any

from henchmen.arsenal.registry import tool


def _get_github_client() -> Any:
    """Return an authenticated PyGithub client using GITHUB_TOKEN env var."""
    import github

    token = os.environ.get("GITHUB_TOKEN", "")
    return github.Github(token)


def _get_repo(client: Any) -> Any:
    """Return the repo object from GITHUB_REPO env var (format: owner/name)."""
    repo_name = os.environ.get("GITHUB_REPO", "")
    if not repo_name:
        raise ValueError("GITHUB_REPO environment variable is not set")
    return client.get_repo(repo_name)


@tool(
    name="create_pull_request",
    category="github",
    description="Create a GitHub pull request from head_branch into base_branch.",
)
async def create_pull_request(
    title: str,
    body: str,
    head_branch: str,
    base_branch: str = "main",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Open a new pull request on GitHub."""

    def _sync() -> dict[str, Any]:
        client = _get_github_client()
        repo = _get_repo(client)
        pr = repo.create_pull(title=title, body=body, head=head_branch, base=base_branch)
        if labels:
            pr.set_labels(*labels)
        return {
            "success": True,
            "pr_number": pr.number,
            "url": pr.html_url,
            "title": pr.title,
        }

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="comment_on_pr",
    category="github",
    description="Add a comment to a GitHub pull request.",
)
async def comment_on_pr(pr_number: int, body: str) -> dict[str, Any]:
    """Post a comment on the specified PR."""

    def _sync() -> dict[str, Any]:
        client = _get_github_client()
        repo = _get_repo(client)
        pr = repo.get_pull(pr_number)
        comment = pr.create_issue_comment(body)
        return {"success": True, "comment_id": comment.id, "pr_number": pr_number}

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="label_issue",
    category="github",
    description="Add labels to a GitHub issue.",
)
async def label_issue(issue_number: int, labels: list[str]) -> dict[str, Any]:
    """Apply labels to the specified issue."""

    def _sync() -> dict[str, Any]:
        client = _get_github_client()
        repo = _get_repo(client)
        issue = repo.get_issue(issue_number)
        issue.add_to_labels(*labels)
        return {"success": True, "issue_number": issue_number, "labels": labels}

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="assign_issue",
    category="github",
    description="Assign users to a GitHub issue.",
)
async def assign_issue(issue_number: int, assignees: list[str]) -> dict[str, Any]:
    """Assign one or more users to the specified issue."""

    def _sync() -> dict[str, Any]:
        client = _get_github_client()
        repo = _get_repo(client)
        issue = repo.get_issue(issue_number)
        issue.add_to_assignees(*assignees)
        return {"success": True, "issue_number": issue_number, "assignees": assignees}

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="fetch_issues",
    category="github",
    description="Fetch GitHub issues filtered by state and optional labels.",
)
async def fetch_issues(
    state: str = "open",
    labels: list[str] | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Return a list of issues matching the given filters."""

    def _sync() -> dict[str, Any]:
        client = _get_github_client()
        repo = _get_repo(client)
        kwargs: dict[str, Any] = {"state": state}
        if labels:
            kwargs["labels"] = [repo.get_label(lbl) for lbl in labels]
        issues_paged = repo.get_issues(**kwargs)
        issues = []
        for issue in issues_paged[:max_results]:
            issues.append(
                {
                    "number": issue.number,
                    "title": issue.title,
                    "state": issue.state,
                    "url": issue.html_url,
                    "labels": [lbl.name for lbl in issue.labels],
                    "assignees": [a.login for a in issue.assignees],
                }
            )
        return {"success": True, "issues": issues, "count": len(issues)}

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        return {"error": str(exc)}
