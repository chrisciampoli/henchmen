"""Slack tools - post messages, thread replies, file uploads."""

import asyncio
import os
from typing import Any

from henchmen.arsenal.registry import tool


def _get_slack_client() -> Any:
    """Return an authenticated Slack WebClient using SLACK_BOT_TOKEN env var."""
    from slack_sdk import WebClient

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise ValueError("SLACK_BOT_TOKEN environment variable is not set")
    return WebClient(token=token)


@tool(
    name="post_message",
    category="slack",
    description="Post a message to a Slack channel. Optionally reply to an existing thread.",
)
async def post_message(channel: str, text: str, thread_ts: str | None = None) -> dict[str, Any]:
    """Post a message to a Slack channel, optionally in a thread."""

    def _sync() -> dict[str, Any]:
        client = _get_slack_client()
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        response = client.chat_postMessage(**kwargs)
        return {
            "success": True,
            "channel": channel,
            "ts": response["ts"],
            "thread_ts": response.get("message", {}).get("thread_ts"),
        }

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="thread_reply",
    category="slack",
    description="Reply to an existing Slack thread.",
)
async def thread_reply(channel: str, thread_ts: str, text: str) -> dict[str, Any]:
    """Post a reply in an existing Slack thread."""

    def _sync() -> dict[str, Any]:
        client = _get_slack_client()
        response = client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
        return {
            "success": True,
            "channel": channel,
            "ts": response["ts"],
            "thread_ts": thread_ts,
        }

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="upload_file",
    category="slack",
    description="Upload a file to a Slack channel.",
)
async def upload_file(channel: str, file_path: str, title: str | None = None) -> dict[str, Any]:
    """Upload a local file to a Slack channel."""

    def _sync() -> dict[str, Any]:
        client = _get_slack_client()
        kwargs: dict[str, Any] = {"channels": channel, "file": file_path}
        if title:
            kwargs["title"] = title
        response = client.files_upload_v2(**kwargs)
        file_info = response.get("file", {})
        return {
            "success": True,
            "channel": channel,
            "file_id": file_info.get("id"),
            "file_name": file_info.get("name"),
            "permalink": file_info.get("permalink"),
        }

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        return {"error": str(exc)}
