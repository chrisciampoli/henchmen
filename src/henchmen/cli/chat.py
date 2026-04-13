"""`henchmen chat` — interactive task builder powered by a local LLM (Ollama).

Provides a conversational REPL where the user describes work in natural
language and the LLM assembles a structured HenchmenTask. Settings-aware:
pre-loads defaults (repo, org, env) so the user doesn't repeat themselves.
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

import httpx

from henchmen.config.settings import Settings, get_settings

_TASK_PATTERN = re.compile(r"===TASK===\s*\n(.*?)\n===END===", re.DOTALL)
_CHAT_TEMPERATURE = 0.7
_LOCAL_DISPATCH_URL = "http://localhost:8000/dispatch/api/v1/tasks"
_LOCAL_DISPATCH_TIMEOUT = 5.0


def _build_system_prompt(settings: Settings) -> str:
    """Build the system prompt with settings defaults interpolated."""
    org = settings.github_default_org or "(not set)"
    repo = settings.github_default_repo or "(not set)"
    env = settings.environment.value

    return f"""\
You are a Henchmen task builder assistant. Your job is to help the user \
describe a coding task and then assemble a structured task for dispatch.

Current defaults:
- Organization: {org}
- Repository: {repo}
- Environment: {env}

Ask the user ONE question at a time to gather the following information:
1. Task type: bugfix, feature, or refactor
2. Repository (default: {repo}) — confirm or let the user change it
3. Title: a short, descriptive title for the task
4. Description: a detailed description of what needs to be done
5. Priority: critical, high, normal (default), or low
6. Branch: target branch (default: main)

When you have enough information to build the task, emit a structured block \
in EXACTLY this format (do NOT use markdown fences around it):

===TASK===
type: <bugfix|feature|refactor>
title: <short title>
description: <detailed description>
repo: <owner/repo>
branch: <branch name>
priority: <critical|high|normal|low>
===END===

Rules:
- Be conversational and helpful, but stay focused on task building.
- Ask one question at a time — don't overwhelm the user.
- Use the defaults above when the user doesn't specify values.
- Only emit the ===TASK=== block when you have at least a title and description.
- Never emit the block inside markdown code fences."""


def _check_ollama(base_url: str, model: str) -> str | None:
    """Pre-flight check: verify Ollama is running and model is available.

    Returns an error message string if something is wrong, or None if OK.
    """
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=5.0)
        resp.raise_for_status()
    except httpx.ConnectError:
        return f"Cannot connect to Ollama at {base_url}.\nStart it with: ollama serve"
    except httpx.HTTPError as exc:
        return f"Ollama health check failed: {exc}"

    data = resp.json()
    available = [m.get("name", "") for m in data.get("models", [])]
    # Ollama tags include the `:latest` suffix; match with or without it.
    if not any(name == model or name.startswith(f"{model}:") for name in available):
        available_str = ", ".join(available) if available else "(none)"
        return (
            f"Model '{model}' is not available in Ollama.\n"
            f"Available models: {available_str}\n"
            f"Pull it with: ollama pull {model}"
        )
    return None


def _parse_task_block(text: str) -> dict[str, str] | None:
    """Extract a ===TASK===...===END=== block from LLM output.

    Returns a dict of parsed key-value pairs, or None if no valid block found.
    Requires at least a 'title' field.
    """
    match = _TASK_PATTERN.search(text)
    if not match:
        return None

    block = match.group(1)
    fields: dict[str, str] = {}
    for line in block.strip().splitlines():
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key and value:
                fields[key] = value

    if "title" not in fields:
        return None
    return fields


def _print_welcome(settings: Settings, model: str) -> None:
    """Print the welcome banner."""
    org = settings.github_default_org
    repo = settings.github_default_repo
    env = settings.environment.value
    repo_display = f"{org}/{repo}" if org and repo else repo or "(not set)"

    print()
    print("henchmen chat -- interactive task builder")
    print()
    print(f"  Model:   {model} (via Ollama)")
    print(f"  Repo:    {repo_display}")
    print(f"  Env:     {env}")
    print()
    print("Describe what you need done, and I'll help you build a task.")
    print("Type 'quit' to exit, '/help' for commands.")
    print()


def _print_help() -> None:
    """Print available REPL commands."""
    print()
    print("Commands:")
    print("  /help    -- show this help")
    print("  /reset   -- clear conversation, start over")
    print("  /status  -- show collected task fields so far")
    print("  quit     -- exit chat")
    print("  exit     -- exit chat")
    print()


def _print_task_preview(task_data: dict[str, str]) -> None:
    """Print a formatted task preview."""
    print()
    print("--- Task Preview ---")
    for key, value in task_data.items():
        print(f"  {key}: {value}")
    print("--------------------")
    print()


async def _call_ollama(
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
) -> str:
    """Send a chat request to Ollama and return the response content."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": _CHAT_TEMPERATURE},
    }
    async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as client:
        resp = await client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        content: str = data.get("message", {}).get("content", "")
        return content


