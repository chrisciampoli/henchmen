"""Unit tests for henchmen chat — interactive task builder CLI."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from henchmen.cli.chat import (
    _build_system_prompt,
    _call_ollama,
    _check_ollama,
    _dispatch_task,
    _parse_task_block,
    _read_multiline_input,
)
from henchmen.config.settings import Settings

# --- _parse_task_block ---


def test_parse_task_block_happy_path() -> None:
    text = """\
Sure, here's your task:

===TASK===
type: bugfix
title: Fix login timeout
description: The login endpoint times out after 30 seconds
repo: acme/backend
branch: main
priority: high
===END===
"""
    result = _parse_task_block(text)
    assert result is not None
    assert result["type"] == "bugfix"
    assert result["title"] == "Fix login timeout"
    assert result["description"] == "The login endpoint times out after 30 seconds"
    assert result["repo"] == "acme/backend"
    assert result["branch"] == "main"
    assert result["priority"] == "high"


def test_parse_task_block_missing_title() -> None:
    text = """\
===TASK===
type: feature
description: Add user profiles
===END===
"""
    result = _parse_task_block(text)
    assert result is None


def test_parse_task_block_no_block() -> None:
    text = "Just some regular conversation without any task block."
    result = _parse_task_block(text)
    assert result is None


def test_parse_task_block_extra_whitespace() -> None:
    text = """\
===TASK===
  type:   feature
  title:   Add dark mode
  description:   Support dark mode toggle in settings
  repo:   acme/frontend
===END===
"""
    result = _parse_task_block(text)
    assert result is not None
    assert result["title"] == "Add dark mode"
    assert result["type"] == "feature"
    assert result["repo"] == "acme/frontend"


# --- _build_system_prompt ---


def test_build_system_prompt_includes_defaults(mock_settings: Settings) -> None:
    prompt = _build_system_prompt(mock_settings)
    assert mock_settings.environment.value in prompt
    # Should contain the org/repo defaults (or "(not set)" if empty)
    if mock_settings.github_default_org:
        assert mock_settings.github_default_org in prompt
    if mock_settings.github_default_repo:
        assert mock_settings.github_default_repo in prompt
    assert "===TASK===" in prompt
    assert "===END===" in prompt


# --- _check_ollama ---


def test_check_ollama_success() -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"models": [{"name": "llama3.2:latest"}]}
    mock_resp.raise_for_status = MagicMock()

    with patch("henchmen.cli.chat.httpx.get", return_value=mock_resp):
        result = _check_ollama("http://localhost:11434", "llama3.2")
    assert result is None


def test_check_ollama_not_running() -> None:
    with patch("henchmen.cli.chat.httpx.get", side_effect=httpx.ConnectError("refused")):
        result = _check_ollama("http://localhost:11434", "llama3.2")
    assert result is not None
    assert "Cannot connect" in result
    assert "ollama serve" in result


def test_check_ollama_model_missing() -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"models": [{"name": "mistral:latest"}]}
    mock_resp.raise_for_status = MagicMock()

    with patch("henchmen.cli.chat.httpx.get", return_value=mock_resp):
        result = _check_ollama("http://localhost:11434", "llama3.2")
    assert result is not None
    assert "not available" in result
    assert "ollama pull llama3.2" in result


# --- _dispatch_task ---


@pytest.mark.asyncio
async def test_dispatch_task_local_success(mock_settings: Settings) -> None:
    task_data = {"title": "Fix bug", "description": "Fix the login bug", "repo": "acme/backend"}

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"task_id": "abc-123", "status": "accepted"}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("henchmen.cli.chat.httpx.AsyncClient", return_value=mock_client):
        result = await _dispatch_task(task_data, mock_settings)

    assert result["method"] == "local"
    assert result["result"]["task_id"] == "abc-123"


@pytest.mark.asyncio
async def test_dispatch_task_local_down_falls_back(mock_settings: Settings) -> None:
    task_data = {"title": "Fix bug", "description": "Fix the login bug"}

    # Local dispatch fails with ConnectError
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_normalizer = MagicMock()
    mock_task = MagicMock()
    mock_task.id = "task-456"
    mock_normalizer.from_cli.return_value = mock_task
    mock_normalizer.publish_task = AsyncMock(return_value="msg-789")

    mock_registry = MagicMock()
    mock_broker = MagicMock()
    mock_registry.return_value.get_message_broker.return_value = mock_broker

    with (
        patch("henchmen.cli.chat.httpx.AsyncClient", return_value=mock_client),
        patch("henchmen.dispatch.normalizer.TaskNormalizer", return_value=mock_normalizer),
        patch("henchmen.providers.registry.ProviderRegistry", mock_registry),
    ):
        result = await _dispatch_task(task_data, mock_settings)

    assert result["method"] == "broker"
    assert result["result"]["task_id"] == "task-456"
    assert result["result"]["message_id"] == "msg-789"


# --- _call_ollama ---


@pytest.mark.asyncio
async def test_call_ollama_returns_content_no_stream() -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"message": {"content": "Hello! What task?"}}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("henchmen.cli.chat.httpx.AsyncClient", return_value=mock_client):
        result = await _call_ollama("http://localhost:11434", "llama3.2", [], stream_to_stdout=False)

    assert result == "Hello! What task?"


# --- _read_multiline_input ---


def test_read_multiline_input_single_line() -> None:
    with patch("builtins.input", return_value="hello world"):
        result = _read_multiline_input("> ")
    assert result == "hello world"


# --- run_chat_cli full flow ---


@pytest.mark.asyncio
async def test_chat_loop_full_flow(mock_settings: Settings) -> None:
    """Simulate a conversation: user describes task, LLM emits block, user confirms."""
    from henchmen.cli.chat import _chat_loop

    # Mock _check_ollama to pass
    # Mock input() to return a sequence of user inputs
    # Mock _call_ollama to return responses
    llm_response = """\
Got it! Here's your task:

===TASK===
type: bugfix
title: Fix login bug in auth module
description: Fix the authentication bug in the login endpoint
repo: test/repo
branch: main
priority: normal
===END===
"""

    with (
        patch("henchmen.cli.chat._check_ollama", return_value=None),
        patch("henchmen.cli.chat._call_ollama", AsyncMock(return_value=llm_response)),
        patch("henchmen.cli.chat._dispatch_task", AsyncMock(return_value={"method": "local", "result": {"ok": True}})),
        patch(
            "henchmen.cli.chat._read_multiline_input",
            side_effect=["Fix the login bug in the auth module"],
        ),
        patch("builtins.input", return_value="y"),  # confirm dispatch
        patch("builtins.print"),
    ):
        exit_code = await _chat_loop()

    assert exit_code == 0


@pytest.mark.asyncio
async def test_chat_loop_ollama_not_running(mock_settings: Settings) -> None:
    """If Ollama is not running, exit with code 1."""
    from henchmen.cli.chat import _chat_loop

    with (
        patch("henchmen.cli.chat._check_ollama", return_value="Cannot connect to Ollama"),
        patch("builtins.print"),
    ):
        exit_code = await _chat_loop()

    assert exit_code == 1


@pytest.mark.asyncio
async def test_chat_loop_quit(mock_settings: Settings) -> None:
    """User types 'quit' to exit."""
    from henchmen.cli.chat import _chat_loop

    with (
        patch("henchmen.cli.chat._check_ollama", return_value=None),
        patch("henchmen.cli.chat._read_multiline_input", side_effect=["quit"]),
        patch("builtins.print"),
    ):
        exit_code = await _chat_loop()

    assert exit_code == 0
