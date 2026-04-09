"""Jira webhook handler."""

from typing import Any

from henchmen.config.settings import Settings
from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.providers.interfaces.message_broker import MessageBroker

HENCHMEN_TRANSITION_NAME = "Ready for Henchmen"


def _is_henchmen_transition(payload: dict[str, Any]) -> bool:
    """Return True if the Jira webhook represents a transition to 'Ready for Henchmen'."""
    transition = payload.get("transition", {})
    transition_name: str = transition.get("transitionName", "")
    return transition_name == HENCHMEN_TRANSITION_NAME


async def handle_jira_webhook(
    payload: dict[str, Any],
    normalizer: TaskNormalizer,
    settings: Settings,
    broker: MessageBroker | None = None,
) -> dict[str, Any]:
    """Process Jira webhook (issue transition to 'Ready for Henchmen').

    Only processes issues that have been transitioned to the 'Ready for Henchmen' status.
    """
    if not _is_henchmen_transition(payload):
        return {"status": "ignored", "reason": "transition is not 'Ready for Henchmen'"}

    task = normalizer.from_jira(payload)
    msg_id = await normalizer.publish_task(task, settings, broker=broker)
    return {"task_id": task.id, "message_id": msg_id, "status": "dispatched"}
