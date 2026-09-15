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
    _local_dispatch_url,
    _parse_task_block,
    _read_multiline_input,
    _resolve_chat_model,
    _task_type,
)
from henchmen.config.settings import Settings
from henchmen.dispatch.api_models import CreateTaskRequest
from henchmen.models.llm import LLMResponse, ModelTier, TokenUsage
from henchmen.models.task import TaskType

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
    assert _parse_task_block(text) is None


def test_parse_task_block_no_block() -> None:
    assert _parse_task_block("Just some regular conversation without any task block.") is None


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
    if mock_settings.github_default_org:
        assert mock_settings.github_default_org in prompt
    if mock_settings.github_default_repo:
        assert mock_settings.github_default_repo in prompt
    assert "===TASK===" in prompt
    assert "===END===" in prompt


# --- _check_ollama ---


def test_check_ollama_success() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"models": [{"name": "llama3.2:latest"}]}
    mock_resp.raise_for_status = MagicMock()

    with patch("henchmen.cli.chat.httpx.get", return_value=mock_resp):
        assert _check_ollama("http://localhost:11434", "llama3.2") is None


def test_check_ollama_not_running() -> None:
    with patch("henchmen.cli.chat.httpx.get", side_effect=httpx.ConnectError("refused")):
        result = _check_ollama("http://localhost:11434", "llama3.2")
    assert result is not None
    assert "Cannot connect" in result
    assert "ollama serve" in result


def test_check_ollama_model_missing() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"models": [{"name": "mistral:latest"}]}
    mock_resp.raise_for_status = MagicMock()

    with patch("henchmen.cli.chat.httpx.get", return_value=mock_resp):
        result = _check_ollama("http://localhost:11434", "llama3.2")
    assert result is not None
    assert "not available" in result
    assert "ollama pull llama3.2" in result


# --- model + URL resolution (no magic constants) ---


class TestResolution:
    @pytest.fixture(autouse=True)
    def _hermetic(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        """Build Settings from defaults only — never from the developer's .env.local."""
        import os

        for key in [k for k in os.environ if k.startswith("HENCHMEN_")]:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.chdir(tmp_path)
        yield

    def test_local_dispatch_url_honours_serve_port(self) -> None:
        settings = Settings(provider="local", local_serve_port=9123)
        assert _local_dispatch_url(settings) == "http://localhost:9123/dispatch/api/v1/tasks"

    def test_ollama_model_chain(self) -> None:
        settings = Settings(provider="local", llm_ollama_model="base:7b")
        with patch("henchmen.cli.chat._is_ollama", return_value=True):
            assert _resolve_chat_model(settings, MagicMock()) == "base:7b"

            settings_chat = Settings(provider="local", llm_ollama_model="base:7b", llm_chat_model="shared:7b")
            assert _resolve_chat_model(settings_chat, MagicMock()) == "shared:7b"

            settings_ollama_chat = Settings(
                provider="local",
                llm_ollama_model="base:7b",
                llm_chat_model="shared:7b",
                llm_ollama_chat_model="ollama-chat:7b",
            )
            assert _resolve_chat_model(settings_ollama_chat, MagicMock()) == "ollama-chat:7b"

    def test_non_ollama_falls_back_to_light_tier(self) -> None:
        settings = Settings(provider="local", llm_provider="anthropic")
        with patch("henchmen.cli.chat._is_ollama", return_value=False):
            model = _resolve_chat_model(settings, MagicMock())
        assert model == settings.anthropic_model_light
        assert model != ModelTier.LIGHT.value

    def test_explicit_chat_model_wins_for_non_ollama(self) -> None:
        settings = Settings(provider="local", llm_provider="openai", llm_chat_model="gpt-custom")
        with patch("henchmen.cli.chat._is_ollama", return_value=False):
            assert _resolve_chat_model(settings, MagicMock()) == "gpt-custom"


# --- _dispatch_task ---


def _http_client(*, post: AsyncMock) -> AsyncMock:
    client = AsyncMock()
    client.post = post
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_dispatch_task_local_success(mock_settings: Settings) -> None:
    task_data = {"title": "Fix bug", "description": "Fix the login bug", "repo": "acme/backend", "type": "bugfix"}

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"task_id": "abc-123", "status": "accepted"}
    mock_resp.raise_for_status = MagicMock()
    post = AsyncMock(return_value=mock_resp)

    with patch("henchmen.cli.chat.httpx.AsyncClient", return_value=_http_client(post=post)):
        result = await _dispatch_task(task_data, mock_settings)

    assert result["method"] == "local"
    assert result["result"]["task_id"] == "abc-123"
    url, kwargs = post.call_args[0][0], post.call_args[1]
    assert url == _local_dispatch_url(mock_settings)
    # The payload must be accepted by the real intake contract (extra="forbid"):
    # an unknown "type" key would 422 every chat dispatch against `henchmen serve`.
    body = CreateTaskRequest.model_validate(kwargs["json"])
    # The collected task type travels as the explicit field; the description is untouched.
    assert body.task_type == TaskType.BUGFIX
    assert body.description == "Fix the login bug"
    # No token configured: no Authorization header is invented.
    assert "Authorization" not in kwargs["headers"]