async def _dispatch_task(task_data: dict[str, str], settings: Settings) -> dict[str, Any]:
    """Dispatch task via local HTTP first, falling back to Pub/Sub broker.

    Returns a dict with 'method' and 'result' keys.
    """
    # Build the payload matching TaskNormalizer.from_cli() contract
    org = settings.github_default_org
    repo = task_data.get("repo", settings.github_default_repo or "")
    if org and "/" not in repo:
        repo = f"{org}/{repo}"

    payload: dict[str, Any] = {
        "title": task_data["title"],
        "description": task_data.get("description", ""),
        "repo": repo,
        "branch": task_data.get("branch", "main"),
        "priority": task_data.get("priority", "normal"),
        "created_by": "chat",
    }

    # Try local HTTP dispatch first
    try:
        async with httpx.AsyncClient(timeout=_LOCAL_DISPATCH_TIMEOUT) as client:
            resp = await client.post(_LOCAL_DISPATCH_URL, json=payload)
            resp.raise_for_status()
            return {"method": "local", "result": resp.json()}
    except (httpx.ConnectError, httpx.ConnectTimeout):
        pass  # Fall through to broker

    # Fallback: broker dispatch
    from henchmen.dispatch.normalizer import TaskNormalizer
    from henchmen.providers.registry import ProviderRegistry

    normalizer = TaskNormalizer()
    task = normalizer.from_cli(payload)
    registry = ProviderRegistry(settings)
    broker = registry.get_message_broker()
    msg_id = await normalizer.publish_task(task, settings, broker)
    return {"method": "broker", "result": {"task_id": task.id, "message_id": msg_id}}


async def _confirm_and_dispatch(task_data: dict[str, str], settings: Settings) -> bool:
    """Show preview, ask for confirmation, and dispatch if confirmed.

    Returns True if dispatched, False if cancelled.
    """
    _print_task_preview(task_data)

    try:
        answer = input("Dispatch this task? [Y/n] ").strip().lower()  # noqa: ASYNC250
    except (KeyboardInterrupt, EOFError):
        print()
        return False

    if answer in ("", "y", "yes"):
        try:
            result = await _dispatch_task(task_data, settings)
            method = result["method"]
            print(f"Task dispatched via {method}: {result['result']}")
            return True
        except Exception as exc:
            print(f"Dispatch failed: {exc}")
            print("You can try again or type 'quit' to exit.")
            return False
    else:
        print("Task cancelled. Continue chatting to refine it.")
        return False


async def _chat_loop() -> int:
    """Async REPL loop. Returns exit code."""
    settings = get_settings()
    model = settings.llm_ollama_model
    base_url = settings.llm_ollama_base_url

    # Pre-flight check
    error = _check_ollama(base_url, model)
    if error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    _print_welcome(settings, model)

    system_prompt = _build_system_prompt(settings)
    ollama_messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
    ]
    # Track extracted fields for /status
    last_extracted: dict[str, str] = {}

    while True:
        try:
            user_input = input("> ").strip()  # noqa: ASYNC250
        except (KeyboardInterrupt, EOFError):
            print("\nChat ended.")
            return 0

        if not user_input:
            continue

        # REPL commands
        if user_input.lower() in ("quit", "exit"):
            print("Chat ended.")
            return 0

        if user_input == "/help":
            _print_help()
            continue

        if user_input == "/reset":
            ollama_messages = [{"role": "system", "content": system_prompt}]
            last_extracted = {}
            print("Conversation reset. Start describing your task.")
            continue

        if user_input == "/status":
            if last_extracted:
                _print_task_preview(last_extracted)
            else:
                print("No task fields collected yet.")
            continue

        # Add user message and call LLM
        ollama_messages.append({"role": "user", "content": user_input})

        try:
            response = await _call_ollama(base_url, model, ollama_messages)
        except Exception as exc:
            print(f"LLM error: {exc}")
            # Remove the failed user message so conversation stays consistent
            ollama_messages.pop()
            continue

        if not response.strip():
            print("(empty response — try rephrasing)")
            ollama_messages.pop()
            continue

        # Add assistant response to history
        ollama_messages.append({"role": "assistant", "content": response})

        # Check for task block
        task_data = _parse_task_block(response)
        if task_data:
            last_extracted = task_data
            # Print the conversational part (before the block) if any
            pre_block = response[: response.index("===TASK===")].strip()
            if pre_block:
                print(f"\n{pre_block}")

            dispatched = await _confirm_and_dispatch(task_data, settings)
            if dispatched:
                return 0
        else:
            print(f"\n{response}\n")

    return 0  # pragma: no cover


def run_chat_cli() -> int:
    """Entry point for `henchmen chat`. Returns exit code."""
    return asyncio.run(_chat_loop())
