"""Single source of truth for per-model token prices and cost estimation.

Every component that puts a dollar figure on an LLM call — the Anthropic
provider, the Vertex provider, the observability tracker, the Mastermind
pre-dispatch cost gate and the operative guardrails — must use
:func:`estimate_cost` from here rather than carrying its own price table.

Conventions
-----------
* Prices are USD per one million tokens, list prices as of 2026-09.
* ``input_tokens`` passed to :func:`estimate_cost` is the TOTAL prompt size,
  including tokens served from cache and tokens written to cache. Providers
  whose APIs report uncached input separately (Anthropic) must add the cache
  counters back before building ``TokenUsage``.
* Unknown models cost 0.0. Local Ollama models are free by design; the
  tracker decides whether an unknown name deserves a warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from henchmen.providers.tiers import resolve_model_name

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


def _anthropic(input_price: float, output_price: float) -> ModelPrice:
    # Anthropic prompt caching: reads at 10% of input, writes at 125%.
    return ModelPrice(input_price, output_price, cache_read=input_price * 0.10, cache_write=input_price * 1.25)


def _gemini(input_price: float, output_price: float) -> ModelPrice:
    # Vertex AI context caching: cached tokens billed at 25% of input; no per-token write premium.
    return ModelPrice(input_price, output_price, cache_read=input_price * 0.25, cache_write=input_price)


def _openai(input_price: float, output_price: float) -> ModelPrice:
    # OpenAI prompt caching: cached input at 25% (gpt-4.1 family) — conservative across models.
    return ModelPrice(input_price, output_price, cache_read=input_price * 0.25, cache_write=input_price)


PRICE_TABLE: dict[str, ModelPrice] = {
    # Anthropic first-party IDs. Dated snapshots (claude-sonnet-4-20250514),
    # Vertex forms (claude-sonnet-4@20250514) and Bedrock forms
    # (anthropic.claude-sonnet-4-20250514-v1:0) normalise onto these keys.
    "claude-opus-5": _anthropic(5.00, 25.00),
    "claude-opus-4-8": _anthropic(5.00, 25.00),
    "claude-opus-4-7": _anthropic(5.00, 25.00),
    "claude-opus-4-6": _anthropic(5.00, 25.00),
    "claude-opus-4-5": _anthropic(5.00, 25.00),
    "claude-opus-4-1": _anthropic(15.00, 75.00),
    "claude-opus-4": _anthropic(15.00, 75.00),
    "claude-sonnet-5": _anthropic(2.00, 10.00),
    "claude-sonnet-4-6": _anthropic(3.00, 15.00),
    "claude-sonnet-4-5": _anthropic(3.00, 15.00),
    "claude-sonnet-4": _anthropic(3.00, 15.00),
    "claude-haiku-4-5": _anthropic(1.00, 5.00),
    "claude-3-5-haiku": _anthropic(0.80, 4.00),
    # Gemini on Vertex AI (prompts up to 200k tokens).
    "gemini-3.1-pro": _gemini(2.00, 12.00),
    "gemini-2.5-pro": _gemini(1.25, 10.00),
    "gemini-2.5-flash": _gemini(0.30, 2.50),
    "gemini-2.5-flash-lite": _gemini(0.10, 0.40),
    # OpenAI.
    "gpt-4.1": _openai(2.00, 8.00),
    "gpt-4.1-mini": _openai(0.40, 1.60),
    "gpt-4.1-nano": _openai(0.10, 0.40),
    "gpt-4o": _openai(2.50, 10.00),
    "gpt-4o-mini": _openai(0.15, 0.60),
    "o3": _openai(2.00, 8.00),
    "o4-mini": _openai(1.10, 4.40),
}

_DATE_SUFFIX = re.compile(r"[-@]\d{8}$")
_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]{2}\.)?anthropic\.")
_BEDROCK_SUFFIX = re.compile(r"-v\d+(?::\d+)?$")
_VERSION_TAIL = re.compile(r"^(preview|exp|latest|\d)")


def normalize_model_id(model: str) -> str:
    """Reduce vendor-specific spellings to the first-party model family id.

    ``anthropic.claude-sonnet-4-20250514-v1:0`` -> ``claude-sonnet-4``;
    ``claude-haiku-4-5@20251001`` -> ``claude-haiku-4-5``;
    ``gemini-2.5-pro-preview-05-06`` is left for the prefix match in :func:`lookup_price`.
    """
    name = (model or "").strip().lower()
    name = _BEDROCK_PREFIX.sub("", name)
    name = _BEDROCK_SUFFIX.sub("", name)
    name = name.replace("@", "-")
    return _DATE_SUFFIX.sub("", name)


def lookup_price(model: str) -> ModelPrice | None:
    """Exact match, then normalised match, then a versioned-suffix prefix match."""
    if model in PRICE_TABLE:
        return PRICE_TABLE[model]
    normalized = normalize_model_id(model)
    if normalized in PRICE_TABLE:
        return PRICE_TABLE[normalized]
    for key in sorted(PRICE_TABLE, key=len, reverse=True):
        if normalized.startswith(key + "-") and _VERSION_TAIL.match(normalized[len(key) + 1 :]):
            return PRICE_TABLE[key]
    return None


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """USD cost of one call. ``input_tokens`` includes cached and cache-write tokens.

    Returns 0.0 for models with no price entry.
    """
    price = lookup_price(model)
    if price is None:
        return 0.0
    cached = max(0, cached_input_tokens)
    written = max(0, cache_write_tokens)
    uncached = max(0, input_tokens - cached - written)
    return (
        uncached * price.input + cached * price.cache_read + written * price.cache_write + output_tokens * price.output
    ) / 1_000_000


def estimate_cost_for_settings(
    settings: Settings,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Like :func:`estimate_cost` but resolves tier names through the configured provider first."""
    concrete = resolve_model_name(settings, model_name)
    return estimate_cost(concrete, input_tokens, output_tokens, cached_input_tokens, cache_write_tokens)


__all__ = [
    "PRICE_TABLE",
    "ModelPrice",
    "estimate_cost",
    "estimate_cost_for_settings",
    "lookup_price",
    "normalize_model_id",
]
