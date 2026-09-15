"""`henchmen chat` — interactive task builder powered by the configured LLM.

Provides a conversational REPL where the user describes work in natural
language and the LLM assembles a structured HenchmenTask. Settings-aware:
pre-loads defaults (repo, org, env) so the user doesn't repeat themselves.

The provider comes from ``ProviderRegistry`` like every other LLM consumer,
so chat works on Vertex AI, OpenAI, Anthropic, Bedrock or Ollama. Ollama
keeps its streaming path (tokens appear as they arrive); the other providers
go through ``LLMProvider.generate()`` and print the whole reply at once.
"""

from __future__ import annotations

import asyncio
import json
import re
import select
import sys
from typing import TYPE_CHECKING, Any

import httpx

from henchmen.config.settings import Settings, get_settings
from henchmen.dispatch.api_models import dispatch_auth_headers
from henchmen.models.llm import Message, MessageRole, ModelTier
from henchmen.models.task import TaskType
from henchmen.utils.repositories import default_repository, qualify_repo

if TYPE_CHECKING:
    from henchmen.providers.interfaces.llm_provider import LLMProvider

_TASK_PATTERN = re.compile(r"={2,}TASK={2,}\s*\n(.*?)\n={2,}END={2,}", re.DOTALL)
_CHAT_TEMPERATURE = 0.7
_LOCAL_DISPATCH_TIMEOUT = 5.0
_CHAT_MAX_TOKENS = 4096


def _local_dispatch_url(settings: Settings) -> str:
    """Where a locally running ``henchmen serve`` accepts CLI task creation."""
    return f"http://localhost:{settings.local_serve_port}/dispatch/api/v1/tasks"


def _read_multiline_input(prompt: str = "> ") -> str:
    """Read user input, collecting all pasted lines into a single string.

    Python's ``input()`` only reads one line. When users paste multi-line
    text, the remaining lines sit in the stdin buffer. This function drains
    that buffer so pastes are captured in full.
    """
    first_line = input(prompt)  # noqa: ASYNC250
    lines = [first_line]

    # Drain any remaining lines in the stdin buffer (from a paste).
    # Windows: use msvcrt.kbhit(). Unix/macOS: use select().
    try:
        if sys.platform == "win32":
            import msvcrt

            while msvcrt.kbhit():
                extra = sys.stdin.readline()
                if not extra:
                    break
                lines.append(extra.rstrip("\n"))
        else:
            while select.select([sys.stdin], [], [], 0.0)[0]:
                extra = sys.stdin.readline()
                if not extra:
                    break
                lines.append(extra.rstrip("\n"))
    except Exception:
        pass  # If buffer drain fails, we still have the first line

    return "\n".join(lines)


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
6. Branch: the BASE branch to work from (default: main). This is NOT a feature \
branch name — Henchmen creates its own feature branch automatically.

When you have enough information to build the task, emit a structured block \
in EXACTLY this format (do NOT use markdown fences around it):

===TASK===
type: <bugfix|feature|refactor>
title: <short title>
description: <detailed description>
repo: <owner/repo>
branch: <base branch, almost always "main">
priority: <critical|high|normal|low>
===END===