@pytest.mark.asyncio
async def test_dispatch_task_sends_the_dispatch_api_token(mock_settings: Settings) -> None:
    settings = mock_settings.model_copy(update={"dispatch_api_token": "s3cret"})
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"task_id": "abc-123"}
    mock_resp.raise_for_status = MagicMock()
    post = AsyncMock(return_value=mock_resp)

    with patch("henchmen.cli.chat.httpx.AsyncClient", return_value=_http_client(post=post)):
        await _dispatch_task({"title": "Fix bug", "description": "d", "repo": "acme/backend"}, settings)

    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer s3cret"}


class TestTaskType:
    def test_known_types_are_case_insensitive(self) -> None:
        assert _task_type({"type": "Feature"}) == TaskType.FEATURE
        assert _task_type({"type": " refactor "}) == TaskType.REFACTOR

    def test_unknown_or_missing_type_is_dropped(self) -> None:
        assert _task_type({"type": "chore"}) is None
        assert _task_type({}) is None

    @pytest.mark.asyncio
    async def test_unknown_type_is_not_sent(self, mock_settings: Settings) -> None:
        """An unrecognised type must not 422 the whole dispatch."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"task_id": "abc-123"}
        mock_resp.raise_for_status = MagicMock()
        post = AsyncMock(return_value=mock_resp)

        with patch("henchmen.cli.chat.httpx.AsyncClient", return_value=_http_client(post=post)):
            await _dispatch_task(
                {"title": "T", "description": "d", "repo": "acme/backend", "type": "chore"}, mock_settings
            )

        body = post.call_args.kwargs["json"]
        assert "task_type" not in body
        CreateTaskRequest.model_validate(body)

    @pytest.mark.asyncio
    async def test_explicit_bugfix_routes_to_bugfix_scheme_end_to_end(self, mock_settings: Settings) -> None:
        """A chat task typed as bugfix and titled "Add ..." reaches bugfix_standard through the real intake."""
        from henchmen.dispatch.normalizer import TaskNormalizer
        from henchmen.mastermind.agent import MastermindAgent

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        post = AsyncMock(return_value=mock_resp)
        task_data = {"title": "Add null check to parseConfig", "description": "crashes on None", "type": "bugfix"}
        with patch("henchmen.cli.chat.httpx.AsyncClient", return_value=_http_client(post=post)):
            await _dispatch_task({**task_data, "repo": "acme/backend"}, mock_settings)

        request = CreateTaskRequest.model_validate(post.call_args.kwargs["json"])
        task = TaskNormalizer().from_cli(request.model_dump(), mock_settings)
        agent = MastermindAgent.__new__(MastermindAgent)
        assert await agent._select_scheme(task) == "bugfix_standard"


@pytest.mark.asyncio
async def test_dispatch_task_raises_when_serve_is_down_and_broker_is_in_memory(mock_settings: Settings) -> None:
    """The in-memory broker is process-local — publishing into it would drop the task."""
    task_data = {"title": "Fix bug", "description": "Fix the login bug"}
    client = _http_client(post=AsyncMock(side_effect=httpx.ConnectError("refused")))

    registry = MagicMock()
    registry.return_value.resolve_provider_name.return_value = "local"

    with (
        patch("henchmen.cli.chat.httpx.AsyncClient", return_value=client),
        patch("henchmen.providers.registry.ProviderRegistry", registry),
        pytest.raises(RuntimeError, match="henchmen serve is not reachable"),
    ):
        await _dispatch_task(task_data, mock_settings)

    registry.return_value.get_message_broker.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_task_falls_back_to_durable_broker(mock_settings: Settings) -> None:
    task_data = {"title": "Fix bug", "description": "Fix the login bug"}
    client = _http_client(post=AsyncMock(side_effect=httpx.ConnectError("refused")))

    normalizer = MagicMock()
    task = MagicMock()
    task.id = "task-456"
    normalizer.from_cli.return_value = task
    normalizer.publish_task = AsyncMock(return_value="msg-789")

    registry = MagicMock()
    registry.return_value.resolve_provider_name.return_value = "gcp"

    with (
        patch("henchmen.cli.chat.httpx.AsyncClient", return_value=client),
        patch("henchmen.dispatch.normalizer.TaskNormalizer", return_value=normalizer),
        patch("henchmen.providers.registry.ProviderRegistry", registry),
    ):
        result = await _dispatch_task(task_data, mock_settings)

    assert result["method"] == "broker"
    assert result["result"]["task_id"] == "task-456"
    assert result["result"]["message_id"] == "msg-789"


# --- _call_ollama ---


@pytest.mark.asyncio
async def test_call_ollama_returns_content_no_stream() -> None:
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"message": {"content": "Hello! What task?"}}
    mock_resp.raise_for_status = MagicMock()

    with patch(
        "henchmen.cli.chat.httpx.AsyncClient", return_value=_http_client(post=AsyncMock(return_value=mock_resp))
    ):
        result = await _call_ollama("http://localhost:11434", "llama3.2", [], stream_to_stdout=False)

    assert result == "Hello! What task?"


# --- _read_multiline_input ---


def test_read_multiline_input_single_line() -> None:
    with patch("builtins.input", return_value="hello world"):
        assert _read_multiline_input("> ") == "hello world"


# --- _chat_loop ---

_TASK_REPLY = """\
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


