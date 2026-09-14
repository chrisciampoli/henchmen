"""Anthropic API implementation of LLMProvider."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from henchmen.models.llm import (
    FinishReason,
    LLMResponse,
    Message,
    MessageRole,
    ModelTier,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)
from henchmen.providers.llm_common import json_schema, normalize_finish_reason, resolve_provider_model
from henchmen.providers.pricing import estimate_cost
from henchmen.providers.tiers import is_tier_name, tier_models

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

PROVIDER_NAME = "anthropic"

# Current Claude models (Sonnet 5, Opus 5, Opus 4.8/4.7/4.6, Sonnet 4.6, ...)
# reject `temperature` with a 400. Only the older families below still accept
# a sampling parameter, so temperature is sent to those and omitted elsewhere.
_TEMPERATURE_PREFIXES: tuple[str, ...] = ("claude-3", "claude-haiku-4-5")
_TEMPERATURE_PATTERN = re.compile(r"^claude-(?:sonnet|opus)-4-2025")


def _supports_temperature(model: str) -> bool:
    """True when ``model`` belongs to a family that still accepts ``temperature``."""
    name = (model or "").strip().lower()
    return name.startswith(_TEMPERATURE_PREFIXES) or bool(_TEMPERATURE_PATTERN.match(name))


# Thinking blocks cannot carry cache_control, so the history breakpoint goes on
# the last block of any other type.
_UNCACHEABLE_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})

# Calls in a row with zero cache activity before the one-shot warning fires.
_UNCACHED_CALLS_BEFORE_WARNING = 2


def _with_history_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``messages`` with an ephemeral cache breakpoint on the final message.

    The input is not mutated: messages may alias ``Message.provider_blocks``
    that the caller replays on later turns, and a stale breakpoint left on an
    older message would count against Anthropic's four-breakpoint limit.
    """
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        if not content:
            return messages
        blocks: list[Any] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list):
        blocks = list(content)
        for idx in range(len(blocks) - 1, -1, -1):
            block = blocks[idx]
            if isinstance(block, dict) and block.get("type") not in _UNCACHEABLE_BLOCK_TYPES:
                blocks[idx] = {**block, "cache_control": {"type": "ephemeral"}}
                break
        else:
            return messages
    else:
        return messages
    return [*messages[:-1], {**last, "content": blocks}]


