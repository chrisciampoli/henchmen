"""AWS Bedrock implementation of LLMProvider using the Converse API."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from henchmen.models.llm import LLMResponse, Message, MessageRole, ModelTier, TokenUsage, ToolCall, ToolDefinition
from henchmen.providers.llm_common import json_schema, normalize_finish_reason, resolve_provider_model
from henchmen.providers.pricing import estimate_cost
from henchmen.providers.tiers import tier_models

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

PROVIDER_NAME = "aws"


class BedrockProvider:
    """LLMProvider backed by AWS Bedrock using the Converse API."""

    def __init__(self, settings: Settings) -> None:
        import boto3

        self._settings = settings
        region = getattr(settings, "aws_region", "us-east-1")
        self._client: Any = boto3.client("bedrock-runtime", region_name=region)
        models = tier_models(settings, PROVIDER_NAME)
        logger.info(
            "BedrockProvider tier mapping: complex=%s light=%s reasoning=%s",
            models.get(ModelTier.COMPLEX, ""),
            models.get(ModelTier.LIGHT, ""),
            models.get(ModelTier.REASONING, ""),
        )

    def resolve_tier(self, tier: str) -> str:
        """Map a ModelTier to the configured Bedrock model ID; concrete IDs pass through."""
        return resolve_provider_model(self._settings, tier, PROVIDER_NAME)

    def supported_models(self) -> list[str]:
        """Return the configured tier model IDs, deduped and in tier order."""
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
        """Send a request to Bedrock via the Converse API."""
        model = self.resolve_tier(model)
        converse_messages = self._build_messages(messages)
        kwargs: dict[str, Any] = {
            "modelId": model,
            "messages": converse_messages,
            "inferenceConfig": {
                "maxTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system_prompt:
            kwargs["system"] = [{"text": system_prompt}]
        if tools:
            kwargs["toolConfig"] = {"tools": [self._convert_tool(t) for t in tools]}

        response = await asyncio.to_thread(self._client.converse, **kwargs)
        return self._parse_response(response, model)

    @staticmethod
    def _build_messages(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert henchmen messages to Bedrock Converse format.

        Tool calls become ``toolUse`` blocks and tool results ``toolResult``
        blocks; blank text blocks are dropped and consecutive same-role turns
        are merged, both of which Converse rejects with a ValidationException.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                continue
            blocks: list[dict[str, Any]] = []
            if msg.role == MessageRole.TOOL:
                blocks.append(
                    {
                        "toolResult": {
                            "toolUseId": msg.tool_call_id or "unknown",
                            "content": [{"text": msg.content}],
                        }
                    }
                )
                role = "user"
            else:
                role = "assistant" if msg.role == MessageRole.ASSISTANT else "user"
                if msg.content.strip():
                    blocks.append({"text": msg.content})
                for tc in msg.tool_calls or []:
                    blocks.append({"toolUse": {"toolUseId": tc.id, "name": tc.name, "input": tc.arguments}})
            if not blocks:
                continue
            if result and result[-1]["role"] == role:
                result[-1]["content"].extend(blocks)
            else:
                result.append({"role": role, "content": blocks})
        return result

    @staticmethod
    def _convert_tool(tool: ToolDefinition) -> dict[str, Any]:
        """Convert a ToolDefinition to Bedrock toolSpec format."""
        return {
            "toolSpec": {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": {"json": json_schema(tool)},
            }
        }

    @staticmethod
    def _parse_response(response: dict[str, Any], model: str) -> LLMResponse:
        """Parse a Bedrock Converse API response into LLMResponse."""
        content_text = ""
        tool_calls: list[ToolCall] = []

        output = response.get("output", {})
        message = output.get("message", {})
        for block in message.get("content", []):
            if "text" in block:
                content_text += block["text"]
            if "toolUse" in block:
                tool_use = block["toolUse"]
                tool_calls.append(
                    ToolCall(
                        id=tool_use.get("toolUseId", ""),
                        name=tool_use.get("name", ""),
                        arguments=tool_use.get("input", {}),
                    )
                )

        usage_data = response.get("usage", {})
        # Bedrock reports cache tokens separately from inputTokens (as Anthropic
        # does); TokenUsage.input_tokens is the total prompt.
        cache_read = int(usage_data.get("cacheReadInputTokens", 0) or 0)
        cache_write = int(usage_data.get("cacheWriteInputTokens", 0) or 0)
        input_tokens = int(usage_data.get("inputTokens", 0) or 0) + cache_read + cache_write
        output_tokens = int(usage_data.get("outputTokens", 0) or 0)

        return LLMResponse(
            content=content_text,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cache_read,
                cache_write_tokens=cache_write,
                total_tokens=input_tokens + output_tokens,
                estimated_cost_usd=estimate_cost(
                    model,
                    input_tokens,
                    output_tokens,
                    cached_input_tokens=cache_read,
                    cache_write_tokens=cache_write,
                ),
            ),
            model=model,
            finish_reason=normalize_finish_reason(response.get("stopReason"), has_tool_calls=bool(tool_calls)),
        )
