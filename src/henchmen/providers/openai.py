"""OpenAI API implementation of LLMProvider."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from henchmen.models.llm import LLMResponse, Message, MessageRole, ModelTier, TokenUsage, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)


def _build_tier_defaults(settings: Settings) -> dict[str, str]:
    """Build the tier-to-model map from Settings.

    Sourcing model names from settings (L10 fix) means operators can roll to
    new OpenAI models via env var rather than a code change and redeploy.
    """
    return {
        ModelTier.COMPLEX: settings.openai_model_complex,
        ModelTier.LIGHT: settings.openai_model_light,
        ModelTier.REASONING: settings.openai_model_reasoning,
    }


class OpenAIProvider:
    """LLMProvider backed by the OpenAI API."""

    def __init__(self, settings: Settings) -> None:
        import openai

        self._client = openai.AsyncOpenAI(api_key=getattr(settings, "openai_api_key", None))
        self._tier_defaults: dict[str, str] = _build_tier_defaults(settings)
        logger.info(
            "OpenAIProvider tier mapping: complex=%s light=%s reasoning=%s",
            self._tier_defaults.get(ModelTier.COMPLEX, ""),
            self._tier_defaults.get(ModelTier.LIGHT, ""),
            self._tier_defaults.get(ModelTier.REASONING, ""),
        )

    def resolve_tier(self, tier: str) -> str:
        """Map a model tier to the default OpenAI model for that tier."""
        return self._tier_defaults.get(tier, tier)

    def supported_models(self) -> list[str]:
        """Return the list of supported OpenAI model identifiers."""
        seen: set[str] = set()
        out: list[str] = []
        for name in (
            self._tier_defaults.get(ModelTier.COMPLEX, ""),
            self._tier_defaults.get(ModelTier.LIGHT, ""),
            self._tier_defaults.get(ModelTier.REASONING, ""),
        ):
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

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
