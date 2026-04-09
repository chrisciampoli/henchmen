"""Ollama implementation of LLMProvider for local development."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from henchmen.models.llm import LLMResponse, Message, ModelTier, TokenUsage, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)


class OllamaProvider:
    """LLMProvider backed by a local Ollama server."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = getattr(settings, "llm_ollama_base_url", "http://localhost:11434")
        self._default_model = getattr(settings, "llm_ollama_model", "llama3.2")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=300.0)

    def resolve_tier(self, tier: str) -> str:
        """Map any model tier to the configured default Ollama model."""
        if tier in (ModelTier.COMPLEX, ModelTier.LIGHT, ModelTier.REASONING):
            return self._default_model
        return tier

    def supported_models(self) -> list[str]:
        """Return the configured default model as the supported model list."""
        return [self._default_model]

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
        """Send a chat completion request to the Ollama API."""
        ollama_messages: list[dict[str, Any]] = []
        if system_prompt:
            ollama_messages.append({"role": "system", "content": system_prompt})
        for msg in messages:
            ollama_messages.append({"role": msg.role.value, "content": msg.content})
        payload: dict[str, Any] = {
            "model": model,
            "messages": ollama_messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = [self._convert_tool(t) for t in tools]
        response = await self._client.post("/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()
        content = data.get("message", {}).get("content", "")
        tool_calls = self._parse_tool_calls(data.get("message", {}))
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                estimated_cost_usd=0.0,
            ),
            model=model,
            finish_reason="tool_use" if tool_calls else "stop",
        )

    @staticmethod
    def _convert_tool(tool: ToolDefinition) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in tool.parameters:
            properties[p.name] = {"type": p.type, "description": p.description}
            if p.required:
                required.append(p.name)
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
        }

    @staticmethod
    def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
        calls = message.get("tool_calls", [])
        result = []
        for i, call in enumerate(calls):
            fn = call.get("function", {})
            result.append(ToolCall(id=f"call_{i}", name=fn.get("name", ""), arguments=fn.get("arguments", {})))
        return result
