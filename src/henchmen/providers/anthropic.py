"""Anthropic API implementation of LLMProvider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from henchmen.models.llm import LLMResponse, Message, MessageRole, ModelTier, TokenUsage, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)


def _build_tier_defaults(settings: Settings) -> dict[str, str]:
    """Build the tier-to-model map from Settings.

    Sourcing model names from settings (L10 fix) means operators can roll to
    new Anthropic models via env var rather than a code change and redeploy.
    """
    return {
        ModelTier.COMPLEX: settings.anthropic_model_complex,
        ModelTier.LIGHT: settings.anthropic_model_light,
        ModelTier.REASONING: settings.anthropic_model_reasoning,
    }


class AnthropicProvider:
    """LLMProvider backed by the Anthropic API."""

    def __init__(self, settings: Settings) -> None:
        import anthropic

        self._client = anthropic.AsyncAnthropic(api_key=getattr(settings, "anthropic_api_key", None))
        self._tier_defaults: dict[str, str] = _build_tier_defaults(settings)
        logger.info(
            "AnthropicProvider tier mapping: complex=%s light=%s reasoning=%s",
            self._tier_defaults.get(ModelTier.COMPLEX, ""),
            self._tier_defaults.get(ModelTier.LIGHT, ""),
            self._tier_defaults.get(ModelTier.REASONING, ""),
        )

    def resolve_tier(self, tier: str) -> str:
        """Map a model tier or cloud model name to an Anthropic model.

        Scheme nodes reference cloud model names like ``gemini-2.5-pro``
        which don't exist in the Anthropic API. Remap any non-Anthropic
        model name to the COMPLEX tier default (Claude Sonnet).
        """
        # Direct tier match (e.g. "default/complex")
        if tier in self._tier_defaults:
            return self._tier_defaults[tier]
        # Already an Anthropic model name
        if tier.startswith("claude"):
            return tier
        # Non-Anthropic model name (gemini-*, gpt-*, etc.) → remap to COMPLEX
        default = self._tier_defaults.get(ModelTier.COMPLEX, "claude-sonnet-4-6-20250514")
        logger.warning(
            "[anthropic] Remapping non-Anthropic model '%s' -> '%s'",
            tier,
            default,
        )
        return default

    def supported_models(self) -> list[str]:
        """Return the list of supported Anthropic model identifiers."""
        # Dedupe while preserving order so operators see the actual tier
        # mapping first, plus any other known models.
        seen: set[str] = set()
        out: list[str] = []
        for name in (
            self._tier_defaults.get(ModelTier.COMPLEX, ""),
            self._tier_defaults.get(ModelTier.REASONING, ""),
            self._tier_defaults.get(ModelTier.LIGHT, ""),
        ):
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

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
        model = self.resolve_tier(model)
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
