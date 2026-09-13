"""OpenAI API implementation of LLMProvider."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from henchmen.models.llm import LLMResponse, Message, MessageRole, ModelTier, TokenUsage, ToolCall, ToolDefinition
from henchmen.providers.llm_common import json_schema, normalize_finish_reason, resolve_provider_model
from henchmen.providers.pricing import estimate_cost
from henchmen.providers.tiers import tier_models

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

PROVIDER_NAME = "openai"

# o-series reasoning models (o1/o3/o4...) reject `max_tokens` (they take
# `max_completion_tokens`) and reject any non-default `temperature`.
_REASONING_MODEL = re.compile(r"^o\d")


def _is_reasoning_model(model: str) -> bool:
    return bool(_REASONING_MODEL.match((model or "").strip().lower()))


class OpenAIProvider:
    """LLMProvider backed by the OpenAI API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # The SDK raises when no key can be resolved, so the client is built on
        # first use: the registry may construct every provider up front.
        self._client: Any = None
        models = tier_models(settings, PROVIDER_NAME)
        logger.info(
            "OpenAIProvider tier mapping: complex=%s light=%s reasoning=%s",
            models.get(ModelTier.COMPLEX, ""),
            models.get(ModelTier.LIGHT, ""),
            models.get(ModelTier.REASONING, ""),
        )

    def _openai_client(self) -> Any:
        """Lazily build the SDK client, letting the SDK resolve OPENAI_API_KEY itself."""
        if self._client is None:
            import openai

            # An empty string would disable the SDK's own OPENAI_API_KEY lookup,
            # so pass None when the setting is unset and let the SDK resolve it.
            api_key = (getattr(self._settings, "openai_api_key", "") or "").strip() or None
            self._client = openai.AsyncOpenAI(api_key=api_key)
        return self._client

    def resolve_tier(self, tier: str) -> str:
        """Map a model tier to the configured OpenAI model; concrete names pass through."""
        return resolve_provider_model(self._settings, tier, PROVIDER_NAME)

    def supported_models(self) -> list[str]:
        """Return the configured tier models, deduped and in tier order."""
        models = tier_models(self._settings, PROVIDER_NAME)
        seen: set[str] = set()
        out: list[str] = []
        for tier in (ModelTier.COMPLEX, ModelTier.LIGHT, ModelTier.REASONING):
            name = models.get(tier, "")
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    async def count_tokens(self, text: str, model: str) -> int:
        """Approximate token count using a 4-chars-per-token heuristic (model-independent)."""
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
        model = self.resolve_tier(model)
        oai_messages: list[dict[str, Any]] = []
        if system_prompt:
            oai_messages.append({"role": "system", "content": system_prompt})
        oai_messages.extend(self._build_messages(messages))

        kwargs: dict[str, Any] = {"model": model, "messages": oai_messages}
        if _is_reasoning_model(model):
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": json_schema(t),
                    },
                }
                for t in tools
            ]

        response = await self._openai_client().chat.completions.create(**kwargs)
        choice = response.choices[0]
        content = choice.message.content or ""
        tool_calls: list[ToolCall] = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=json.loads(tc.function.arguments or "{}"),
                    )
                )

        usage = response.usage
        input_tokens = int(usage.prompt_tokens or 0) if usage else 0
        output_tokens = int(usage.completion_tokens or 0) if usage else 0
        total_tokens = int(usage.total_tokens or 0) if usage else 0
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                total_tokens=total_tokens or (input_tokens + output_tokens),
                estimated_cost_usd=estimate_cost(model, input_tokens, output_tokens, cached_input_tokens=cached_tokens),
            ),
            model=model,
            finish_reason=normalize_finish_reason(choice.finish_reason, has_tool_calls=bool(tool_calls)),
        )

    @staticmethod
    def _build_messages(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert henchmen messages to the Chat Completions payload.

        Assistant turns keep their ``tool_calls`` and tool results keep their
        ``tool_call_id``; without both the API rejects every turn that follows
        a tool call with a 400.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                continue
            if msg.role == MessageRole.TOOL:
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id or "unknown",
                        "content": msg.content,
                    }
                )
            elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                result.append(
                    {
                        "role": "assistant",
                        "content": msg.content or None,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                )
            elif not msg.content.strip():
                continue  # An empty turn with no tool calls carries nothing.
            else:
                result.append({"role": msg.role.value, "content": msg.content})
        return result
