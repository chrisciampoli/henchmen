"""Slack Socket Mode bot for Henchmen Dispatch.

Connects to Slack via WebSocket (no public URL needed).
Listens for @henchmen mentions and dispatches tasks.
"""

import logging
import os
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from henchmen.config.settings import Settings, get_settings
from henchmen.dispatch.normalizer import TaskNormalizer

logger = logging.getLogger(__name__)

normalizer = TaskNormalizer()


def create_slack_app() -> App:
    """Create and configure the Slack Bolt app."""
    app = App(
        token=os.environ.get("SLACK_BOT_TOKEN", ""),
        signing_secret=os.environ.get("SLACK_SIGNING_SECRET", ""),
    )

    @app.event("app_mention")
    def handle_app_mention(event: dict[str, Any], say: Any, client: Any) -> None:
        """Handle @henchmen mentions in channels."""
        settings = get_settings()
        text = event.get("text", "")
        user = event.get("user", "unknown")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts", event.get("ts", ""))

        logger.info("Received @henchmen mention from %s in %s: %s", user, channel, text)

        # Gather thread context if in a thread
        thread_messages = []
        if thread_ts:
            try:
                result = client.conversations_replies(channel=channel, ts=thread_ts, limit=20)
                thread_messages = [msg.get("text", "") for msg in result.get("messages", [])]
            except Exception as exc:
                logger.warning("Failed to fetch thread: %s", exc)

        # Build the Slack payload for the normalizer
        payload = {
            "event": {
                "type": "app_mention",
                "user": user,
                "text": text,
                "channel": channel,
                "ts": event.get("ts", ""),
                "thread_ts": thread_ts,
                "thread_messages": thread_messages,
            },
            "repo": settings.github_default_repo,
        }

        # Normalize and publish
        task = normalizer.from_slack(payload)
        # Synchronous publish (we're in a sync handler)
        msg_id = _sync_publish(task, settings)

        # Reply in thread
        say(
            text=f"Got it! I've created task `{task.id}` for this.\n"
            f"Scheme will be selected and an operative dispatched shortly.",
            thread_ts=thread_ts,
        )
        logger.info("Task %s dispatched (msg_id=%s)", task.id, msg_id)

    @app.event("message")
    def handle_message(event: dict[str, Any]) -> None:
        """Ignore regular messages (required to prevent warnings)."""
        pass

    return app


def _sync_publish(task: Any, settings: Settings) -> str:
    """Synchronously publish a task via the configured MessageBroker."""
    import asyncio

    from henchmen.providers.registry import ProviderRegistry

    broker = ProviderRegistry(settings).get_message_broker()
    data = task.model_dump_json().encode("utf-8")
    result: str = asyncio.get_event_loop().run_until_complete(
        broker.publish(settings.pubsub_topic_task_intake, data, task_id=task.id)
    )
    return result


def main() -> None:
    """Start the Slack bot in Socket Mode."""
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    # Force unbuffered output so Cloud Run captures logs
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if not app_token:
        logger.error("SLACK_APP_TOKEN not set")
        return

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        logger.error("SLACK_BOT_TOKEN not set")
        return

    logger.info("SLACK_APP_TOKEN present: %s (length: %d)", bool(app_token), len(app_token))
    logger.info("SLACK_BOT_TOKEN present: %s (length: %d)", bool(bot_token), len(bot_token))

    try:
        slack_app = create_slack_app()
        handler = SocketModeHandler(slack_app, app_token)
        logger.info("Starting Henchmen Slack bot in Socket Mode...")
        handler.start()  # type: ignore[no-untyped-call]
    except Exception:
        logger.exception("FATAL: Slack bot crashed")
        raise


if __name__ == "__main__":
    main()
