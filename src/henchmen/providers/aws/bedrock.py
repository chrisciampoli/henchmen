"""AWS Bedrock implementation of LLMProvider using the Converse API."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from henchmen.models.llm import LLMResponse, Message, MessageRole, ModelTier, TokenUsage, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

# Bedrock model IDs for each tier
_TIER_MODELS: dict[str, str] = {
    ModelTier.COMPLEX: "anthropic.claude-sonnet-4-20250514-v1:0",
    ModelTier.REASONING: "anthropic.claude-sonnet-4-20250514-v1:0",
    ModelTier.LIGHT: "anthropic.claude-haiku-4-5-20251001-v1:0",
}

_SUPPORTED_MODELS: list[str] = [
    "anthropic.claude-sonnet-4-20250514-v1:0",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
    "amazon.titan-text-express-v1",
]


class BedrockProvider:
    """LLMProvider backed by AWS Bedrock using the Converse API."""

    def __init__(self, settings: Settings) -> None:
        import boto3

        region = getattr(settings, "aws_region", "us-east-1")
        self._client: Any = boto3.client("bedrock-runtime", region_name=region)

    def resolve_tier(self, tier: str) -> str:
        """Map a ModelTier to a concrete Bedrock model ID."""
        return _TIER_MODELS.get(tier, tier)

    def supported_models(self) -> list[str]:
        """Return list of supported Bedrock model IDs."""
        return list(_SUPPORTED_MODELS)

    async def count_tokens(self, text: str, model: str) -> int:
        """Approximate token count using 4 chars/token heuristic."""
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
        """Convert henchmen Message list to Bedrock Converse format."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                continue
            role = "assistant" if msg.role == MessageRole.ASSISTANT else "user"
            result.append({"role": role, "content": [{"text": msg.content}]})
        return result

    @staticmethod
    def _convert_tool(tool: ToolDefinition) -> dict[str, Any]:
        """Convert a ToolDefinition to Bedrock toolSpec format."""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in tool.parameters:
            properties[p.name] = {"type": p.type, "description": p.description}
            if p.required:
                required.append(p.name)
        return {
            "toolSpec": {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    }
                },
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
        input_tokens = int(usage_data.get("inputTokens", 0))
        output_tokens = int(usage_data.get("outputTokens", 0))

        stop_reason = response.get("stopReason", "end_turn")
        finish_reason = "tool_use" if tool_calls else ("max_tokens" if stop_reason == "max_tokens" else "stop")

        return LLMResponse(
            content=content_text,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                estimated_cost_usd=0.0,
            ),
            model=model,
            finish_reason=finish_reason,
        )