class AnthropicProvider:
    """LLMProvider backed by the Anthropic API."""

    def __init__(self, settings: Settings) -> None:
        import anthropic

        self._settings = settings
        # An empty string would disable the SDK's own ANTHROPIC_API_KEY lookup,
        # so pass None when the setting is unset and let the SDK resolve it.
        api_key = settings.anthropic_api_key.strip() or None
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._uncached_calls = 0
        self._warned_uncached = False
        models = tier_models(settings, PROVIDER_NAME)
        logger.info(
            "AnthropicProvider tier mapping: complex=%s light=%s reasoning=%s",
            models.get(ModelTier.COMPLEX, ""),
            models.get(ModelTier.LIGHT, ""),
            models.get(ModelTier.REASONING, ""),
        )

    def resolve_tier(self, tier: str) -> str:
        """Map a model tier or foreign model name to a concrete Anthropic model.

        Scheme nodes may still reference cloud model names like ``gemini-2.5-pro``
        which do not exist in the Anthropic API; those are remapped to the
        COMPLEX tier with a warning so the mismatch is visible.
        """
        if is_tier_name(tier):
            return resolve_provider_model(self._settings, tier, PROVIDER_NAME)
        if tier.startswith("claude"):
            return tier
        default = resolve_provider_model(self._settings, ModelTier.COMPLEX.value, PROVIDER_NAME)
        logger.warning("[anthropic] Remapping non-Anthropic model '%s' -> '%s'", tier, default)
        return default

    def supported_models(self) -> list[str]:
        """Return the configured tier models, deduped and in tier order."""
        models = tier_models(self._settings, PROVIDER_NAME)
        seen: set[str] = set()
        out: list[str] = []
        for tier in (ModelTier.COMPLEX, ModelTier.REASONING, ModelTier.LIGHT):
            name = models.get(tier, "")
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    async def count_tokens(self, text: str, model: str) -> int:
        """Count tokens using the Anthropic token counting API."""
        result = await self._client.messages.count_tokens(
            model=self.resolve_tier(model),
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
        merged = self._build_messages(messages)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": merged,
            "max_tokens": max_tokens,
        }
        if _supports_temperature(model):
            kwargs["temperature"] = temperature

        # Prompt caching: the request renders as tools -> system -> messages, so
        # a breakpoint on the last tool caches the whole tool list and one on the
        # system prompt extends the cached prefix through it.
        if system_prompt:
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        if tools:
            tool_params: list[dict[str, Any]] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": json_schema(t),
                }
                for t in tools
            ]
            tool_params[-1]["cache_control"] = {"type": "ephemeral"}
            kwargs["tools"] = tool_params

        # The conversation history (tool calls, tool outputs, diffs) is the
        # bulk of an agentic loop's prompt and grows every step. A third
        # breakpoint on the final message caches that prefix too, so the next
        # turn re-reads it at the cache-read rate instead of full price.
        kwargs["messages"] = _with_history_breakpoint(merged)

        response = await self._client.messages.create(**kwargs)

        stop_reason = getattr(response, "stop_reason", None)
        refused = stop_reason == "refusal"
        if refused:
            details = getattr(response, "stop_details", None)
            logger.warning(
                "[anthropic] Model %s declined the request: category=%s explanation=%s",
                model,
                getattr(details, "category", None),
                getattr(details, "explanation", None),
            )

        content = ""
        tool_calls: list[ToolCall] = []
        provider_blocks: list[dict[str, Any]] = []
        for block in response.content:
            provider_blocks.append(self._block_to_dict(block))
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input))
        if refused:
            # A refusal carries no usable assistant turn; do not replay it.
            content = ""
            tool_calls = []
            provider_blocks = []

        # `usage.input_tokens` is the UNCACHED remainder on Anthropic; the cache
        # counters are reported separately. TokenUsage.input_tokens is the total
        # prompt, so add them back before costing or context accounting.
        cache_creation = int(getattr(response.usage, "cache_creation_input_tokens", 0) or 0)
        cache_read = int(getattr(response.usage, "cache_read_input_tokens", 0) or 0)
        uncached_input = int(response.usage.input_tokens or 0)
        input_tokens = uncached_input + cache_creation + cache_read
        output_tokens = int(response.usage.output_tokens or 0)
        if cache_creation or cache_read:
            self._uncached_calls = 0
            logger.info(
                "[anthropic] Cache: created=%d read=%d uncached=%d total_input=%d",
                cache_creation,
                cache_read,
                uncached_input,
                input_tokens,
            )
        elif input_tokens:
            self._note_uncached_call(model, input_tokens)

        cost = estimate_cost(
            model,
            input_tokens,
            output_tokens,
            cached_input_tokens=cache_read,
            cache_write_tokens=cache_creation,
        )

        finish_reason = (
            FinishReason.REFUSAL.value
            if refused
            else normalize_finish_reason(stop_reason, has_tool_calls=bool(tool_calls))
        )
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cache_read,
                cache_write_tokens=cache_creation,
                total_tokens=input_tokens + output_tokens,
                estimated_cost_usd=cost,
            ),
            model=model,
            finish_reason=finish_reason,
            provider_blocks=provider_blocks or None,
        )

    def _note_uncached_call(self, model: str, input_tokens: int) -> None:
        """Warn once when consecutive calls neither write nor read the prompt cache.

        A silent no-op cache is usually a prompt below the model's minimum
        cacheable prefix, or a prefix that changes between calls; either way
        every input token is billed at full price.
        """
        self._uncached_calls += 1
        if self._uncached_calls < _UNCACHED_CALLS_BEFORE_WARNING or self._warned_uncached:
            return
        self._warned_uncached = True
        logger.warning(
            "[anthropic] %d consecutive %s calls reported no cache writes or reads (last prompt=%d tokens). "
            "The prompt is likely below the model's minimum cacheable length, or its prefix is not "
            "byte-stable between calls; input tokens are being billed at full price.",
            self._uncached_calls,
            model,
            input_tokens,
        )

    @staticmethod
    def _block_to_dict(block: Any) -> dict[str, Any]:
        """Serialise a response content block for verbatim replay."""
        dump = getattr(block, "model_dump", None)
        if callable(dump):
            result = dump(exclude_none=True)
            if isinstance(result, dict):
                return result
        return {"type": getattr(block, "type", "text"), "text": getattr(block, "text", "")}

    @staticmethod
    def _build_messages(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert henchmen messages to the Anthropic messages payload."""
        ant_messages: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                continue
            if msg.role == MessageRole.TOOL:
                # Anthropic expects tool results as role=user with tool_result blocks
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
            elif msg.role == MessageRole.ASSISTANT and msg.provider_blocks:
                # Replay the model's own blocks verbatim so thinking/tool_use
                # blocks survive the round trip instead of being reconstructed.
                ant_messages.append({"role": "assistant", "content": list(msg.provider_blocks)})
            elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                content_blocks: list[dict[str, Any]] = []
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
            elif not msg.content.strip():
                # Anthropic rejects blank text content; an empty turn carries nothing.
                continue
            else:
                ant_messages.append({"role": msg.role.value, "content": msg.content})

        # Anthropic requires every tool_result to have a corresponding tool_use
        # in a preceding assistant message. Context window trimming can orphan
        # tool_results by dropping their parent assistant message.
        tool_use_ids: set[str] = set()
        for m in ant_messages:
            content = m.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_use_ids.add(str(block.get("id", "")))

        cleaned: list[dict[str, Any]] = []
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
        merged: list[dict[str, Any]] = []
        for m in cleaned:
            if merged and merged[-1].get("role") == m.get("role"):
                prev_content = merged[-1].get("content")
                curr_content = m.get("content")
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    merged[-1] = {**merged[-1], "content": f"{prev_content}\n{curr_content}"}
                elif isinstance(prev_content, list) and isinstance(curr_content, list):
                    merged[-1] = {**merged[-1], "content": prev_content + curr_content}
                else:
                    merged.append(m)
            else:
                merged.append(m)
        return merged