def _fake_provider(reply: str) -> MagicMock:
    provider = MagicMock()
    provider.generate = AsyncMock(
        return_value=LLMResponse(
            content=reply,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            model="fake",
            finish_reason="stop",
        )
    )
    return provider


@pytest.mark.asyncio
async def test_chat_loop_uses_generate_for_non_ollama_providers(mock_settings: Settings) -> None:
    from henchmen.cli.chat import _chat_loop

    provider = _fake_provider(_TASK_REPLY)
    registry = MagicMock()
    registry.return_value.get_llm_provider.return_value = provider

    with (
        patch("henchmen.providers.registry.ProviderRegistry", registry),
        patch("henchmen.cli.chat._is_ollama", return_value=False),
        patch("henchmen.cli.chat._check_ollama") as ollama_probe,
        patch("henchmen.cli.chat._dispatch_task", AsyncMock(return_value={"method": "local", "result": {"ok": True}})),
        patch("henchmen.cli.chat._read_multiline_input", side_effect=["Fix the login bug"]),
        patch("builtins.input", return_value="y"),
        patch("builtins.print"),
    ):
        exit_code = await _chat_loop()

    assert exit_code == 0
    provider.generate.assert_awaited_once()
    ollama_probe.assert_not_called()


@pytest.mark.asyncio
async def test_chat_loop_streams_for_ollama(mock_settings: Settings) -> None:
    from henchmen.cli.chat import _chat_loop

    registry = MagicMock()
    registry.return_value.get_llm_provider.return_value = MagicMock()

    with (
        patch("henchmen.providers.registry.ProviderRegistry", registry),
        patch("henchmen.cli.chat._is_ollama", return_value=True),
        patch("henchmen.cli.chat._check_ollama", return_value=None),
        patch("henchmen.cli.chat._call_ollama", AsyncMock(return_value=_TASK_REPLY)) as call_ollama,
        patch("henchmen.cli.chat._dispatch_task", AsyncMock(return_value={"method": "local", "result": {"ok": True}})),
        patch("henchmen.cli.chat._read_multiline_input", side_effect=["Fix the login bug"]),
        patch("builtins.input", return_value="y"),
        patch("builtins.print"),
    ):
        exit_code = await _chat_loop()

    assert exit_code == 0
    call_ollama.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_loop_ollama_not_running(mock_settings: Settings) -> None:
    from henchmen.cli.chat import _chat_loop

    registry = MagicMock()
    registry.return_value.get_llm_provider.return_value = MagicMock()

    with (
        patch("henchmen.providers.registry.ProviderRegistry", registry),
        patch("henchmen.cli.chat._is_ollama", return_value=True),
        patch("henchmen.cli.chat._check_ollama", return_value="Cannot connect to Ollama"),
        patch("builtins.print"),
    ):
        exit_code = await _chat_loop()

    assert exit_code == 1


