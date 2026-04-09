"""GCP Vertex AI (Gemini) implementation of LLMProvider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from google import genai

from henchmen.models.llm import LLMResponse, Message, MessageRole, ModelTier, TokenUsage, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

_PRICE_MAP: dict[str, tuple[float, float]] = {
    "gemini-3.1-pro": (2.0, 12.0),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.15, 0.60),
}

_TIER_MAP_KEYS: dict[str, str] = {
    ModelTier.COMPLEX: "vertex_ai_model_complex",
    ModelTier.LIGHT: "vertex_ai_model_light",
    ModelTier.REASONING: "vertex_ai_model_complex",
}


class VertexAIProvider:
    """LLMProvider backed by Vertex AI Gemini models."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_region,
        )

    def resolve_tier(self, tier: str) -> str:
        """Map a ModelTier to the concrete model name from settings."""
        setting_key = _TIER_MAP_KEYS.get(tier)
        if setting_key:
            return str(getattr(self._settings, setting_key))
        return tier

    def supported_models(self) -> list[str]:
        """Return the list of Gemini models available on Vertex AI."""
        return ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.1-pro"]

    async def count_tokens(self, text: str, model: str) -> int:
        """Count tokens for the given text using the specified model."""
        response = await self._client.aio.models.count_tokens(model=model, contents=text)
        return response.total_tokens or 0

    async def generate(
        self,
        messages: list[Message],
        model: str,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        system_prompt: str | None = None,
    ) -> LLMResponse:
        """Generate a response from the Gemini model via Vertex AI."""
        from google.genai import types

        contents = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                continue
            role = "model" if msg.role == MessageRole.ASSISTANT else "user"
            contents.append(types.Content(role=role, parts=[types.Part(text=msg.content)]))

        genai_tools: list[Any] | None = None
        if tools:
            declarations = []
            for tool in tools:
                params: dict[str, Any] = {}
                required: list[str] = []
                for p in tool.parameters:
                    params[p.name] = {"type": p.type.upper(), "description": p.description}
                    if p.required:
                        required.append(p.name)
                declarations.append(
                    types.FunctionDeclaration(
                        name=tool.name,
                        description=tool.description,
                        parameters={"type": "OBJECT", "properties": params, "required": required},  # type: ignore[arg-type]
                    )
                )
            genai_tools = [types.Tool(function_declarations=declarations)]

        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            system_instruction=system_prompt,
            tools=genai_tools,
        )
        response = await self._client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

        content_text = ""
        tool_calls: list[ToolCall] = []
        if response.candidates:
            candidate_content = response.candidates[0].content
            if candidate_content and candidate_content.parts:
                for part in candidate_content.parts:
                    if part.text:
                        content_text += part.text
                    if part.function_call:
                        tool_calls.append(
                            ToolCall(
                                id=f"call_{part.function_call.name}",
                                name=str(part.function_call.name),
                                arguments=dict(part.function_call.args) if part.function_call.args else {},
                            )
                        )

        usage_meta = response.usage_metadata
        input_tokens: int = int(usage_meta.prompt_token_count) if usage_meta and usage_meta.prompt_token_count else 0
        output_tokens: int = (
            int(usage_meta.candidates_token_count) if usage_meta and usage_meta.candidates_token_count else 0
        )
        cached: int = (
            int(usage_meta.cached_content_token_count) if usage_meta and usage_meta.cached_content_token_count else 0
        )
        cost = self._estimate_cost(model, input_tokens, output_tokens, cached)

        finish = "tool_use" if tool_calls else "stop"
        if response.candidates and response.candidates[0].finish_reason:
            reason = str(response.candidates[0].finish_reason)
            if "MAX_TOKENS" in reason:
                finish = "max_tokens"

        return LLMResponse(
            content=content_text,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached,
                total_tokens=input_tokens + output_tokens,
                estimated_cost_usd=cost,
            ),
            model=model,
            finish_reason=finish,
        )

    def _estimate_cost(self, model: str, input_tokens: int, output_tokens: int, cached: int) -> float:
        """Estimate USD cost for a Gemini API call, accounting for cache discounts."""
        prices = _PRICE_MAP.get(model, (1.25, 10.0))
        billable_input = input_tokens - cached
        cached_cost = (cached / 1_000_000) * prices[0] * 0.25
        input_cost = (billable_input / 1_000_000) * prices[0]
        output_cost = (output_tokens / 1_000_000) * prices[1]
        return input_cost + cached_cost + output_cost
