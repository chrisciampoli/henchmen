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
            if msg.role == MessageRole.TOOL:
                # Anthropic expects tool results as role=user with tool_result content blocks
                ant_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id or "unknown",
                                "content": msg.content,
                            }
                        ],
                    }
                )
            elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                # Assistant messages with tool calls need content blocks
                content_blocks: list[dict[str, object]] = []
                if msg.content:
                    content_blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                ant_messages.append({"role": "assistant", "content": content_blocks})
            else:
                ant_messages.append({"role": msg.role.value, "content": msg.content})

        # Anthropic requires every tool_result to have a corresponding tool_use
        # in the immediately preceding assistant message. Context window trimming
        # can orphan tool_results by dropping their parent assistant message.
        # Collect all tool_use IDs and strip orphaned tool_results.
        tool_use_ids: set[str] = set()
        for m in ant_messages:
            content = m.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_use_ids.add(str(block.get("id", "")))

        cleaned: list[dict[str, object]] = []
        for m in ant_messages:
            content = m.get("content")
            if isinstance(content, list):
                filtered = [
                    b
                    for b in content
                    if not (
                        isinstance(b, dict)
                        and b.get("type") == "tool_result"
                        and str(b.get("tool_use_id", "")) not in tool_use_ids
                    )
                ]
                if not filtered:
                    continue  # Drop entirely empty messages
                m = {**m, "content": filtered}
            cleaned.append(m)

        # Anthropic requires alternating user/assistant roles. Merge consecutive same-role.
        merged: list[dict[str, object]] = []
        for m in cleaned:
            if merged and merged[-1].get("role") == m.get("role"):
                prev_content = merged[-1].get("content")
                curr_content = m.get("content")
                # Merge text content
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    merged[-1] = {**merged[-1], "content": f"{prev_content}\n{curr_content}"}
                elif isinstance(prev_content, list) and isinstance(curr_content, list):
                    merged[-1] = {**merged[-1], "content": prev_content + curr_content}
                else:
                    merged.append(m)
            else:
                merged.append(m)

        kwargs: dict[str, object] = {
            "model": model,
            "messages": merged,
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
