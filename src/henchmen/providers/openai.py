"""OpenAI API implementation of LLMProvider."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from henchmen.models.llm import LLMResponse, Message, MessageRole, ModelTier, TokenUsage, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

_TIER_DEFAULTS: dict[str, str] = {
    ModelTier.COMPLEX: "gpt-4.1",
    ModelTier.LIGHT: "gpt-4.1-mini",
    ModelTier.REASONING: "o3",
}


class OpenAIProvider:
    """LLMProvider backed by the OpenAI API."""

    def __init__(self, settings: Settings) -> None:
        import openai

        self._client = openai.AsyncOpenAI(api_key=getattr(settings, "openai_api_key", None))

    def resolve_tier(self, tier: str) -> str:
        """Map a model tier to the default OpenAI model for that tier."""
        return _TIER_DEFAULTS.get(tier, tier)

    def supported_models(self) -> list[str]:
        """Return the list of supported OpenAI model identifiers."""
        return ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "o3"]

    async def count_tokens(self, text: str, model: str) -> int:
        """Approximate token count using a 4-chars-per-token heuristic."""
        return len(text) // 4

    async def generate(
        self,
        messages: list[Message],
        model: str,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        system_prompt: str | None = None,
    ) -> LLMResponse:
        """Send a chat completion request to the OpenAI API."""
        oai_messages: list[dict[str, object]] = []
        if system_prompt:
            oai_messages.append({"role": "system", "content": system_prompt})
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                continue
            oai_messages.append({"role": msg.role.value, "content": msg.content})

        kwargs: dict[str, object] = {
            "model": model,
            "messages": oai_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                p.name: {"type": p.type, "description": p.description} for p in t.parameters
                            },
                            "required": [p.name for p in t.parameters if p.required],
                        },
                    },
                }
                for t in tools
            ]

        response = await self._client.chat.completions.create(**kwargs)  # type: ignore[call-overload]
        choice = response.choices[0]
        content = choice.message.content or ""
        tool_calls: list[ToolCall] = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=json.loads(tc.function.arguments),
                    )
                )

        usage = response.usage
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                total_tokens=usage.total_tokens if usage else 0,
            ),
            model=model,
            finish_reason="tool_use" if tool_calls else (choice.finish_reason or "stop"),
        )
