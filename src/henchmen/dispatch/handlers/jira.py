"""Jira webhook handler."""

import logging
from typing import Any

from henchmen.config.settings import Settings
from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.providers.interfaces.message_broker import MessageBroker

logger = logging.getLogger(__name__)

HENCHMEN_TRANSITION_NAME = "Ready for Henchmen"


def _is_henchmen_transition(payload: dict[str, Any]) -> bool:
    """Return True if the Jira webhook represents a move into 'Ready for Henchmen'.

    Two payload shapes carry that information:

    * A workflow post-function webhook sends ``transition.transitionName``.
    * A standard ``jira:issue_updated`` event sends a ``changelog`` whose
      items include ``{"field": "status", "toString": "<status name>"}``.

    Both are accepted so the documented "Issue updated" webhook setup works.
    """
    transition = payload.get("transition") or {}
    if transition.get("transitionName") == HENCHMEN_TRANSITION_NAME:
        return True
    if transition.get("to_status") == HENCHMEN_TRANSITION_NAME:
        return True

    changelog = payload.get("changelog") or {}
    for item in changelog.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("field") in ("status", "Status") and item.get("toString") == HENCHMEN_TRANSITION_NAME:
            return True
    return False


async def handle_jira_webhook(
    payload: dict[str, Any],
    normalizer: TaskNormalizer,
    settings: Settings,
    broker: MessageBroker | None = None,
    dedup_key: str | None = None,
) -> dict[str, Any]:
    """Process Jira webhook (issue transition to 'Ready for Henchmen').

    Only processes issues that have been transitioned to the 'Ready for Henchmen' status.
    """
    if not _is_henchmen_transition(payload):
        return {"status": "ignored", "reason": "transition is not 'Ready for Henchmen'"}

    task = normalizer.from_jira(payload, settings)
    if not task.context.repo:
        logger.warning(
            "[jira] Issue %s has no repo (checked HENCHMEN_JIRA_REPO_FIELD and 'repo') "
            "and HENCHMEN_GITHUB_DEFAULT_REPO is unset",
            task.source_id,
        )
    msg_id = await normalizer.publish_task(task, settings, broker=broker, dedup_key=dedup_key)
    return {"task_id": task.id, "message_id": msg_id, "status": "dispatched"}