@pytest.mark.asyncio
async def test_chat_loop_unknown_provider_exits_with_hint(mock_settings: Settings, capsys) -> None:
    from henchmen.cli.chat import _chat_loop

    registry = MagicMock()
    registry.return_value.get_llm_provider.side_effect = ValueError("Unknown provider for llm: 'nope'")

    with patch("henchmen.providers.registry.ProviderRegistry", registry):
        exit_code = await _chat_loop()

    assert exit_code == 1
    assert "henchmen init" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_chat_loop_quit(mock_settings: Settings) -> None:
    from henchmen.cli.chat import _chat_loop

    registry = MagicMock()
    registry.return_value.get_llm_provider.return_value = MagicMock()

    with (
        patch("henchmen.providers.registry.ProviderRegistry", registry),
        patch("henchmen.cli.chat._is_ollama", return_value=True),
        patch("henchmen.cli.chat._check_ollama", return_value=None),
        patch("henchmen.cli.chat._read_multiline_input", side_effect=["quit"]),
        patch("builtins.print"),
    ):
        exit_code = await _chat_loop()

    assert exit_code == 0


@pytest.mark.asyncio
async def test_chat_in_the_container_uses_the_token_apply_generated(monkeypatch, tmp_path) -> None:
    from henchmen.config import paths
    from henchmen.console.config_store import ConfigStore

    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    config = tmp_path / "henchmen.env"
    config.write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")
    ConfigStore(config, tmp_path / "secrets").ensure_dispatch_api_token()
    settings = Settings(_env_file=paths.env_files())  # type: ignore[call-arg]
    assert settings.dispatch_api_token

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"task_id": "abc-123"}
    mock_resp.raise_for_status = MagicMock()
    post = AsyncMock(return_value=mock_resp)
    with patch("henchmen.cli.chat.httpx.AsyncClient", return_value=_http_client(post=post)):
        await _dispatch_task({"title": "Fix bug", "description": "d", "repo": "acme/backend"}, settings)
    assert post.call_args.kwargs["headers"] == {"Authorization": f"Bearer {settings.dispatch_api_token}"}
