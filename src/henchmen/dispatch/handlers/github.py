"""GitHub webhook handler."""

import json
import logging
from typing import TYPE_CHECKING, Any

from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.providers.interfaces.message_broker import MessageBroker

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

HENCHMEN_LABEL = "henchmen"
HENCHMEN_COMMENT_TRIGGER = "@henchmen"


def _is_henchmen_issue_labeled(payload: dict[str, Any]) -> bool:
    """Return True if this is an issue labeled with 'henchmen'."""
    if payload.get("action") != "labeled":
        return False
    label: str = payload.get("label", {}).get("name", "")
    return bool(label == HENCHMEN_LABEL)


def _is_henchmen_pr_comment(payload: dict[str, Any]) -> bool:
    """Return True if this is a PR review comment containing @henchmen."""
    comment = payload.get("comment", {})
    body: str = comment.get("body", "")
    return HENCHMEN_COMMENT_TRIGGER in body and "pull_request" in payload


async def handle_github_webhook(
    payload: dict[str, Any],
    normalizer: TaskNormalizer,
    settings: "Settings",
    broker: MessageBroker | None = None,
) -> dict[str, Any]:
    """Process GitHub webhook events.

    Handles:
    - Issue labeled 'henchmen'
    - PR review comment containing '@henchmen fix this'
    - check_suite failure on henchmen/* branches (CI feedback loop)
    - Push to default branch (embedding update)
    """
    if _is_henchmen_issue_labeled(payload):
        task = normalizer.from_github(payload)
        msg_id = await normalizer.publish_task(task, settings, broker=broker)
        return {"task_id": task.id, "message_id": msg_id, "status": "dispatched", "trigger": "issue_labeled"}

    if _is_henchmen_pr_comment(payload):
        task = normalizer.from_github(payload)
        msg_id = await normalizer.publish_task(task, settings, broker=broker)
        return {"task_id": task.id, "message_id": msg_id, "status": "dispatched", "trigger": "pr_comment"}

    if _is_ci_failure_on_henchmen_branch(payload):
        return await handle_ci_failure_webhook(payload, settings, broker=broker)

    if _is_push_to_default_branch(payload):
        return await handle_push_embed(payload, settings, broker=broker)

    return {"status": "ignored", "reason": "no matching trigger"}


def _is_push_to_default_branch(payload: dict[str, Any]) -> bool:
    """Return True if this is a push event to the repo's default branch."""
    ref: str = payload.get("ref", "")
    repo_info = payload.get("repository", {})
    default_branch: str = repo_info.get("default_branch", "main")
    return bool(ref == f"refs/heads/{default_branch}")


def _is_ci_failure_on_henchmen_branch(payload: dict[str, Any]) -> bool:
    """Return True if this is a check_suite completion with failure on a henchmen/* branch."""
    if payload.get("action") != "completed":
        return False
    suite = payload.get("check_suite", {})
    if suite.get("conclusion") != "failure":
        return False
    branch: str = suite.get("head_branch", "")
    return bool(branch.startswith("henchmen/"))


async def handle_ci_failure_webhook(
    payload: dict[str, Any],
    settings: "Settings",
    broker: MessageBroker | None = None,
) -> dict[str, Any]:
    """Handle a GitHub check_suite failure event on a Henchmen branch."""
    suite = payload.get("check_suite", {})
    repo = payload.get("repository", {}).get("full_name", "")
    branch = suite.get("head_branch", "")
    check_suite_id = suite.get("id", 0)
    head_sha = suite.get("head_sha", "")
    task_id_prefix = branch.replace("henchmen/", "", 1)

    if broker is None:
        from henchmen.providers.registry import ProviderRegistry

        broker = ProviderRegistry(settings).get_message_broker()

    data = json.dumps(
        {
            "task_id_prefix": task_id_prefix,
            "repo": repo,
            "branch": branch,
            "check_suite_id": check_suite_id,
            "head_sha": head_sha,
        }
    ).encode("utf-8")
    await broker.publish(settings.pubsub_topic_ci_failure, data)

    return {
        "status": "ci_failure_dispatched",
        "task_id_prefix": task_id_prefix,
        "repo": repo,
        "check_suite_id": check_suite_id,
    }


async def handle_push_embed(
    payload: dict[str, Any],
    settings: "Settings",
    broker: MessageBroker | None = None,
) -> dict[str, Any]:
    """Handle a GitHub push event by requesting an embedding update.

    Publishes a message to the embed-request Pub/Sub topic so the
    embedding pipeline can incrementally update the Pinecone index.
    """
    repo = payload.get("repository", {}).get("full_name", "")
    commit_sha = payload.get("after", "")

    if broker is None:
        from henchmen.providers.registry import ProviderRegistry

        broker = ProviderRegistry(settings).get_message_broker()

    data = json.dumps(
        {
            "repo": repo,
            "commit_sha": commit_sha,
            "mode": "incremental",
        }
    ).encode("utf-8")

    await broker.publish(settings.pubsub_topic_embed_request, data)

    return {
        "status": "embed_requested",
        "repo": repo,
        "commit_sha": commit_sha,
    }
