"""Slack event handler."""

import logging
from typing import Any

from henchmen.config.settings import Settings
from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.providers.interfaces.message_broker import MessageBroker

logger = logging.getLogger(__name__)

# Plain-text markers a human may type. Slack itself sends ``<@U0BOT123>``;
# that form is matched against the bot's own user id (see ``_bot_user_id``).
HENCHMEN_MENTION_MARKERS = ("@henchmen", "<@henchmen>")


def _bot_user_id(payload: dict[str, Any]) -> str:
    """Return the bot's Slack user id from the Events API envelope, if present.

    Slack puts the installed app's bot user id in ``authorizations[0].user_id``
    on every event delivery, which lets us recognise a real ``<@U0BOT123>``
    mention inside a plain ``message`` event.
    """
    authorizations = payload.get("authorizations")
    if isinstance(authorizations, list) and authorizations:
        first = authorizations[0]
        if isinstance(first, dict):
            user_id = first.get("user_id")
            if isinstance(user_id, str):
                return user_id
    return ""


async def handle_slack_event(
    payload: dict[str, Any],
    normalizer: TaskNormalizer,
    settings: Settings,
    broker: MessageBroker,
    dedup_key: str | None = None,
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
    bot_user_id = _bot_user_id(payload)
    if bot_user_id and f"<@{bot_user_id}>" in text:
        has_mention = True

    if not (is_app_mention or has_mention):
        return {"status": "ignored", "reason": "not a henchmen mention"}

    task = normalizer.from_slack(payload, settings)
    if not task.context.repo:
        logger.warning("[slack] No repo on the event and HENCHMEN_GITHUB_DEFAULT_REPO is unset")
    msg_id = await normalizer.publish_task(task, settings, broker=broker, dedup_key=dedup_key)
    return {"task_id": task.id, "message_id": msg_id, "status": "dispatched"}
