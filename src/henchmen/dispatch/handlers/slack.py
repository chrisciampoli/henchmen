"""Slack event handler."""

from typing import Any

from henchmen.config.settings import Settings
from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.providers.interfaces.message_broker import MessageBroker

HENCHMEN_MENTION_MARKERS = ("@henchmen", "<@henchmen>")


async def handle_slack_event(
    payload: dict[str, Any],
    normalizer: TaskNormalizer,
    settings: Settings,
    broker: MessageBroker | None = None,
) -> dict[str, Any]:
    """Process Slack event (app_mention in thread).

    Expects a Slack Events API envelope with an inner ``event`` key.
    Only processes ``app_mention`` events that contain a @henchmen mention.
    """
    event = payload.get("event", payload)
    event_type = event.get("type", "")
    text = event.get("text", "")

    # Only handle app_mention events or messages that @-mention henchmen
    is_app_mention = event_type == "app_mention"
    has_mention = any(marker in text for marker in HENCHMEN_MENTION_MARKERS)

    if not (is_app_mention or has_mention):
        return {"status": "ignored", "reason": "not a henchmen mention"}

    task = normalizer.from_slack(payload)
    msg_id = await normalizer.publish_task(task, settings, broker=broker)
    return {"task_id": task.id, "message_id": msg_id, "status": "dispatched"}
