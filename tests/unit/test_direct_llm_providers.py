"""Tests for OpenAI and Anthropic direct API LLM providers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.models.llm import Message, MessageRole, ModelTier, ToolCall, ToolDefinition, ToolParameter

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


def _tool_history() -> list[Message]:
    """The message shape the operative produces after one tool round-trip."""
    return [
        Message(role=MessageRole.USER, content="Edit main.py"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="call_1", name="file_edit", arguments={"path": "main.py"})],
        ),
        Message(role=MessageRole.TOOL, content="edited", tool_call_id="call_1"),
    ]


def _enum_tool() -> ToolDefinition:
    return ToolDefinition(
        name="file_edit",
        description="Edit a file",
        parameters=[
            ToolParameter(name="mode", type="string", description="Edit mode", enum=["replace", "append"]),
            ToolParameter(name="paths", type="array", description="Files", required=False, items={"type": "string"}),
        ],
    )


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

    @staticmethod
    def _mock_completion(content="ok", tool_calls=None, finish_reason="stop", cached_tokens=0):
        usage = MagicMock()
        usage.prompt_tokens = 10
        usage.completion_tokens = 5
        usage.total_tokens = 15
        usage.prompt_tokens_details = MagicMock(cached_tokens=cached_tokens)

        choice = MagicMock()
        choice.message.content = content
        choice.message.tool_calls = tool_calls
        choice.finish_reason = finish_reason

        response = MagicMock()
        response.choices = [choice]
        response.usage = usage
        return response

    def _capture(self, provider, response):
        captured: dict = {}

        async def mock_create(**kwargs):
            captured.update(kwargs)
            return response

        provider._client.chat = MagicMock()
        provider._client.chat.completions = MagicMock()
        provider._client.chat.completions.create = mock_create
        return captured

    def test_resolve_tier_complex(self):
        provider = self._make_provider()
        assert provider.resolve_tier(ModelTier.COMPLEX) == "gpt-4.1"

    def test_resolve_tier_light(self):
        provider = self._make_provider()
        assert provider.resolve_tier(ModelTier.LIGHT) == "gpt-4.1-mini"

    def test_resolve_tier_reasoning(self):
        provider = self._make_provider()
        assert provider.resolve_tier(ModelTier.REASONING) == "o3"

    def test_resolve_tier_reads_settings_override(self):
        provider = self._make_provider(openai_model_complex="gpt-4.1-turbo-custom")
        assert provider.resolve_tier(ModelTier.COMPLEX) == "gpt-4.1-turbo-custom"

    def test_resolve_tier_passthrough_unknown(self):
        provider = self._make_provider()
        assert provider.resolve_tier("gpt-custom") == "gpt-custom"

    def test_resolve_tier_unconfigured_raises(self):
        provider = self._make_provider(openai_model_light="")
        with pytest.raises(ValueError, match="No model configured"):
            provider.resolve_tier(ModelTier.LIGHT)

    def test_supported_models(self):
        provider = self._make_provider()
        models = provider.supported_models()
        assert "gpt-4.1" in models
        assert "gpt-4.1-mini" in models
        assert "o3" in models

    def test_empty_api_key_falls_back_to_sdk_env_lookup(self):
        settings = _settings(openai_api_key="")
        from henchmen.providers.openai import OpenAIProvider

        provider = OpenAIProvider(settings)
        with patch("openai.AsyncOpenAI") as ctor:
            provider._openai_client()
        assert ctor.call_args.kwargs["api_key"] is None

    def test_client_is_built_lazily(self):
        """The registry constructs every provider up front; an absent key must not crash that."""
        settings = _settings(openai_api_key="")
        with patch("openai.AsyncOpenAI", side_effect=AssertionError("client built eagerly")):
            from henchmen.providers.openai import OpenAIProvider

            provider = OpenAIProvider(settings)
        assert provider._client is None

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
        provider = self._make_provider()
        provider._client.chat = MagicMock()
        provider._client.chat.completions = MagicMock()
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._mock_completion(content="Hello from OpenAI!")
        )

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
        assert result.usage.estimated_cost_usd > 0

    @pytest.mark.asyncio
    async def test_generate_resolves_tier_name(self):
        """A scheme node passing ``default/complex`` must not reach the API as a model id."""
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_completion())

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model=ModelTier.COMPLEX.value,
        )

        assert captured["model"] == "gpt-4.1"
        assert result.model == "gpt-4.1"

    @pytest.mark.asyncio
    async def test_generate_with_system_prompt(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_completion(content="Got it"))

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Go")],
            model="gpt-4.1",
            system_prompt="You are helpful.",
        )

        assert captured["messages"][0] == {"role": "system", "content": "You are helpful."}

    @pytest.mark.asyncio
    async def test_generate_skips_system_role_messages(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_completion(content="Done"))

        await provider.generate(
            messages=[
                Message(role=MessageRole.SYSTEM, content="Ignored system msg"),
                Message(role=MessageRole.USER, content="User message"),
            ],
            model="gpt-4.1",
        )

        roles = [m["role"] for m in captured["messages"]]
        assert "system" not in roles
        assert "user" in roles

    @pytest.mark.asyncio
    async def test_generate_with_tool_calls(self):
        import json

        provider = self._make_provider()

        mock_tc = MagicMock()
        mock_tc.id = "call_abc123"
        mock_tc.function.name = "search"
        mock_tc.function.arguments = json.dumps({"query": "openai"})

        provider._client.chat = MagicMock()
        provider._client.chat.completions = MagicMock()
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._mock_completion(content="", tool_calls=[mock_tc], finish_reason="tool_calls")
        )

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Search for openai")],
            model="gpt-4.1",
        )

        assert result.finish_reason == "tool_use"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "call_abc123"
        assert result.tool_calls[0].name == "search"
        assert result.tool_calls[0].arguments == {"query": "openai"}

    @pytest.mark.asyncio
    async def test_generate_replays_tool_history(self):
        """Assistant tool_calls and tool_call_id must survive: the API 400s without them."""
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_completion())

        await provider.generate(messages=_tool_history(), model="gpt-4.1")

        assistant = captured["messages"][1]
        assert assistant["role"] == "assistant"
        assert assistant["tool_calls"][0]["id"] == "call_1"
        assert assistant["tool_calls"][0]["function"]["name"] == "file_edit"
        tool_msg = captured["messages"][2]
        assert tool_msg == {"role": "tool", "tool_call_id": "call_1", "content": "edited"}

    @pytest.mark.asyncio
    async def test_generate_reasoning_model_omits_temperature_and_max_tokens(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_completion())

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Think")],
            model=ModelTier.REASONING.value,
            max_tokens=4096,
        )

        assert captured["model"] == "o3"
        assert captured["max_completion_tokens"] == 4096
        assert "max_tokens" not in captured
        assert "temperature" not in captured

    @pytest.mark.asyncio
    async def test_generate_non_reasoning_model_sends_max_tokens(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_completion())

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Go")],
            model="gpt-4.1",
            max_tokens=512,
        )

        assert captured["max_tokens"] == 512
        assert captured["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_generate_reports_cached_tokens(self):
        provider = self._make_provider()
        provider._client.chat = MagicMock()
        provider._client.chat.completions = MagicMock()
        provider._client.chat.completions.create = AsyncMock(return_value=self._mock_completion(cached_tokens=8))

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="gpt-4.1",
        )
        assert result.usage.cached_tokens == 8

    @pytest.mark.asyncio
    async def test_generate_forwards_enum_and_items(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_completion())

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Go")],
            model="gpt-4.1",
            tools=[_enum_tool()],
        )

        params = captured["tools"][0]["function"]["parameters"]
        assert params["properties"]["mode"]["enum"] == ["replace", "append"]
        assert params["properties"]["paths"]["items"] == {"type": "string"}
        assert params["required"] == ["mode"]

    @pytest.mark.asyncio
    async def test_generate_normalizes_length_finish_reason(self):
        provider = self._make_provider()
        provider._client.chat = MagicMock()
        provider._client.chat.completions = MagicMock()
        provider._client.chat.completions.create = AsyncMock(return_value=self._mock_completion(finish_reason="length"))

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="gpt-4.1",
        )
        assert result.finish_reason == "max_tokens"


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

    @staticmethod
    def _mock_message(
        blocks=None,
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=6,
        cache_creation=0,
        cache_read=0,
        stop_details=None,
    ):
        if blocks is None:
            text_block = MagicMock()
            text_block.type = "text"
            text_block.text = "Hello from Anthropic!"
            text_block.model_dump = MagicMock(return_value={"type": "text", "text": "Hello from Anthropic!"})
            blocks = [text_block]

        usage = MagicMock()
        usage.input_tokens = input_tokens
        usage.output_tokens = output_tokens
        usage.cache_creation_input_tokens = cache_creation
        usage.cache_read_input_tokens = cache_read

        response = MagicMock()
        response.content = blocks
        response.usage = usage
        response.stop_reason = stop_reason
        response.stop_details = stop_details
        return response

    def _capture(self, provider, response):
        captured: dict = {}

        async def mock_create(**kwargs):
            captured.update(kwargs)
            return response

        provider._client.messages = MagicMock()
        provider._client.messages.create = mock_create
        return captured

    def test_resolve_tier_complex(self):
        provider = self._make_provider()
        assert provider.resolve_tier(ModelTier.COMPLEX) == "claude-sonnet-5"

    def test_resolve_tier_light(self):
        provider = self._make_provider()
        assert provider.resolve_tier(ModelTier.LIGHT) == "claude-haiku-4-5"

    def test_resolve_tier_reasoning(self):
        provider = self._make_provider()
        assert provider.resolve_tier(ModelTier.REASONING) == "claude-opus-5"

    def test_resolve_tier_reads_settings_override(self):
        provider = self._make_provider(anthropic_model_reasoning="claude-opus-4-8")
        assert provider.resolve_tier(ModelTier.REASONING) == "claude-opus-4-8"

    def test_resolve_tier_passthrough_unknown(self):
        provider = self._make_provider()
        assert provider.resolve_tier("claude-custom") == "claude-custom"

    def test_resolve_tier_remaps_foreign_model_to_configured_complex(self):
        """The old fallback named a model id that does not exist; the COMPLEX setting wins."""
        provider = self._make_provider(anthropic_model_complex="claude-sonnet-5")
        assert provider.resolve_tier("gemini-2.5-pro") == "claude-sonnet-5"

    def test_resolve_tier_unconfigured_raises(self):
        provider = self._make_provider(anthropic_model_complex="")
        with pytest.raises(ValueError, match="No model configured"):
            provider.resolve_tier(ModelTier.COMPLEX)

    def test_supported_models(self):
        provider = self._make_provider()
        models = provider.supported_models()
        assert "claude-sonnet-5" in models
        assert "claude-opus-5" in models
        assert "claude-haiku-4-5" in models

    def test_empty_api_key_falls_back_to_sdk_env_lookup(self):
        settings = _settings(anthropic_api_key="")
        with patch("anthropic.AsyncAnthropic") as ctor:
            from henchmen.providers.anthropic import AnthropicProvider

            AnthropicProvider(settings)
        assert ctor.call_args.kwargs["api_key"] is None

    @pytest.mark.asyncio
    async def test_count_tokens_calls_api(self):
        provider = self._make_provider()

        mock_result = MagicMock()
        mock_result.input_tokens = 7

        provider._client.messages = MagicMock()
        provider._client.messages.count_tokens = AsyncMock(return_value=mock_result)

        count = await provider.count_tokens("hello world", "claude-sonnet-5")
        assert count == 7
        provider._client.messages.count_tokens.assert_called_once_with(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "hello world"}],
        )

    @pytest.mark.asyncio
    async def test_count_tokens_resolves_tier(self):
        provider = self._make_provider()

        mock_result = MagicMock()
        mock_result.input_tokens = 3
        provider._client.messages = MagicMock()
        provider._client.messages.count_tokens = AsyncMock(return_value=mock_result)

        await provider.count_tokens("hi", ModelTier.LIGHT.value)
        assert provider._client.messages.count_tokens.call_args.kwargs["model"] == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_generate_basic_response(self):
        provider = self._make_provider()
        provider._client.messages = MagicMock()
        provider._client.messages.create = AsyncMock(return_value=self._mock_message())

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="claude-sonnet-5",
        )

        assert result.content == "Hello from Anthropic!"
        assert result.model == "claude-sonnet-5"
        assert result.finish_reason == "stop"
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 6
        assert result.usage.total_tokens == 16

    @pytest.mark.asyncio
    async def test_generate_resolves_tier_name(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_message())

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model=ModelTier.REASONING.value,
        )

        assert captured["model"] == "claude-opus-5"
        assert result.model == "claude-opus-5"

    @pytest.mark.asyncio
    async def test_generate_with_system_prompt_uses_prompt_caching(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_message(cache_creation=100))

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Go")],
            model="claude-sonnet-5",
            system_prompt="You are a helpful assistant.",
        )

        system_val = captured.get("system")
        assert isinstance(system_val, list), "system prompt should be a list for prompt caching"
        assert len(system_val) == 1
        assert system_val[0]["type"] == "text"
        assert system_val[0]["text"] == "You are a helpful assistant."
        assert system_val[0]["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("system_prompt", [None, ""])
    async def test_generate_without_system_prompt_sends_no_system_kwarg(self, system_prompt):
        """The Messages API rejects ``system=None``; an absent prompt must omit the key entirely."""
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_message())

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Go")],
            model="claude-sonnet-5",
            system_prompt=system_prompt,
        )

        assert "system" not in captured

    @pytest.mark.asyncio
    async def test_generate_caches_tool_list(self):
        """Tools render before the system prompt, so the last tool carries the breakpoint."""
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_message())

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Go")],
            model="claude-sonnet-5",
            tools=[_enum_tool(), ToolDefinition(name="other", description="Other", parameters=[])],
        )

        tools = captured["tools"]
        assert "cache_control" not in tools[0]
        assert tools[-1]["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_generate_forwards_enum_and_items(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_message())

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Go")],
            model="claude-sonnet-5",
            tools=[_enum_tool()],
        )

        schema = captured["tools"][0]["input_schema"]
        assert schema["properties"]["mode"]["enum"] == ["replace", "append"]
        assert schema["properties"]["paths"]["items"] == {"type": "string"}

    @pytest.mark.asyncio
    async def test_generate_counts_cache_tokens_in_input_total(self):
        """`usage.input_tokens` is the uncached remainder on Anthropic; TokenUsage wants the total."""
        provider = self._make_provider()
        provider._client.messages = MagicMock()
        provider._client.messages.create = AsyncMock(
            return_value=self._mock_message(input_tokens=50, output_tokens=10, cache_read=200, cache_creation=30)
        )

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="claude-sonnet-5",
            system_prompt="System prompt to cache",
        )

        assert result.usage.input_tokens == 280
        assert result.usage.cached_tokens == 200
        assert result.usage.cache_write_tokens == 30
        assert result.usage.total_tokens == 290
        assert result.usage.estimated_cost_usd > 0

    @pytest.mark.asyncio
    async def test_generate_cost_matches_shared_price_table(self):
        from henchmen.providers.pricing import estimate_cost

        provider = self._make_provider()
        provider._client.messages = MagicMock()
        provider._client.messages.create = AsyncMock(
            return_value=self._mock_message(
                input_tokens=1_000_000, output_tokens=1_000_000, cache_read=1_000_000, cache_creation=1_000_000
            )
        )

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="claude-sonnet-5",
        )

        expected = estimate_cost(
            "claude-sonnet-5", 3_000_000, 1_000_000, cached_input_tokens=1_000_000, cache_write_tokens=1_000_000
        )
        assert result.usage.estimated_cost_usd == pytest.approx(expected)
        # Sonnet 5: $2/M in, $10/M out, cache read 10%, cache write 125%.
        assert expected == pytest.approx(2.0 + 10.0 + 0.2 + 2.5)

    @pytest.mark.asyncio
    async def test_generate_omits_temperature_for_current_models(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_message())

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="claude-sonnet-5",
            temperature=0.0,
        )
        assert "temperature" not in captured

    @pytest.mark.asyncio
    async def test_generate_sends_temperature_for_older_families(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_message())

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="claude-haiku-4-5",
            temperature=0.0,
        )
        assert captured["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_generate_forwards_max_tokens(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_message())

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="claude-sonnet-5",
            max_tokens=1234,
        )
        assert captured["max_tokens"] == 1234

    @pytest.mark.asyncio
    async def test_generate_handles_refusal(self):
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "partial"
        text_block.model_dump = MagicMock(return_value={"type": "text", "text": "partial"})

        provider = self._make_provider()
        provider._client.messages = MagicMock()
        provider._client.messages.create = AsyncMock(
            return_value=self._mock_message(
                blocks=[text_block],
                stop_reason="refusal",
                stop_details=MagicMock(category="cyber", explanation="declined"),
            )
        )

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="claude-sonnet-5",
        )

        assert result.finish_reason == "refusal"
        assert result.content == ""
        assert result.tool_calls == []
        assert result.provider_blocks is None

    @pytest.mark.asyncio
    async def test_generate_skips_system_role_messages(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_message())

        await provider.generate(
            messages=[
                Message(role=MessageRole.SYSTEM, content="Ignored system msg"),
                Message(role=MessageRole.USER, content="User message"),
            ],
            model="claude-sonnet-5",
        )

        roles = [m["role"] for m in captured["messages"]]
        assert "system" not in roles
        assert "user" in roles

    @pytest.mark.asyncio
    async def test_generate_with_tool_calls(self):
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "toolu_01"
        tool_block.name = "lookup"
        tool_block.input = {"term": "anthropic"}
        tool_block.model_dump = MagicMock(
            return_value={"type": "tool_use", "id": "toolu_01", "name": "lookup", "input": {"term": "anthropic"}}
        )

        provider = self._make_provider()
        provider._client.messages = MagicMock()
        provider._client.messages.create = AsyncMock(
            return_value=self._mock_message(blocks=[tool_block], stop_reason="tool_use")
        )

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Look up anthropic")],
            model="claude-sonnet-5",
        )

        assert result.finish_reason == "tool_use"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "toolu_01"
        assert result.tool_calls[0].name == "lookup"
        assert result.tool_calls[0].arguments == {"term": "anthropic"}
        assert result.provider_blocks == [
            {"type": "tool_use", "id": "toolu_01", "name": "lookup", "input": {"term": "anthropic"}}
        ]

    @pytest.mark.asyncio
    async def test_generate_replays_tool_history(self):
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_message())

        await provider.generate(messages=_tool_history(), model="claude-sonnet-5")

        assistant = captured["messages"][1]
        assert assistant["role"] == "assistant"
        assert assistant["content"] == [
            {"type": "tool_use", "id": "call_1", "name": "file_edit", "input": {"path": "main.py"}}
        ]
        tool_result = captured["messages"][2]
        assert tool_result["role"] == "user"
        assert tool_result["content"][0]["tool_use_id"] == "call_1"

    @pytest.mark.asyncio
    async def test_generate_replays_provider_blocks_verbatim(self):
        """Thinking blocks must survive the round trip instead of being rebuilt from text."""
        blocks = [
            {"type": "thinking", "thinking": "step 1", "signature": "sig-abc"},
            {"type": "tool_use", "id": "call_1", "name": "file_edit", "input": {"path": "main.py"}},
        ]
        provider = self._make_provider()
        captured = self._capture(provider, self._mock_message())

        await provider.generate(
            messages=[
                Message(role=MessageRole.USER, content="Edit main.py"),
                Message(
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_calls=[ToolCall(id="call_1", name="file_edit", arguments={"path": "main.py"})],
                    provider_blocks=blocks,
                ),
                Message(role=MessageRole.TOOL, content="edited", tool_call_id="call_1"),
            ],
            model="claude-sonnet-5",
        )

        assert captured["messages"][1]["content"] == blocks
