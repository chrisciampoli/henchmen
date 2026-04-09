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


# Sentinel used by scheme nodes that need no LLM call (e.g. eslint --fix, ruff --fix).
# Intentionally NOT a ModelTier value — there is no provider dispatch for it.
DETERMINISTIC_SENTINEL = "deterministic"


class ModelTier(StrEnum):
    COMPLEX = "default/complex"
    LIGHT = "default/light"
    REASONING = "default/reasoning"


class Message(StrictBase):
    """A single message in a conversation."""

    role: MessageRole = Field(..., description="Role of the message sender")
    content: str = Field(..., description="Message text content")
    tool_call_id: str | None = Field(default=None, description="ID of the tool call this message responds to")
    tool_calls: list["ToolCall"] | None = Field(default=None, description="Tool calls made in this message")


class ToolParameter(StrictBase):
    """A parameter definition for a tool."""

    name: str = Field(..., description="Parameter name")
    type: str = Field(..., description="JSON Schema type (string, integer, boolean, array, object)")
    description: str = Field(..., description="Human-readable description")
    required: bool = Field(default=True, description="Whether this parameter is required")
    enum: list[str] | None = Field(default=None, description="Allowed values")


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
    """Token consumption metrics for an LLM call."""

    input_tokens: int = Field(default=0, description="Input tokens consumed")
    output_tokens: int = Field(default=0, description="Output tokens generated")
    cached_tokens: int = Field(default=0, description="Input tokens served from cache")
    total_tokens: int = Field(default=0, description="Total tokens (input + output)")
    estimated_cost_usd: float = Field(default=0.0, description="Estimated cost in USD")


class LLMResponse(StrictBase):
    """Unified response from any LLM provider."""

    content: str = Field(..., description="Text content of the response")
    tool_calls: list[ToolCall] = Field(default_factory=list, description="Tool calls requested by the model")
    usage: TokenUsage = Field(default_factory=TokenUsage, description="Token usage metrics")
    model: str = Field(..., description="Model that generated this response")
    finish_reason: str = Field(..., description="Why generation stopped: stop, tool_use, max_tokens")
