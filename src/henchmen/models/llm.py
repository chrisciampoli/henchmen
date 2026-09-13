"""Shared data models for LLM provider interactions."""

from enum import StrEnum
from typing import Any

from pydantic import Field

from henchmen.models._base import StrictBase


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ModelTier(StrEnum):
    COMPLEX = "default/complex"
    LIGHT = "default/light"
    REASONING = "default/reasoning"


class FinishReason(StrEnum):
    """Normalised reason generation stopped.

    Every provider maps its native stop reason onto one of these values via
    ``henchmen.providers.llm_common.normalize_finish_reason`` so consumers can
    branch on truncation (``MAX_TOKENS``) or a safety decline (``REFUSAL``)
    without knowing which vendor answered.
    """

    STOP = "stop"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    ERROR = "error"


class Message(StrictBase):
    """A single message in a conversation."""

    role: MessageRole = Field(..., description="Role of the message sender")
    content: str = Field(..., description="Message text content")
    tool_call_id: str | None = Field(default=None, description="ID of the tool call this message responds to")
    tool_calls: list["ToolCall"] | None = Field(default=None, description="Tool calls made in this message")
    provider_blocks: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Provider-opaque assistant content blocks (thinking/text/tool_use) captured verbatim from a previous "
            "response. When set on an ASSISTANT message the owning provider replays them unchanged instead of "
            "rebuilding blocks from content/tool_calls, which preserves signed reasoning blocks."
        ),
    )


class ToolParameter(StrictBase):
    """A parameter definition for a tool."""

    name: str = Field(..., description="Parameter name")
    type: str = Field(..., description="JSON Schema type (string, integer, boolean, array, object)")
    description: str = Field(..., description="Human-readable description")
    required: bool = Field(default=True, description="Whether this parameter is required")
    enum: list[str] | None = Field(default=None, description="Allowed values")
    items: dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema for array element types (required by Gemini for array parameters)",
    )


class ToolDefinition(StrictBase):
    """A tool that can be called by an LLM."""

    name: str = Field(..., description="Tool name (must be unique within a request)")
    description: str = Field(..., description="What this tool does")
    parameters: list[ToolParameter] = Field(default_factory=list, description="Tool parameters")


class ToolCall(StrictBase):
    """A tool invocation from an LLM response."""

    id: str = Field(..., description="Unique call identifier")
    name: str = Field(..., description="Tool name to invoke")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Arguments to pass to the tool")


class TokenUsage(StrictBase):
    """Token consumption metrics for an LLM call.

    ``input_tokens`` is the TOTAL prompt size including tokens served from
    cache and tokens written to cache, so every provider reports the same
    quantity and ``henchmen.providers.pricing.estimate_cost`` can subtract the
    discounted slices itself.
    """

    input_tokens: int = Field(default=0, description="Total prompt tokens, including cached and cache-write tokens")
    output_tokens: int = Field(default=0, description="Output tokens generated")
    cached_tokens: int = Field(default=0, description="Input tokens served from cache (subset of input_tokens)")
    cache_write_tokens: int = Field(default=0, description="Input tokens written to cache (subset of input_tokens)")
    total_tokens: int = Field(default=0, description="Total tokens (input + output)")
    estimated_cost_usd: float = Field(default=0.0, description="Estimated cost in USD")


class LLMResponse(StrictBase):
    """Unified response from any LLM provider."""

    content: str = Field(..., description="Text content of the response")
    tool_calls: list[ToolCall] = Field(default_factory=list, description="Tool calls requested by the model")
    usage: TokenUsage = Field(default_factory=TokenUsage, description="Token usage metrics")
    model: str = Field(..., description="Concrete model that generated this response")
    finish_reason: str = Field(..., description="Normalised FinishReason: stop, tool_use, max_tokens, refusal, error")
    provider_blocks: list[dict[str, Any]] | None = Field(
        default=None,
        description="Raw assistant content blocks, to be replayed verbatim via Message.provider_blocks",
    )
