"""Tests for LLM shared data models."""

import pytest

from henchmen.models.llm import (
    FinishReason,
    LLMResponse,
    Message,
    MessageRole,
    ModelTier,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolParameter,
)


def test_message_creation():
    msg = Message(role=MessageRole.USER, content="Fix the bug")
    assert msg.role == MessageRole.USER
    assert msg.content == "Fix the bug"
    assert msg.tool_call_id is None
    assert msg.provider_blocks is None


def test_message_carries_provider_blocks():
    blocks = [{"type": "thinking", "thinking": "...", "signature": "sig"}, {"type": "text", "text": "hi"}]
    msg = Message(role=MessageRole.ASSISTANT, content="hi", provider_blocks=blocks)
    assert msg.provider_blocks == blocks


def test_tool_definition():
    tool = ToolDefinition(
        name="code_edit",
        description="Edit a file",
        parameters=[
            ToolParameter(name="file_path", type="string", description="Path to file"),
            ToolParameter(name="content", type="string", description="New content"),
        ],
    )
    assert tool.name == "code_edit"
    assert len(tool.parameters) == 2


def test_tool_parameter_enum_and_items():
    param = ToolParameter(
        name="mode",
        type="string",
        description="Edit mode",
        enum=["replace", "append"],
        items=None,
    )
    assert param.enum == ["replace", "append"]
    array_param = ToolParameter(
        name="paths",
        type="array",
        description="Files",
        items={"type": "string"},
    )
    assert array_param.items == {"type": "string"}


def test_tool_call():
    tc = ToolCall(id="call_1", name="code_edit", arguments={"file_path": "main.py", "content": "print('hi')"})
    assert tc.name == "code_edit"
    assert tc.arguments["file_path"] == "main.py"


def test_token_usage_defaults():
    usage = TokenUsage()
    assert usage.input_tokens == 0
    assert usage.cache_write_tokens == 0
    assert usage.estimated_cost_usd == 0.0


def test_token_usage_total():
    usage = TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150)
    assert usage.total_tokens == 150


def test_token_usage_tracks_cache_writes():
    usage = TokenUsage(input_tokens=1000, cached_tokens=600, cache_write_tokens=300, output_tokens=20)
    # input_tokens is the TOTAL prompt: cached + written + uncached.
    assert usage.input_tokens - usage.cached_tokens - usage.cache_write_tokens == 100


def test_llm_response():
    resp = LLMResponse(
        content="I fixed the bug",
        tool_calls=[],
        usage=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150),
        model="gemini-2.5-pro",
        finish_reason="stop",
    )
    assert resp.finish_reason == "stop"
    assert resp.usage.input_tokens == 100
    assert resp.provider_blocks is None


def test_llm_response_with_tool_calls():
    resp = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="call_1", name="code_edit", arguments={"path": "x.py"})],
        usage=TokenUsage(input_tokens=50, output_tokens=30, total_tokens=80),
        model="gpt-4o",
        finish_reason="tool_use",
    )
    assert len(resp.tool_calls) == 1
    assert resp.finish_reason == "tool_use"


def test_model_tier_values():
    assert ModelTier.COMPLEX == "default/complex"
    assert ModelTier.LIGHT == "default/light"
    assert ModelTier.REASONING == "default/reasoning"


def test_deterministic_sentinel_is_gone():
    """The sentinel duplicated ``NodeType.DETERMINISTIC`` and had no consumers."""
    import henchmen.models.llm as llm_models

    assert not hasattr(llm_models, "DETERMINISTIC_SENTINEL")


@pytest.mark.parametrize(
    "member,value",
    [
        (FinishReason.STOP, "stop"),
        (FinishReason.TOOL_USE, "tool_use"),
        (FinishReason.MAX_TOKENS, "max_tokens"),
        (FinishReason.REFUSAL, "refusal"),
        (FinishReason.ERROR, "error"),
    ],
)
def test_finish_reason_values(member, value):
    assert member == value