Rules:
- Be conversational and helpful, but stay focused on task building.
- Ask one question at a time — don't overwhelm the user.
- Use the defaults above when the user doesn't specify values.
- Only emit the ===TASK=== block when you have at least a title and description.
- Never emit the block inside markdown code fences.
- The branch field must be an EXISTING branch (usually "main"). Never invent feature branch names."""


def _is_ollama(provider: LLMProvider) -> bool:
    """True when the resolved provider is the local Ollama backend."""
    from henchmen.providers.local.ollama import OllamaProvider

    return isinstance(provider, OllamaProvider)


def _resolve_chat_model(settings: Settings, provider: LLMProvider) -> str:
    """Model behind chat: the explicit override, else the provider's LIGHT tier.

    Ollama keeps its dedicated override chain so an existing
    ``HENCHMEN_LLM_OLLAMA_CHAT_MODEL`` keeps working.
    """
    from henchmen.providers.tiers import tier_models

    if _is_ollama(provider):
        return settings.llm_ollama_chat_model or settings.llm_chat_model or settings.llm_ollama_model
    if settings.llm_chat_model:
        return settings.llm_chat_model
    return tier_models(settings).get(ModelTier.LIGHT, "") or ModelTier.LIGHT.value


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


def _print_welcome(settings: Settings, model: str, provider_name: str) -> None:
    """Print the welcome banner."""
    env = settings.environment.value
    # The Console saves github_default_repo as owner/name; only a bare name gets the org prefix.
    repo_display = default_repository(settings) or "(not set)"

    print()
    print("henchmen chat -- interactive task builder")
    print()
    print(f"  Model:   {model} (via {provider_name})")
    print(f"  Repo:    {repo_display}")
    print(f"  Env:     {env}")
    print()
    print("Describe what you need done, and I'll help you build a task.")
    print("Paste multi-line specs directly -- all lines will be captured.")
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
    *,
    stream_to_stdout: bool = True,
) -> str:
    """Send a chat request to Ollama and return the response content.

    When *stream_to_stdout* is True (the default), tokens are printed to
    stdout as they arrive so the user sees the LLM "thinking" in real time.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream_to_stdout,
        "options": {"temperature": _CHAT_TEMPERATURE},
    }
    async with httpx.AsyncClient(base_url=base_url, timeout=300.0) as client:
        if not stream_to_stdout:
            resp = await client.post("/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            content: str = data.get("message", {}).get("content", "")
            return content

        # Streaming mode: print tokens as they arrive
        collected: list[str] = []
        print()  # blank line before response
        async with client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for raw_line in resp.aiter_lines():
                if not raw_line.strip():
                    continue
                try:
                    chunk = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("message", {}).get("content", "")
                if token:
                    print(token, end="", flush=True)
                    collected.append(token)
                if chunk.get("done", False):
                    break
        print("\n")  # newline after streamed response
        return "".join(collected)


async def _complete(
    provider: LLMProvider,
    settings: Settings,
    model: str,
    system_prompt: str,
    history: list[Message],
) -> str:
    """One assistant turn: stream from Ollama, or generate() for every other provider."""
    if _is_ollama(provider):
        wire = [{"role": "system", "content": system_prompt}]
        wire += [{"role": m.role.value, "content": m.content} for m in history]
        return await _call_ollama(settings.llm_ollama_base_url, model, wire)

    response = await provider.generate(
        messages=history,
        model=model,
        temperature=_CHAT_TEMPERATURE,
        max_tokens=_CHAT_MAX_TOKENS,
        system_prompt=system_prompt,
    )
    print()
    print(response.content)
    print()
    return response.content


def _task_type(task_data: dict[str, str]) -> TaskType | None:
    """The user's explicit task type, or ``None`` when none (or an unknown one) was collected.

    Sent as ``task_type`` so Mastermind picks the scheme from it instead of
    keyword matching; an unrecognised value is dropped rather than 422 the
    whole dispatch.
    """
    raw = task_data.get("type", "").strip().lower()
    try:
        return TaskType(raw)
    except ValueError:
        return None


async def _dispatch_task(task_data: dict[str, str], settings: Settings) -> dict[str, Any]:
    """Dispatch a task to a local ``henchmen serve``, else to a durable broker.

    Returns a dict with 'method' and 'result' keys. The in-memory broker is
    process-local: publishing into it from the chat process would drop the
    task on exit, so that combination raises instead of reporting success.
    """
    from henchmen.providers.registry import ProviderRegistry

    # Build the payload matching TaskNormalizer.from_cli() contract
    repo = qualify_repo(task_data.get("repo", settings.github_default_repo or ""), settings.github_default_org)

    payload: dict[str, Any] = {
        "title": task_data["title"],
        "description": task_data.get("description", ""),
        "repo": repo,
        "branch": task_data.get("branch", "main"),
        "priority": task_data.get("priority", "normal"),
        "created_by": "chat",
    }
    task_type = _task_type(task_data)
    if task_type is not None:
        payload["task_type"] = task_type.value

    url = _local_dispatch_url(settings)
    # Dispatch requires this bearer token on /api/v1/tasks whenever it is configured
    # (always on a desktop install, where apply generates it).
    headers = dispatch_auth_headers(settings.dispatch_api_token)
    try:
        async with httpx.AsyncClient(timeout=_LOCAL_DISPATCH_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return {"method": "local", "result": resp.json()}
    except (httpx.ConnectError, httpx.ConnectTimeout):
        pass  # Fall through to broker

    registry = ProviderRegistry(settings)
    if registry.resolve_provider_name("message_broker") == "local" and not settings.local_forward_base_url:
        raise RuntimeError(
            f"henchmen serve is not reachable at {url} and the local in-memory broker cannot deliver "
            "tasks across processes. Start it with `henchmen serve` (set HENCHMEN_LOCAL_SERVE_PORT "
            "if you use a non-default port)."
        )

    from henchmen.dispatch.normalizer import TaskNormalizer

    normalizer = TaskNormalizer()
    task = normalizer.from_cli(payload)
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
    from henchmen.providers.registry import ProviderRegistry
    from henchmen.providers.tiers import active_llm_provider

    settings = get_settings()
    try:
        provider = ProviderRegistry(settings).get_llm_provider()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("Hint: run `henchmen init` to pick a provider.", file=sys.stderr)
        return 1

    provider_name = active_llm_provider(settings)
    model = _resolve_chat_model(settings, provider)
    if not model:
        print(f"ERROR: no chat model configured for provider {provider_name!r}.", file=sys.stderr)
        print("Hint: set HENCHMEN_LLM_CHAT_MODEL or run `henchmen init`.", file=sys.stderr)
        return 1

    if _is_ollama(provider):
        error = _check_ollama(settings.llm_ollama_base_url, model)
        if error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1

    _print_welcome(settings, model, provider_name)

    system_prompt = _build_system_prompt(settings)
    history: list[Message] = []
    # Track extracted fields for /status
    last_extracted: dict[str, str] = {}

    while True:
        try:
            user_input = _read_multiline_input("> ")
        except (KeyboardInterrupt, EOFError):
            print("\nChat ended.")
            return 0

        user_input = user_input.strip()
        if not user_input:
            continue

        # REPL commands (only check single-line inputs)
        if "\n" not in user_input:
            if user_input.lower() in ("quit", "exit"):
                print("Chat ended.")
                return 0

            if user_input == "/help":
                _print_help()
                continue

            if user_input == "/reset":
                history = []
                last_extracted = {}
                print("Conversation reset. Start describing your task.")
                continue

            if user_input == "/status":
                if last_extracted:
                    _print_task_preview(last_extracted)
                else:
                    print("No task fields collected yet.")
                continue

        # Show line count for multi-line pastes so user knows it was captured
        line_count = user_input.count("\n") + 1
        if line_count > 1:
            print(f"(received {line_count} lines)")

        history.append(Message(role=MessageRole.USER, content=user_input))

        try:
            response = await _complete(provider, settings, model, system_prompt, history)
        except Exception as exc:
            print(f"LLM error: {exc}")
            # Remove the failed user message so conversation stays consistent
            history.pop()
            continue

        if not response.strip():
            print("(empty response -- try rephrasing)")
            history.pop()
            continue

        history.append(Message(role=MessageRole.ASSISTANT, content=response))

        # Check for task block (the reply has already been printed)
        task_data = _parse_task_block(response)
        if task_data:
            last_extracted = task_data
            dispatched = await _confirm_and_dispatch(task_data, settings)
            if dispatched:
                return 0


def run_chat_cli() -> int:
    """Entry point for `henchmen chat`. Returns exit code."""
    return asyncio.run(_chat_loop())
