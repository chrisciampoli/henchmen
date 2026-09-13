"""Helpers shared by every :class:`~henchmen.providers.interfaces.llm_provider.LLMProvider`.

Tier resolution, finish-reason normalisation and JSON-Schema construction live
here so a defect fixed in one provider cannot survive in another. Providers
must not carry their own tier maps or price tables — tiers come from
:mod:`henchmen.providers.tiers` (Settings-driven) and prices from
:mod:`henchmen.providers.pricing`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from henchmen.models.llm import FinishReason, ToolDefinition
from henchmen.providers.tiers import is_tier_name, resolve_model_name

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

# Native stop reasons -> the normalised contract. Keys are compared lower-cased
# with any enum prefix stripped, so Gemini's ``FinishReason.MAX_TOKENS`` and
# Anthropic's ``max_tokens`` both land on the same entry.
_FINISH_REASONS: dict[str, FinishReason] = {
    # Anthropic
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "pause_turn": FinishReason.STOP,
    "max_tokens": FinishReason.MAX_TOKENS,
    "tool_use": FinishReason.TOOL_USE,
    "refusal": FinishReason.REFUSAL,
    # OpenAI
    "stop": FinishReason.STOP,
    "length": FinishReason.MAX_TOKENS,
    "tool_calls": FinishReason.TOOL_USE,
    "function_call": FinishReason.TOOL_USE,
    "content_filter": FinishReason.REFUSAL,
    # Bedrock Converse
    "guardrail_intervened": FinishReason.REFUSAL,
    "content_filtered": FinishReason.REFUSAL,
    # Vertex AI / Gemini
    "finish_reason_unspecified": FinishReason.ERROR,
    "safety": FinishReason.REFUSAL,
    "recitation": FinishReason.REFUSAL,
    "blocklist": FinishReason.REFUSAL,
    "prohibited_content": FinishReason.REFUSAL,
    "spii": FinishReason.REFUSAL,
    "image_safety": FinishReason.REFUSAL,
    "malformed_function_call": FinishReason.ERROR,
    "other": FinishReason.ERROR,
}


def normalize_finish_reason(raw: object, *, has_tool_calls: bool = False) -> str:
    """Map a provider's native stop reason onto the :class:`FinishReason` contract.

    Tool calls always win: a turn that produced tool calls is ``tool_use`` even
    when the vendor labels it ``end_turn``. Unrecognised reasons become
    ``error`` (fail-closed — an unknown stop is never reported as a clean stop)
    and are logged once per call.
    """
    if has_tool_calls:
        return FinishReason.TOOL_USE.value
    if raw is None:
        return FinishReason.STOP.value
    text = str(raw).strip()
    if not text:
        return FinishReason.STOP.value
    key = text.rsplit(".", 1)[-1].lower()
    mapped = _FINISH_REASONS.get(key)
    if mapped is None:
        logger.warning("Unrecognised finish_reason %r from provider; reporting as 'error'", text)
        return FinishReason.ERROR.value
    return mapped.value


def resolve_provider_model(settings: Settings, model: str, provider: str) -> str:
    """Resolve a tier name (``default/complex``) to the configured model for ``provider``.

    Concrete model names pass through unchanged. Raises ``ValueError`` when the
    tier has no configured model rather than sending the tier string to a
    vendor API as if it were a model id.
    """
    resolved = resolve_model_name(settings, model, provider=provider)
    if not resolved or is_tier_name(resolved):
        raise ValueError(
            f"No model configured for tier {model!r} on LLM provider {provider!r}. "
            f"Set the matching HENCHMEN_* model setting (see `henchmen doctor`)."
        )
    return resolved


def parameter_schema(tool: ToolDefinition) -> tuple[dict[str, Any], list[str]]:
    """Build ``(properties, required)`` for a tool, preserving enum and array item schemas.

    ``ToolParameter.enum`` and ``ToolParameter.items`` are dropped by a naive
    ``{"type": ..., "description": ...}`` projection, which loses the
    constraints the model needs to call the tool correctly.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in tool.parameters:
        schema: dict[str, Any] = {"type": param.type, "description": param.description}
        if param.enum:
            schema["enum"] = list(param.enum)
        if param.items:
            schema["items"] = dict(param.items)
        properties[param.name] = schema
        if param.required:
            required.append(param.name)
    return properties, required


def json_schema(tool: ToolDefinition) -> dict[str, Any]:
    """Full JSON-Schema object for a tool's parameters."""
    properties, required = parameter_schema(tool)
    return {"type": "object", "properties": properties, "required": required}


__all__ = [
    "json_schema",
    "normalize_finish_reason",
    "parameter_schema",
    "resolve_provider_model",
]
