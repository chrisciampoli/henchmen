"""Tier-name resolution shared by every LLM provider, the cost tracker and the CLI.

Scheme nodes name a *tier* (``default/complex``, ``default/light``,
``default/reasoning``) rather than a concrete model. Each LLM provider maps
tiers to models through ``Settings`` fields; this module is the one place
that knows which field belongs to which provider, so the operative, the
Mastermind cost gate, the tracker and ``henchmen init`` all agree.

It also normalises the friendly provider names people type in ``.env.local``
(``ollama``, ``vertex``, ``bedrock``) onto the registry's canonical names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from henchmen.models.llm import ModelTier

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

CANONICAL_LLM_PROVIDERS: tuple[str, ...] = ("gcp", "aws", "local", "openai", "anthropic")

LLM_PROVIDER_ALIASES: dict[str, str] = {
    "ollama": "local",
    "vertex": "gcp",
    "vertexai": "gcp",
    "vertex-ai": "gcp",
    "vertex_ai": "gcp",
    "gemini": "gcp",
    "google": "gcp",
    "bedrock": "aws",
    "claude": "anthropic",
}

# provider -> tier -> Settings field holding the concrete model name
TIER_FIELDS: dict[str, dict[ModelTier, str]] = {
    "anthropic": {
        ModelTier.COMPLEX: "anthropic_model_complex",
        ModelTier.LIGHT: "anthropic_model_light",
        ModelTier.REASONING: "anthropic_model_reasoning",
    },
    "openai": {
        ModelTier.COMPLEX: "openai_model_complex",
        ModelTier.LIGHT: "openai_model_light",
        ModelTier.REASONING: "openai_model_reasoning",
    },
    "gcp": {
        ModelTier.COMPLEX: "vertex_ai_model_complex",
        ModelTier.LIGHT: "vertex_ai_model_light",
        ModelTier.REASONING: "vertex_ai_model_reasoning",
    },
    "local": {
        ModelTier.COMPLEX: "llm_ollama_model_complex",
        ModelTier.LIGHT: "llm_ollama_model_light",
        ModelTier.REASONING: "llm_ollama_model_reasoning",
    },
    "aws": {
        ModelTier.COMPLEX: "bedrock_model_complex",
        ModelTier.LIGHT: "bedrock_model_light",
        ModelTier.REASONING: "bedrock_model_reasoning",
    },
}

_TIER_VALUES: frozenset[str] = frozenset(tier.value for tier in ModelTier)


def normalize_llm_provider(name: str) -> str:
    """Map a user-facing provider name onto a canonical registry name.

    Unknown names are returned lower-cased and stripped so the registry can
    raise its own, more specific error.
    """
    key = (name or "").strip().lower()
    return LLM_PROVIDER_ALIASES.get(key, key)


def active_llm_provider(settings: Settings) -> str:
    """The canonical LLM provider in effect: the override if set, else the default provider."""
    return normalize_llm_provider(settings.llm_provider or settings.provider)


def is_tier_name(model_name: str) -> bool:
    return model_name in _TIER_VALUES


def tier_models(settings: Settings, provider: str | None = None) -> dict[ModelTier, str]:
    """Concrete model per tier for ``provider`` (default: the active one).

    Ollama tiers fall back to ``llm_ollama_model`` when a per-tier field is empty.
    Unknown providers yield an empty mapping.
    """
    canonical = normalize_llm_provider(provider) if provider else active_llm_provider(settings)
    fields = TIER_FIELDS.get(canonical)
    if not fields:
        return {}
    resolved: dict[ModelTier, str] = {}
    for tier, field_name in fields.items():
        value = str(getattr(settings, field_name, "") or "")
        if not value and canonical == "local":
            value = str(settings.llm_ollama_model)
        resolved[tier] = value
    return resolved


def resolve_model_name(settings: Settings, model_name: str, provider: str | None = None) -> str:
    """Resolve a tier name to the configured concrete model; concrete names pass through.

    An empty ``model_name`` resolves to the COMPLEX tier so callers never hand
    a provider an empty model id.
    """
    if not model_name:
        model_name = ModelTier.COMPLEX.value
    if not is_tier_name(model_name):
        return model_name
    models = tier_models(settings, provider)
    resolved = models.get(ModelTier(model_name), "")
    return resolved or model_name


__all__ = [
    "CANONICAL_LLM_PROVIDERS",
    "LLM_PROVIDER_ALIASES",
    "TIER_FIELDS",
    "active_llm_provider",
    "is_tier_name",
    "normalize_llm_provider",
    "resolve_model_name",
    "tier_models",
]
