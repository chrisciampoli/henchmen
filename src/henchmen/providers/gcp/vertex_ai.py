"""GCP Vertex AI (Gemini) implementation of LLMProvider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from google import genai

from henchmen.models.llm import (
    LLMResponse,
    Message,
    MessageRole,
    ModelTier,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolParameter,
)
from henchmen.providers.llm_common import normalize_finish_reason, resolve_provider_model
from henchmen.providers.pricing import estimate_cost
from henchmen.providers.tiers import tier_models

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

PROVIDER_NAME = "gcp"


def _gemini_schema(param: ToolParameter) -> dict[str, Any]:
    """Build a Gemini parameter schema (upper-cased types, enum and array items preserved)."""
    schema: dict[str, Any] = {"type": param.type.upper(), "description": param.description}
    if param.enum:
        schema["enum"] = list(param.enum)
    if param.items:
        schema["items"] = _upper_types(param.items)
    elif param.type.lower() == "array":
        # Gemini rejects an ARRAY declaration with no item type; assume strings
        # rather than failing the whole tool list.
        schema["items"] = {"type": "STRING"}
    return schema


def _upper_types(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively upper-case JSON-Schema ``type`` values for the Gemini dialect."""
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, str):
            out[key] = value.upper()
        elif key in ("items", "properties") and isinstance(value, dict):
            if key == "properties":
                out[key] = {k: _upper_types(v) if isinstance(v, dict) else v for k, v in value.items()}
            else:
                out[key] = _upper_types(value)
        else:
            out[key] = value
    return out


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
        """Count tokens for the given text using the specified model or tier."""
        response = await self._client.aio.models.count_tokens(model=self.resolve_tier(model), contents=text)
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

        model = self.resolve_tier(model)
        contents = self._build_contents(messages, types)

        genai_tools: list[Any] | None = None
        if tools:
            declarations = []
            for tool in tools:
                params: dict[str, Any] = {}
                required: list[str] = []
                for p in tool.parameters:
                    params[p.name] = _gemini_schema(p)
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
            # google-genai's `contents` accepts a covariant Sequence; some
            # versions tighten the union enough that mypy is happy with our
            # list[Content], so we tag both arg-type and unused-ignore.
            contents=contents,  # type: ignore[arg-type, unused-ignore]
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
        # Gemini's prompt_token_count already includes cached tokens.
        input_tokens: int = int(usage_meta.prompt_token_count) if usage_meta and usage_meta.prompt_token_count else 0
        output_tokens: int = (
            int(usage_meta.candidates_token_count) if usage_meta and usage_meta.candidates_token_count else 0
        )
        cached: int = (
            int(usage_meta.cached_content_token_count) if usage_meta and usage_meta.cached_content_token_count else 0
        )
        cost = estimate_cost(model, input_tokens, output_tokens, cached_input_tokens=cached)

        raw_finish = response.candidates[0].finish_reason if response.candidates else None
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
            finish_reason=normalize_finish_reason(raw_finish, has_tool_calls=bool(tool_calls)),
        )

    @staticmethod
    def _build_contents(messages: list[Message], types: Any) -> list[Any]:
        """Convert henchmen messages to Gemini ``Content`` turns.

        Tool calls become ``function_call`` parts and tool results become
        ``function_response`` parts; empty text parts are never emitted (Vertex
        rejects them with 400 INVALID_ARGUMENT) and consecutive same-role turns
        are merged so the conversation stays alternating.
        """
        call_names: dict[str, str] = {}
        for msg in messages:
            for tc in msg.tool_calls or []:
                call_names[tc.id] = tc.name

        contents: list[Any] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                continue
            parts: list[Any] = []
            if msg.role == MessageRole.TOOL:
                call_id = msg.tool_call_id or ""
                name = call_names.get(call_id) or call_id.removeprefix("call_") or "tool"
                parts.append(types.Part.from_function_response(name=name, response={"result": msg.content}))
                role = "user"
            else:
                role = "model" if msg.role == MessageRole.ASSISTANT else "user"
                if msg.content.strip():
                    parts.append(types.Part(text=msg.content))
                for tc in msg.tool_calls or []:
                    parts.append(types.Part.from_function_call(name=tc.name, args=tc.arguments))
            if not parts:
                continue
            if contents and contents[-1].role == role:
                contents[-1].parts.extend(parts)
            else:
                contents.append(types.Content(role=role, parts=parts))
        return contents
