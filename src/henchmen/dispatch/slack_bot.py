"""Slack Socket Mode bot for Henchmen Dispatch.

Connects to Slack via WebSocket (no public URL needed).
Listens for @henchmen mentions and dispatches tasks.

The bot normally runs inside the Dispatch FastAPI process: ``lifespan`` calls
:func:`start_socket_mode`, which connects in the background and leaves uvicorn
serving the HTTP intake routes. ``python -m henchmen.dispatch.slack_bot`` still
works for running the bot on its own.

``slack_bolt`` is imported lazily so this module can be imported (and the
Dispatch app started) without the optional ``[slack]`` extra installed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from henchmen.config.settings import Settings, get_settings
from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.models.task import HenchmenTask

if TYPE_CHECKING:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

logger = logging.getLogger(__name__)

normalizer = TaskNormalizer()

# Slack API errors that mean "nothing to do", not "something is broken".
_BENIGN_JOIN_ERRORS = {"already_in_channel", "is_archived"}


def _bot_user_id(client: Any, cache: dict[str, str]) -> str:
    """Return the bot's own Slack user id, fetched once via ``auth.test``."""
    if "user_id" not in cache:
        try:
            cache["user_id"] = str(client.auth_test().get("user_id", ""))
        except Exception as exc:  # pragma: no cover - network failure path
            logger.warning("Could not resolve bot user id: %s", exc)
            cache["user_id"] = ""
    return cache["user_id"]


def create_slack_app(settings: Settings | None = None) -> App:
    """Create and configure the Slack Bolt app."""
    from slack_bolt import App

    settings = settings or get_settings()
    app = App(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret,
    )

    # One broker per app instead of one per message (a fresh GCP
    # PublisherClient per mention is expensive and leaks gRPC channels).
    from henchmen.providers.registry import ProviderRegistry

    broker = ProviderRegistry(settings).get_message_broker()
    identity: dict[str, str] = {}

    @app.event("app_mention")
    def handle_app_mention(event: dict[str, Any], say: Any, client: Any) -> None:
        """Handle @henchmen mentions in channels."""
        runtime_settings = get_settings()
        text = event.get("text", "")
        user = event.get("user", "unknown")
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts", event.get("ts", ""))

        logger.info("Received @henchmen mention from %s in %s", user, channel)

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
            "authorizations": [{"user_id": _bot_user_id(client, identity)}],
            "repo": runtime_settings.github_default_repo,
        }

        # Normalize and publish
        task = normalizer.from_slack(payload, runtime_settings)
        # Synchronous publish (we're in a sync handler on a Bolt worker thread)
        msg_id = _sync_publish(task, runtime_settings, broker=broker)

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

    return app


def _sync_publish(task: HenchmenTask, settings: Settings, broker: Any | None = None) -> str:
    """Synchronously publish a task via the configured MessageBroker.

    Bolt runs listeners on worker threads, which have no running (and on
    Python 3.12+ no implicit) event loop, so ``asyncio.run`` is used to drive
    the async broker rather than ``get_event_loop().run_until_complete``.
    """
    if broker is None:
        from henchmen.providers.registry import ProviderRegistry

        broker = ProviderRegistry(settings).get_message_broker()
    data = task.model_dump_json().encode("utf-8")
    result: str = asyncio.run(broker.publish(settings.pubsub_topic_task_intake, data, task_id=task.id))
    return result


def _join_notification_channel(app: App, channel: str) -> None:
    """Join ``channel`` if configured, tolerating the already-joined case."""
    if not channel:
        return
    try:
        app.client.conversations_join(channel=channel)
        logger.info("Joined Slack channel %s", channel)
    except Exception as exc:
        error = getattr(getattr(exc, "response", None), "data", {}) or {}
        code = error.get("error", "") if isinstance(error, dict) else ""
        if code in _BENIGN_JOIN_ERRORS:
            logger.info("Slack channel %s: %s", channel, code)
        elif code == "method_not_supported_for_channel_type":
            logger.warning("Slack channel %s is a private channel — invite the bot to it manually", channel)
        else:
            logger.warning("Could not join Slack channel %s: %s", channel, exc)


def start_socket_mode(settings: Settings) -> SocketModeHandler | None:
    """Connect the Slack Socket Mode client in the background.

    Returns ``None`` (after one explanatory log line) when Slack is not
    configured, so the Dispatch HTTP service starts regardless. The returned
    handler must be closed on shutdown.
    """
    if not settings.slack_bot_token or not settings.slack_app_token:
        logger.info(
            "Slack Socket Mode disabled: set HENCHMEN_SLACK_BOT_TOKEN and HENCHMEN_SLACK_APP_TOKEN to enable it"
        )
        return None

    try:
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        slack_app = create_slack_app(settings)
        handler = SocketModeHandler(slack_app, settings.slack_app_token)
        handler.connect()  # type: ignore[no-untyped-call]
    except Exception:
        logger.exception("Failed to start Slack Socket Mode")
        return None

    _join_notification_channel(slack_app, settings.slack_notification_channel)
    logger.info("Slack Socket Mode connected")
    return handler


def main() -> None:
    """Start the Slack bot in Socket Mode as a standalone process."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    # Force unbuffered output so Cloud Run captures logs
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

    settings = get_settings()
    if not settings.slack_app_token or not settings.slack_bot_token:
        logger.error("HENCHMEN_SLACK_APP_TOKEN and HENCHMEN_SLACK_BOT_TOKEN must both be set")
        sys.exit(1)

    try:
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        slack_app = create_slack_app(settings)
        handler = SocketModeHandler(slack_app, settings.slack_app_token)
        _join_notification_channel(slack_app, settings.slack_notification_channel)
        logger.info("Starting Henchmen Slack bot in Socket Mode...")
        handler.start()  # type: ignore[no-untyped-call]
    except Exception:
        logger.exception("FATAL: Slack bot crashed")
        raise


if __name__ == "__main__":
    main()
