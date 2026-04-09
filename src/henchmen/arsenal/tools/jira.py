"""Jira tools - issue status transitions, comments, and lookups."""

import asyncio
import os
from typing import Any

from henchmen.arsenal.registry import tool


def _get_jira_client() -> Any:
    """Return an authenticated Jira client using environment variables."""
    from jira import JIRA

    server = os.environ.get("JIRA_SERVER", "")
    email = os.environ.get("JIRA_EMAIL", "")
    api_token = os.environ.get("JIRA_API_TOKEN", "")
    if not server:
        raise ValueError("JIRA_SERVER environment variable is not set")
    return JIRA(server=server, basic_auth=(email, api_token))


@tool(
    name="update_issue_status",
    category="jira",
    description="Transition a Jira issue to a new status by status name.",
)
async def update_issue_status(issue_key: str, status: str) -> dict[str, Any]:
    """Update a Jira issue's status using an available transition."""

    def _sync() -> dict[str, Any]:
        client = _get_jira_client()
        transitions = client.transitions(issue_key)
        transition_id = None
        for t in transitions:
            if t["name"].lower() == status.lower():
                transition_id = t["id"]
                break
        if transition_id is None:
            available = [t["name"] for t in transitions]
            return {
                "error": f"No transition named '{status}' found. Available: {available}",
                "issue_key": issue_key,
            }
        client.transition_issue(issue_key, transition_id)
        return {"success": True, "issue_key": issue_key, "new_status": status}

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="add_comment",
    category="jira",
    description="Add a comment to a Jira issue.",
)
async def add_comment(issue_key: str, body: str) -> dict[str, Any]:
    """Post a comment on the specified Jira issue."""

    def _sync() -> dict[str, Any]:
        client = _get_jira_client()
        comment = client.add_comment(issue_key, body)
        return {"success": True, "issue_key": issue_key, "comment_id": comment.id}

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="transition_issue",
    category="jira",
    description="Transition a Jira issue using an explicit transition name.",
)
async def transition_issue(issue_key: str, transition_name: str) -> dict[str, Any]:
    """Perform a named workflow transition on a Jira issue."""

    def _sync() -> dict[str, Any]:
        client = _get_jira_client()
        transitions = client.transitions(issue_key)
        transition_id = None
        for t in transitions:
            if t["name"].lower() == transition_name.lower():
                transition_id = t["id"]
                break
        if transition_id is None:
            available = [t["name"] for t in transitions]
            return {
                "error": f"Transition '{transition_name}' not found. Available: {available}",
                "issue_key": issue_key,
            }
        client.transition_issue(issue_key, transition_id)
        return {"success": True, "issue_key": issue_key, "transition": transition_name}

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="fetch_issue",
    category="jira",
    description="Fetch the details of a Jira issue by its key.",
)
async def fetch_issue(issue_key: str) -> dict[str, Any]:
    """Return the fields of a Jira issue."""

    def _sync() -> dict[str, Any]:
        client = _get_jira_client()
        issue = client.issue(issue_key)
        fields = issue.fields
        return {
            "success": True,
            "key": issue.key,
            "summary": fields.summary,
            "status": fields.status.name,
            "assignee": fields.assignee.displayName if fields.assignee else None,
            "reporter": fields.reporter.displayName if fields.reporter else None,
            "description": fields.description,
            "issue_type": fields.issuetype.name,
            "priority": fields.priority.name if fields.priority else None,
            "labels": list(fields.labels),
        }

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        return {"error": str(exc)}
