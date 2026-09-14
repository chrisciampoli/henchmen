"""Tests for LLM provider request routing: foreign-model remapping, Vertex endpoint
selection and Anthropic conversation-history prompt caching."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.config.settings import Settings
from henchmen.models.llm import Message, MessageRole, ModelTier, ToolCall


def _settings(**overrides: object) -> Settings:
    """Hermetic Settings: no dotenv, no ambient HENCHMEN_* credentials needed."""
    base: dict[str, object] = {
        "gcp_project_id": "test-project",
        "gcp_region": "us-central1",
        "anthropic_api_key": "sk-ant-test",
        "openai_api_key": "sk-test",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _anthropic(**overrides: object):
    from henchmen.providers.anthropic import AnthropicProvider

    with patch("anthropic.AsyncAnthropic", return_value=MagicMock()):
        return AnthropicProvider(_settings(**overrides))


def _anthropic_response(*, input_tokens: int = 100, cache_creation: int = 0, cache_read: int = 0) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = "ok"
    block.model_dump = MagicMock(return_value={"type": "text", "text": "ok"})
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = 5
    usage.cache_creation_input_tokens = cache_creation
    usage.cache_read_input_tokens = cache_read
    response = MagicMock()
    response.content = [block]
    response.usage = usage
    response.stop_reason = "end_turn"
    return response


# ---------------------------------------------------------------------------
# Foreign model names
# ---------------------------------------------------------------------------


class TestForeignModelRemap:
    def test_openai_remaps_gemini_to_complex_tier(self, caplog: pytest.LogCaptureFixture) -> None:
        from henchmen.providers.openai import OpenAIProvider

        provider = OpenAIProvider(_settings())
        with caplog.at_level(logging.WARNING):
            assert provider.resolve_tier("gemini-2.5-pro") == provider.resolve_tier(ModelTier.COMPLEX)
        assert "Remapping foreign model 'gemini-2.5-pro'" in caplog.text

    def test_openai_remaps_claude(self) -> None:
        from henchmen.providers.openai import OpenAIProvider

        provider = OpenAIProvider(_settings(openai_model_complex="gpt-4.1"))
        assert provider.resolve_tier("claude-sonnet-5") == "gpt-4.1"

    def test_openai_keeps_native_and_custom_ids(self) -> None:
        from henchmen.providers.openai import OpenAIProvider

        provider = OpenAIProvider(_settings())
        assert provider.resolve_tier("o4-mini") == "o4-mini"
        assert provider.resolve_tier("ft:gpt-4.1:acme") == "ft:gpt-4.1:acme"

    def test_bedrock_remaps_first_party_names(self) -> None:
        from henchmen.providers.aws.bedrock import BedrockProvider

        with patch("boto3.client", return_value=MagicMock()):
            provider = BedrockProvider(_settings(bedrock_model_complex="us.anthropic.claude-sonnet-4-20250514-v1:0"))
        for foreign in ("gemini-2.5-pro", "gpt-4.1", "claude-sonnet-5"):
            assert provider.resolve_tier(foreign) == "us.anthropic.claude-sonnet-4-20250514-v1:0"
        assert provider.resolve_tier("anthropic.claude-haiku-4-5-20251001-v1:0") == (
            "anthropic.claude-haiku-4-5-20251001-v1:0"
        )
        assert provider.resolve_tier("meta.llama3-70b-instruct-v1:0") == "meta.llama3-70b-instruct-v1:0"


# ---------------------------------------------------------------------------
# Vertex AI endpoint location
# ---------------------------------------------------------------------------


class TestVertexLocation:
    @staticmethod
    def _provider():
        clients: list[MagicMock] = []

        def _make_client(**kwargs: object) -> MagicMock:
            client = MagicMock(name=f"client-{kwargs['location']}")
            client.location = kwargs["location"]
            clients.append(client)
            return client

        fake_genai = MagicMock()
        fake_genai.Client.side_effect = _make_client
        with patch("henchmen.providers.gcp.vertex_ai.genai", fake_genai):
            from henchmen.providers.gcp.vertex_ai import VertexAIProvider

            provider = VertexAIProvider(_settings())
        return provider, clients

    def test_gemini_3_routes_to_global_endpoint(self) -> None:
        provider, clients = self._provider()
        assert {c.location for c in clients} == {"us-central1", "global"}
        assert provider._client_for("gemini-3.1-pro").location == "global"
        assert provider._client_for("gemini-2.5-pro").location == "us-central1"

    @pytest.mark.asyncio
    async def test_reasoning_tier_is_sent_to_global_client(self) -> None:
        provider, _ = self._provider()
        regional = AsyncMock()
        global_ = AsyncMock(return_value=MagicMock(candidates=[], usage_metadata=None))
        provider._client.aio.models.generate_content = regional
        provider._global_client.aio.models.generate_content = global_

        await provider.generate(messages=[Message(role=MessageRole.USER, content="hi")], model=ModelTier.REASONING)

        assert global_.call_args.kwargs["model"] == "gemini-3.1-pro"
        regional.assert_not_called()


# ---------------------------------------------------------------------------
# Anthropic prompt caching over the conversation history
# ---------------------------------------------------------------------------


class TestAnthropicHistoryCaching:
    @pytest.mark.asyncio
    async def test_final_string_message_gets_breakpoint(self) -> None:
        provider = _anthropic()
        create = AsyncMock(return_value=_anthropic_response(cache_creation=50))
        provider._client.messages.create = create

        await provider.generate(messages=[Message(role=MessageRole.USER, content="Go")], model="claude-sonnet-5")

        messages = create.call_args.kwargs["messages"]
        assert messages == [
            {"role": "user", "content": [{"type": "text", "text": "Go", "cache_control": {"type": "ephemeral"}}]}
        ]

    @pytest.mark.asyncio
    async def test_breakpoint_lands_on_last_tool_result_only(self) -> None:
        provider = _anthropic()
        create = AsyncMock(return_value=_anthropic_response(cache_read=80))
        provider._client.messages.create = create
        history = [
            Message(role=MessageRole.USER, content="Edit main.py"),
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="call_1", name="file_edit", arguments={"path": "main.py"})],
            ),
            Message(role=MessageRole.TOOL, content="edited", tool_call_id="call_1"),
        ]

        await provider.generate(messages=history, model="claude-sonnet-5")

        messages = create.call_args.kwargs["messages"]
        breakpoints = [
            block
            for m in messages
            if isinstance(m["content"], list)
            for block in m["content"]
            if "cache_control" in block
        ]
        assert len(breakpoints) == 1
        assert messages[-1]["content"][-1]["type"] == "tool_result"
        assert messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_thinking_blocks_are_skipped_and_input_not_mutated(self) -> None:
        from henchmen.providers.anthropic import _with_history_breakpoint

        thinking = {"type": "thinking", "thinking": "hmm", "signature": "sig"}
        text = {"type": "text", "text": "answer"}
        original = [{"role": "assistant", "content": [text, thinking]}]

        result = _with_history_breakpoint(original)

        assert result[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in result[0]["content"][1]
        assert "cache_control" not in text
        assert original[0]["content"] == [text, thinking]

    @pytest.mark.asyncio
    async def test_warns_once_when_cache_stays_cold(self, caplog: pytest.LogCaptureFixture) -> None:
        provider = _anthropic()
        provider._client.messages.create = AsyncMock(return_value=_anthropic_response())
        call = {"messages": [Message(role=MessageRole.USER, content="Go")], "model": "claude-sonnet-5"}

        with caplog.at_level(logging.WARNING):
            await provider.generate(**call)
            assert "no cache writes or reads" not in caplog.text
            for _ in range(3):
                await provider.generate(**call)

        assert caplog.text.count("no cache writes or reads") == 1
