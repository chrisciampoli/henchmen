"""Anthropic API implementation of LLMProvider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from henchmen.models.llm import LLMResponse, Message, MessageRole, ModelTier, TokenUsage, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

_TIER_DEFAULTS: dict[str, str] = {
    ModelTier.COMPLEX: "claude-sonnet-4-6-20250514",
    ModelTier.LIGHT: "claude-haiku-4-5-20251001",
    ModelTier.REASONING: "claude-opus-4-6-20250514",
}


class AnthropicProvider:
    """LLMProvider backed by the Anthropic API."""

    def __init__(self, settings: Settings) -> None:
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=getattr(settings, "anthropic_api_key", None))

    def resolve_tier(self, tier: str) -> str:
        """Map a model tier to the default Anthropic model for that tier."""
        return _TIER_DEFAULTS.get(tier, tier)

    def supported_models(self) -> list[str]:
        """Return the list of supported Anthropic model identifiers."""
        return ["claude-sonnet-4-6-20250514", "claude-opus-4-6-20250514", "claude-haiku-4-5-20251001"]

    async def count_tokens(self, text: str, model: str) -> int:
        """Count tokens using the Anthropic token counting API."""
        result = await self._client.messages.count_tokens(
            model=model,
            messages=[{"role": "user", "content": text}],
        )
        return result.input_tokens

    async def generate(
        self,
        messages: list[Message],
        model: str,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        system_prompt: str | None = None,
    ) -> LLMResponse:
        """Send a messages request to the Anthropic API."""
        ant_messages: list[dict[str, object]] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                continue
            ant_messages.append({"role": msg.role.value, "content": msg.content})

        kwargs: dict[str, object] = {
            "model": model,
            "messages": ant_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": {
                        "type": "object",
                        "properties": {p.name: {"type": p.type, "description": p.description} for p in t.parameters},
                        "required": [p.name for p in t.parameters if p.required],
                    },
                }
                for t in tools
            ]

        response = await self._client.messages.create(**kwargs)  # type: ignore[call-overload]
        content = ""
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input))

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                total_tokens=response.usage.input_tokens + response.usage.output_tokens,
            ),
            model=model,
            finish_reason="tool_use" if tool_calls else response.stop_reason or "stop",
        )
