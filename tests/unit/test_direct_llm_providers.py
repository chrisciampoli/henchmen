"""Tests for OpenAI and Anthropic direct API LLM providers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.models.llm import ModelTier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**overrides):
    """Build a real ``Settings`` instance for LLM provider tests.

    Uses ``os.environ`` to seed ``HENCHMEN_*`` env vars (the shared
    ``mock_settings`` fixture only covers GCP defaults), then applies
    per-call overrides via Pydantic's ``model_copy``. The autouse
    ``_isolate_settings`` fixture clears ``get_settings.cache_clear()``
    between tests, so each invocation rebuilds a fresh instance.
    """
    import os

    from henchmen.config.settings import get_settings

    os.environ.setdefault("HENCHMEN_GCP_PROJECT_ID", "test-project")
    os.environ.setdefault("HENCHMEN_OPENAI_API_KEY", "sk-test-openai")
    os.environ.setdefault("HENCHMEN_ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    settings = get_settings()
    if overrides:
        settings = settings.model_copy(update=overrides)
    return settings


# ---------------------------------------------------------------------------
# OpenAIProvider
# ---------------------------------------------------------------------------


class TestOpenAIProvider:
    def _make_provider(self, **settings_overrides):
        settings = _settings(**settings_overrides)
        mock_client = MagicMock()
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            from henchmen.providers.openai import OpenAIProvider

            provider = OpenAIProvider(settings)
        provider._client = mock_client
        return provider

    def test_resolve_tier_complex(self):
        provider = self._make_provider()
        assert provider.resolve_tier(ModelTier.COMPLEX) == "gpt-4.1"

    def test_resolve_tier_light(self):
        provider = self._make_provider()
        assert provider.resolve_tier(ModelTier.LIGHT) == "gpt-4.1-mini"

    def test_resolve_tier_reasoning(self):
        provider = self._make_provider()
        assert provider.resolve_tier(ModelTier.REASONING) == "o3"

    def test_resolve_tier_passthrough_unknown(self):
        provider = self._make_provider()
        assert provider.resolve_tier("gpt-custom") == "gpt-custom"

    def test_supported_models(self):
        # L10 fix: supported_models now returns the three configured tier
        # models (from Settings), deduped. It no longer includes hard-coded
        # extras like gpt-4o.
        provider = self._make_provider()
        models = provider.supported_models()
        assert "gpt-4.1" in models
        assert "gpt-4.1-mini" in models
        assert "o3" in models

    @pytest.mark.asyncio
    async def test_count_tokens_approximation(self):
        provider = self._make_provider()
        count = await provider.count_tokens("hello world", "gpt-4.1")
        # 11 chars -> 2 tokens (integer division by 4)
        assert count == 2

    @pytest.mark.asyncio
    async def test_count_tokens_longer_text(self):
        provider = self._make_provider()
        text = "a" * 400
        count = await provider.count_tokens(text, "gpt-4.1")
        assert count == 100

    @pytest.mark.asyncio
    async def test_generate_basic_response(self):
        from henchmen.models.llm import Message, MessageRole

        provider = self._make_provider()

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5
        mock_usage.total_tokens = 15

        mock_choice = MagicMock()
        mock_choice.message.content = "Hello from OpenAI!"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        provider._client.chat = MagicMock()
        provider._client.chat.completions = MagicMock()
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="gpt-4.1",
        )

        assert result.content == "Hello from OpenAI!"
        assert result.model == "gpt-4.1"
        assert result.finish_reason == "stop"
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5
        assert result.usage.total_tokens == 15

    @pytest.mark.asyncio
    async def test_generate_with_system_prompt(self):
        from henchmen.models.llm import Message, MessageRole

        provider = self._make_provider()

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 20
        mock_usage.completion_tokens = 4
        mock_usage.total_tokens = 24

        mock_choice = MagicMock()
        mock_choice.message.content = "Got it"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        captured_kwargs: dict = {}

        async def mock_create(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_response

        provider._client.chat = MagicMock()
        provider._client.chat.completions = MagicMock()
        provider._client.chat.completions.create = mock_create

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Go")],
            model="gpt-4.1",
            system_prompt="You are helpful.",
        )

        assert captured_kwargs["messages"][0] == {"role": "system", "content": "You are helpful."}

    @pytest.mark.asyncio
    async def test_generate_skips_system_role_messages(self):
        from henchmen.models.llm import Message, MessageRole

        provider = self._make_provider()

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 5
        mock_usage.completion_tokens = 2
        mock_usage.total_tokens = 7

        mock_choice = MagicMock()
        mock_choice.message.content = "Done"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        captured_kwargs: dict = {}

        async def mock_create(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_response

        provider._client.chat = MagicMock()
        provider._client.chat.completions = MagicMock()
        provider._client.chat.completions.create = mock_create

        await provider.generate(
            messages=[
                Message(role=MessageRole.SYSTEM, content="Ignored system msg"),
                Message(role=MessageRole.USER, content="User message"),
            ],
            model="gpt-4.1",
        )

        roles = [m["role"] for m in captured_kwargs["messages"]]
        assert "system" not in roles
        assert "user" in roles

    @pytest.mark.asyncio
    async def test_generate_with_tool_calls(self):
        import json

        from henchmen.models.llm import Message, MessageRole

        provider = self._make_provider()

        mock_tc = MagicMock()
        mock_tc.id = "call_abc123"
        mock_tc.function.name = "search"
        mock_tc.function.arguments = json.dumps({"query": "openai"})

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 15
        mock_usage.completion_tokens = 8
        mock_usage.total_tokens = 23

        mock_choice = MagicMock()
        mock_choice.message.content = ""
        mock_choice.message.tool_calls = [mock_tc]
        mock_choice.finish_reason = "tool_calls"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        provider._client.chat = MagicMock()
        provider._client.chat.completions = MagicMock()
        provider._client.chat.completions.create = AsyncMock(return_value=mock_response)

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Search for openai")],
            model="gpt-4.1",
        )

        assert result.finish_reason == "tool_use"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "call_abc123"
        assert result.tool_calls[0].name == "search"
        assert result.tool_calls[0].arguments == {"query": "openai"}


# ---------------------------------------------------------------------------
# AnthropicProvider
# ---------------------------------------------------------------------------


class TestAnthropicProvider:
    def _make_provider(self, **settings_overrides):
        settings = _settings(**settings_overrides)
        mock_client = MagicMock()
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            from henchmen.providers.anthropic import AnthropicProvider

            provider = AnthropicProvider(settings)
        provider._client = mock_client
        return provider

    def test_resolve_tier_complex(self):
        provider = self._make_provider()
        assert provider.resolve_tier(ModelTier.COMPLEX) == "claude-sonnet-4-20250514"

    def test_resolve_tier_light(self):
        provider = self._make_provider()
        assert provider.resolve_tier(ModelTier.LIGHT) == "claude-haiku-4-5-20251001"

    def test_resolve_tier_reasoning(self):
        provider = self._make_provider()
        assert provider.resolve_tier(ModelTier.REASONING) == "claude-opus-4-20250514"

    def test_resolve_tier_passthrough_unknown(self):
        provider = self._make_provider()
        assert provider.resolve_tier("claude-custom") == "claude-custom"

    def test_supported_models(self):
        provider = self._make_provider()
        models = provider.supported_models()
        assert "claude-sonnet-4-20250514" in models
        assert "claude-opus-4-20250514" in models
        assert "claude-haiku-4-5-20251001" in models

    @pytest.mark.asyncio
    async def test_count_tokens_calls_api(self):
        provider = self._make_provider()

        mock_result = MagicMock()
        mock_result.input_tokens = 7

        provider._client.messages = MagicMock()
        provider._client.messages.count_tokens = AsyncMock(return_value=mock_result)

        count = await provider.count_tokens("hello world", "claude-sonnet-4-20250514")
        assert count == 7
        provider._client.messages.count_tokens.assert_called_once_with(
            model="claude-sonnet-4-20250514",
            messages=[{"role": "user", "content": "hello world"}],
        )

    @pytest.mark.asyncio
    async def test_generate_basic_response(self):
        from henchmen.models.llm import Message, MessageRole

        provider = self._make_provider()

        mock_text_block = MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = "Hello from Anthropic!"

        mock_usage = MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 6

        mock_response = MagicMock()
        mock_response.content = [mock_text_block]
        mock_response.usage = mock_usage
        mock_response.stop_reason = "end_turn"

        provider._client.messages = MagicMock()
        provider._client.messages.create = AsyncMock(return_value=mock_response)

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="claude-sonnet-4-20250514",
        )

        assert result.content == "Hello from Anthropic!"
        assert result.model == "claude-sonnet-4-20250514"
        assert result.finish_reason == "end_turn"
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 6
        assert result.usage.total_tokens == 16

    @pytest.mark.asyncio
    async def test_generate_with_system_prompt(self):
        from henchmen.models.llm import Message, MessageRole

        provider = self._make_provider()

        mock_text_block = MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = "Acknowledged"

        mock_usage = MagicMock()
        mock_usage.input_tokens = 12
        mock_usage.output_tokens = 3

        mock_response = MagicMock()
        mock_response.content = [mock_text_block]
        mock_response.usage = mock_usage
        mock_response.stop_reason = "end_turn"

        captured_kwargs: dict = {}

        async def mock_create(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_response

        provider._client.messages = MagicMock()
        provider._client.messages.create = mock_create

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Go")],
            model="claude-sonnet-4-20250514",
            system_prompt="You are a helpful assistant.",
        )

        assert captured_kwargs.get("system") == "You are a helpful assistant."

    @pytest.mark.asyncio
    async def test_generate_skips_system_role_messages(self):
        from henchmen.models.llm import Message, MessageRole

        provider = self._make_provider()

        mock_text_block = MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = "Done"

        mock_usage = MagicMock()
        mock_usage.input_tokens = 5
        mock_usage.output_tokens = 2

        mock_response = MagicMock()
        mock_response.content = [mock_text_block]
        mock_response.usage = mock_usage
        mock_response.stop_reason = "end_turn"

        captured_kwargs: dict = {}

        async def mock_create(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_response

        provider._client.messages = MagicMock()
        provider._client.messages.create = mock_create

        await provider.generate(
            messages=[
                Message(role=MessageRole.SYSTEM, content="Ignored system msg"),
                Message(role=MessageRole.USER, content="User message"),
            ],
            model="claude-sonnet-4-20250514",
        )

        roles = [m["role"] for m in captured_kwargs["messages"]]
        assert "system" not in roles
        assert "user" in roles

    @pytest.mark.asyncio
    async def test_generate_with_tool_calls(self):
        from henchmen.models.llm import Message, MessageRole

        provider = self._make_provider()

        mock_tool_block = MagicMock()
        mock_tool_block.type = "tool_use"
        mock_tool_block.id = "toolu_01"
        mock_tool_block.name = "lookup"
        mock_tool_block.input = {"term": "anthropic"}

        mock_usage = MagicMock()
        mock_usage.input_tokens = 20
        mock_usage.output_tokens = 10

        mock_response = MagicMock()
        mock_response.content = [mock_tool_block]
        mock_response.usage = mock_usage
        mock_response.stop_reason = "tool_use"

        provider._client.messages = MagicMock()
        provider._client.messages.create = AsyncMock(return_value=mock_response)

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Look up anthropic")],
            model="claude-sonnet-4-20250514",
        )

        assert result.finish_reason == "tool_use"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "toolu_01"
        assert result.tool_calls[0].name == "lookup"
        assert result.tool_calls[0].arguments == {"term": "anthropic"}
