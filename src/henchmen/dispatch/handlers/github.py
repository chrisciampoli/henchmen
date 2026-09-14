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

# Only people with a write-ish relationship to the repository may launch an
# operative run from a comment. Without this gate any GitHub account can spend
# the deployment's LLM budget (and use its GitHub token) on a public repo.
ALLOWED_COMMENT_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# check_suite conclusions that mean "CI is red" for the purposes of the
# fix_tests feedback loop. ``cancelled``/``neutral``/``skipped`` are not
# failures; ``stale`` means a newer run supersedes this one.
CI_FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})


def _is_henchmen_issue_labeled(payload: dict[str, Any]) -> bool:
    """Return True if this is an issue labeled with 'henchmen'."""
    if payload.get("action") != "labeled":
        return False
    label: str = payload.get("label", {}).get("name", "")
    return bool(label == HENCHMEN_LABEL)


def _is_henchmen_comment(payload: dict[str, Any]) -> bool:
    """Return True if this is a newly created comment containing @henchmen.

    Covers both ``pull_request_review_comment`` (inline diff comments, which
    carry a top-level ``pull_request``) and ``issue_comment`` (the PR
    "Conversation" tab and plain issues, which carry ``issue``). ``edited``
    and ``deleted`` deliveries are ignored so fixing a typo in a comment does
    not dispatch a second operative.
    """
    if payload.get("action") != "created":
        return False
    comment = payload.get("comment") or {}
    body: str = comment.get("body", "") or ""
    if HENCHMEN_COMMENT_TRIGGER not in body:
        return False
    return "pull_request" in payload or "issue" in payload


def _is_authorized_commenter(payload: dict[str, Any]) -> bool:
    """Return True if the comment author may trigger a run (fail-closed)."""
    comment = payload.get("comment") or {}
    association = comment.get("author_association", "")
    return isinstance(association, str) and association.upper() in ALLOWED_COMMENT_AUTHOR_ASSOCIATIONS


async def handle_github_webhook(
    payload: dict[str, Any],
    normalizer: TaskNormalizer,
    settings: "Settings",
    broker: MessageBroker,
    dedup_key: str | None = None,
) -> dict[str, Any]:
    """Process GitHub webhook events.

    Handles:
    - Issue labeled 'henchmen'
    - PR review / issue comment containing '@henchmen fix this'
    - check_suite failure on henchmen/* branches (CI feedback loop)
    - Push to default branch (embedding update)
    """
    if _is_henchmen_issue_labeled(payload):
        task = normalizer.from_github(payload, settings)
        msg_id = await normalizer.publish_task(task, settings, broker=broker, dedup_key=dedup_key)
        return {"task_id": task.id, "message_id": msg_id, "status": "dispatched", "trigger": "issue_labeled"}

    if _is_henchmen_comment(payload):
        if not _is_authorized_commenter(payload):
            author = (payload.get("comment") or {}).get("user", {}).get("login", "unknown")
            logger.warning("[github] Ignoring @henchmen comment from unauthorized author %s", author)
            return {"status": "ignored", "reason": "unauthorized commenter"}
        task = normalizer.from_github(payload, settings)
        msg_id = await normalizer.publish_task(task, settings, broker=broker, dedup_key=dedup_key)
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
    """Return True if this is a red check_suite completion on a henchmen/* branch."""
    if payload.get("action") != "completed":
        return False
    suite = payload.get("check_suite", {})
    conclusion: str = suite.get("conclusion") or ""
    if conclusion.lower() not in CI_FAILURE_CONCLUSIONS:
        return False
    branch: str = suite.get("head_branch", "")
    return bool(branch.startswith("henchmen/"))


async def handle_ci_failure_webhook(
    payload: dict[str, Any],
    settings: "Settings",
    broker: MessageBroker,
) -> dict[str, Any]:
    """Handle a GitHub check_suite failure event on a Henchmen branch."""
    suite = payload.get("check_suite", {})
    repo = payload.get("repository", {}).get("full_name", "")
    branch = suite.get("head_branch", "")
    check_suite_id = suite.get("id", 0)
    head_sha = suite.get("head_sha", "")
    conclusion = suite.get("conclusion") or ""
    task_id_prefix = branch.replace("henchmen/", "", 1)

    data = json.dumps(
        {
            "task_id_prefix": task_id_prefix,
            "repo": repo,
            "branch": branch,
            "check_suite_id": check_suite_id,
            "head_sha": head_sha,
            "conclusion": conclusion,
        }
    ).encode("utf-8")
    await broker.publish(settings.pubsub_topic_ci_failure, data)

    return {
        "status": "ci_failure_dispatched",
        "task_id_prefix": task_id_prefix,
        "repo": repo,
        "check_suite_id": check_suite_id,
        "conclusion": conclusion,
    }


async def handle_push_embed(
    payload: dict[str, Any],
    settings: "Settings",
    broker: MessageBroker,
) -> dict[str, Any]:
    """Handle a GitHub push event by requesting an embedding update.

    Publishes a message to the embed-request Pub/Sub topic. The topic is the
    integration point for incremental RAG indexing; there is no subscriber in
    this repository yet, so the message is currently a no-op hook rather than
    a live pipeline.
    """
    repo = payload.get("repository", {}).get("full_name", "")
    commit_sha = payload.get("after", "")

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
