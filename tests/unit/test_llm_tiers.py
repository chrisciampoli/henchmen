"""Cross-provider LLM contract tests.

Every LLM provider must (a) resolve the three ``ModelTier`` names from its own
``Settings`` fields rather than a hardcoded table, and (b) round-trip tool
history so the model sees the calls its tool results answer. Both defects
previously escaped the suite because each provider was only tested with a
concrete model name and a single user message.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

from henchmen.models.llm import (
    FinishReason,
    Message,
    MessageRole,
    ModelTier,
    ToolCall,
    ToolDefinition,
    ToolParameter,
)
from henchmen.providers.llm_common import json_schema, normalize_finish_reason, resolve_provider_model

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TIER_OVERRIDES: dict[str, dict[str, str]] = {
    "anthropic": {
        "anthropic_model_complex": "claude-tier-complex",
        "anthropic_model_light": "claude-tier-light",
        "anthropic_model_reasoning": "claude-tier-reasoning",
    },
    "openai": {
        "openai_model_complex": "gpt-tier-complex",
        "openai_model_light": "gpt-tier-light",
        "openai_model_reasoning": "gpt-tier-reasoning",
    },
    "gcp": {
        "vertex_ai_model_complex": "gemini-tier-complex",
        "vertex_ai_model_light": "gemini-tier-light",
        "vertex_ai_model_reasoning": "gemini-tier-reasoning",
    },
    "aws": {
        "bedrock_model_complex": "bedrock-tier-complex",
        "bedrock_model_light": "bedrock-tier-light",
        "bedrock_model_reasoning": "bedrock-tier-reasoning",
    },
    "local": {
        "llm_ollama_model_complex": "ollama-tier-complex",
        "llm_ollama_model_light": "ollama-tier-light",
        "llm_ollama_model_reasoning": "ollama-tier-reasoning",
    },
}

EXPECTED_PREFIX = {
    "anthropic": "claude-tier-",
    "openai": "gpt-tier-",
    "gcp": "gemini-tier-",
    "aws": "bedrock-tier-",
    "local": "ollama-tier-",
}


def _settings(**overrides):
    import os

    from henchmen.config.settings import get_settings

    os.environ.setdefault("HENCHMEN_GCP_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    return get_settings().model_copy(update=overrides)


def _make_provider(key: str, **extra_overrides):
    """Build the provider for ``key`` with its SDK client mocked out."""
    settings = _settings(**{**TIER_OVERRIDES[key], **extra_overrides})
    if key == "anthropic":
        with patch("anthropic.AsyncAnthropic", return_value=MagicMock()):
            from henchmen.providers.anthropic import AnthropicProvider

            return AnthropicProvider(settings)
    if key == "openai":
        with patch("openai.AsyncOpenAI", return_value=MagicMock()):
            from henchmen.providers.openai import OpenAIProvider

            return OpenAIProvider(settings)
    if key == "gcp":
        with patch("henchmen.providers.gcp.vertex_ai.genai", MagicMock()):
            from henchmen.providers.gcp.vertex_ai import VertexAIProvider

            return VertexAIProvider(settings)
    if key == "aws":
        # Stub the module rather than patching ``boto3.client``: test_aws_providers
        # evicts boto3 from sys.modules, so a patch target lookup can fail here.
        with patch.dict(sys.modules, {"boto3": MagicMock()}):
            from henchmen.providers.aws.bedrock import BedrockProvider

            return BedrockProvider(settings)
    if key == "local":
        from henchmen.providers.local.ollama import OllamaProvider

        return OllamaProvider(settings)
    raise AssertionError(f"unknown provider key {key!r}")


def _tool_history() -> list[Message]:
    """The message shape the operative emits after one tool round-trip."""
    return [
        Message(role=MessageRole.USER, content="Edit main.py"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="call_1", name="file_edit", arguments={"path": "main.py"})],
        ),
        Message(role=MessageRole.TOOL, content="edited", tool_call_id="call_1"),
    ]


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(TIER_OVERRIDES))
@pytest.mark.parametrize("tier", list(ModelTier))
def test_every_provider_resolves_tier_from_settings(key, tier):
    provider = _make_provider(key)
    resolved = provider.resolve_tier(tier.value)
    assert resolved == EXPECTED_PREFIX[key] + tier.name.lower()


@pytest.mark.parametrize("key", sorted(TIER_OVERRIDES))
def test_no_provider_leaks_a_tier_name_as_a_model_id(key):
    provider = _make_provider(key)
    for tier in ModelTier:
        assert provider.resolve_tier(tier.value) not in {t.value for t in ModelTier}


@pytest.mark.parametrize("key", sorted(TIER_OVERRIDES))
def test_supported_models_lists_configured_tier_models(key):
    provider = _make_provider(key)
    models = provider.supported_models()
    for tier in ModelTier:
        assert EXPECTED_PREFIX[key] + tier.name.lower() in models


def test_ollama_warns_only_when_a_tier_actually_falls_back(caplog):
    provider = _make_provider("local")
    with caplog.at_level(logging.WARNING):
        assert provider.resolve_tier(ModelTier.COMPLEX.value) == "ollama-tier-complex"
    assert not caplog.records

    fallback = _make_provider(
        "local",
        llm_ollama_model="qwen2.5-coder:7b",
        llm_ollama_model_light="",
    )
    with caplog.at_level(logging.WARNING):
        assert fallback.resolve_tier(ModelTier.LIGHT.value) == "qwen2.5-coder:7b"
    assert any("Flattening tier/model" in r.message for r in caplog.records)


def test_resolve_provider_model_rejects_unconfigured_tier():
    settings = _settings(anthropic_model_light="")
    with pytest.raises(ValueError, match="No model configured"):
        resolve_provider_model(settings, ModelTier.LIGHT.value, "anthropic")


def test_resolve_provider_model_passes_concrete_names_through():
    settings = _settings()
    assert resolve_provider_model(settings, "some-custom-model", "anthropic") == "some-custom-model"


# ---------------------------------------------------------------------------
# Tool history round-trip
# ---------------------------------------------------------------------------


def test_anthropic_tool_history_round_trip():
    from henchmen.providers.anthropic import AnthropicProvider

    payload = AnthropicProvider._build_messages(_tool_history())
    assert payload[1]["content"] == [
        {"type": "tool_use", "id": "call_1", "name": "file_edit", "input": {"path": "main.py"}}
    ]
    assert payload[2]["content"][0]["tool_use_id"] == "call_1"


def test_openai_tool_history_round_trip():
    from henchmen.providers.openai import OpenAIProvider

    payload = OpenAIProvider._build_messages(_tool_history())
    assert payload[1]["tool_calls"][0]["id"] == "call_1"
    assert payload[2]["tool_call_id"] == "call_1"


def test_bedrock_tool_history_round_trip():
    from henchmen.providers.aws.bedrock import BedrockProvider

    payload = BedrockProvider._build_messages(_tool_history())
    # user / assistant(toolUse) / user(toolResult) — alternating, no blank text.
    assert [m["role"] for m in payload] == ["user", "assistant", "user"]
    assert payload[1]["content"] == [
        {"toolUse": {"toolUseId": "call_1", "name": "file_edit", "input": {"path": "main.py"}}}
    ]
    assert payload[2]["content"][0]["toolResult"]["toolUseId"] == "call_1"
    assert all(block.get("text") != "" for m in payload for block in m["content"])


def test_bedrock_prices_calls_through_the_shared_table():
    """Bedrock used to report $0, so guardrails treated it as a free provider."""
    from henchmen.providers.aws.bedrock import BedrockProvider
    from henchmen.providers.pricing import estimate_cost

    model = "anthropic.claude-sonnet-4-20250514-v1:0"
    response = {
        "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
        "usage": {"inputTokens": 1_000_000, "outputTokens": 100_000},
        "stopReason": "end_turn",
    }
    parsed = BedrockProvider._parse_response(response, model)
    assert parsed.usage.estimated_cost_usd == pytest.approx(estimate_cost(model, 1_000_000, 100_000))
    assert parsed.usage.estimated_cost_usd > 0
    assert parsed.finish_reason == FinishReason.STOP.value


def test_ollama_tool_history_round_trip():
    from henchmen.providers.local.ollama import OllamaProvider

    payload = OllamaProvider._build_messages(_tool_history())
    assert payload[1]["tool_calls"] == [{"function": {"name": "file_edit", "arguments": {"path": "main.py"}}}]
    assert payload[2]["role"] == "tool"
    assert payload[2]["tool_name"] == "file_edit"


def test_vertex_tool_history_round_trip():
    from google.genai import types

    from henchmen.providers.gcp.vertex_ai import VertexAIProvider

    contents = VertexAIProvider._build_contents(_tool_history(), types)
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].function_call.name == "file_edit"
    assert contents[2].parts[0].function_response.response == {"result": "edited"}


@pytest.mark.parametrize("key", sorted(TIER_OVERRIDES))
def test_no_provider_emits_blank_assistant_text(key):
    """A tool-only assistant turn must never render as an empty text block."""
    provider = _make_provider(key)
    if key == "gcp":
        from google.genai import types

        contents = provider._build_contents(_tool_history(), types)
        assert all(part.text is None for content in contents for part in content.parts if part.function_call)
        assert not any(part.text == "" for content in contents for part in content.parts)
        return
    payload = provider._build_messages(_tool_history())
    for message in payload:
        content = message.get("content")
        if isinstance(content, str):
            assert content.strip() or message.get("tool_calls")
        elif isinstance(content, list):
            for block in content:
                assert block.get("text") != ""


# ---------------------------------------------------------------------------
# Shared schema + finish-reason helpers
# ---------------------------------------------------------------------------


def test_json_schema_preserves_enum_and_items():
    tool = ToolDefinition(
        name="t",
        description="d",
        parameters=[
            ToolParameter(name="mode", type="string", description="m", enum=["a", "b"]),
            ToolParameter(name="paths", type="array", description="p", required=False, items={"type": "string"}),
        ],
    )
    schema = json_schema(tool)
    assert schema["properties"]["mode"]["enum"] == ["a", "b"]
    assert schema["properties"]["paths"]["items"] == {"type": "string"}
    assert schema["required"] == ["mode"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("end_turn", FinishReason.STOP),
        ("stop", FinishReason.STOP),
        ("stop_sequence", FinishReason.STOP),
        ("length", FinishReason.MAX_TOKENS),
        ("max_tokens", FinishReason.MAX_TOKENS),
        ("FinishReason.MAX_TOKENS", FinishReason.MAX_TOKENS),
        ("tool_calls", FinishReason.TOOL_USE),
        ("refusal", FinishReason.REFUSAL),
        ("content_filter", FinishReason.REFUSAL),
        ("guardrail_intervened", FinishReason.REFUSAL),
        ("SAFETY", FinishReason.REFUSAL),
        ("MALFORMED_FUNCTION_CALL", FinishReason.ERROR),
        (None, FinishReason.STOP),
        ("something-new", FinishReason.ERROR),
    ],
)
def test_normalize_finish_reason(raw, expected):
    assert normalize_finish_reason(raw) == expected.value


def test_normalize_finish_reason_prefers_tool_use():
    assert normalize_finish_reason("end_turn", has_tool_calls=True) == FinishReason.TOOL_USE.value
