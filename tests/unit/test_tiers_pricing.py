"""Unit tests for shared tier resolution and the single pricing table."""

from __future__ import annotations

import pytest

from henchmen.config.settings import Settings
from henchmen.models.llm import ModelTier
from henchmen.providers import pricing, tiers
from henchmen.providers.pricing import estimate_cost, estimate_cost_for_settings, lookup_price, normalize_model_id
from henchmen.providers.tiers import (
    active_llm_provider,
    is_tier_name,
    normalize_llm_provider,
    resolve_model_name,
    tier_models,
)


def _settings(**overrides: object) -> Settings:
    """Build Settings from defaults only — never the developer's .env.local."""
    base = Settings(_env_file=None, provider="local")  # type: ignore[call-arg]
    return base.model_copy(update=overrides)


# ---------------------------------------------------------------------------
# Provider name normalisation
# ---------------------------------------------------------------------------


class TestNormalizeProvider:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ollama", "local"),
            ("Ollama", "local"),
            ("vertex", "gcp"),
            ("vertexai", "gcp"),
            ("vertex-ai", "gcp"),
            ("gemini", "gcp"),
            ("bedrock", "aws"),
            ("claude", "anthropic"),
            ("anthropic", "anthropic"),
            ("openai", "openai"),
            (" gcp ", "gcp"),
            ("", ""),
            ("nonsense", "nonsense"),
        ],
    )
    def test_aliases(self, raw: str, expected: str):
        assert normalize_llm_provider(raw) == expected

    def test_active_provider_prefers_override(self):
        assert active_llm_provider(_settings(llm_provider="ollama")) == "local"
        assert active_llm_provider(_settings(provider="gcp", gcp_project_id="p", llm_provider="")) == "gcp"
        assert active_llm_provider(_settings(llm_provider="anthropic")) == "anthropic"


# ---------------------------------------------------------------------------
# Tier resolution
# ---------------------------------------------------------------------------


class TestTierResolution:
    def test_is_tier_name(self):
        assert is_tier_name("default/complex") and is_tier_name(ModelTier.REASONING)
        assert not is_tier_name("gemini-2.5-pro") and not is_tier_name("")

    def test_anthropic_tiers_from_settings(self):
        s = _settings(llm_provider="anthropic", anthropic_model_light="claude-haiku-4-5")
        assert tier_models(s)[ModelTier.LIGHT] == "claude-haiku-4-5"
        assert resolve_model_name(s, "default/complex") == s.anthropic_model_complex
        assert resolve_model_name(s, "default/reasoning") == s.anthropic_model_reasoning

    def test_vertex_has_distinct_reasoning_tier(self):
        s = _settings(provider="gcp", gcp_project_id="p", llm_provider="")
        models = tier_models(s)
        assert models[ModelTier.REASONING] == "gemini-3.1-pro"
        assert models[ModelTier.LIGHT] == "gemini-2.5-flash"
        assert models[ModelTier.COMPLEX] == "gemini-2.5-pro"

    def test_ollama_per_tier_with_fallback(self):
        s = _settings(llm_provider="ollama", llm_ollama_model="base", llm_ollama_model_reasoning="deepseek-r1:8b")
        models = tier_models(s)
        assert models[ModelTier.COMPLEX] == "base"
        assert models[ModelTier.LIGHT] == "base"
        assert models[ModelTier.REASONING] == "deepseek-r1:8b"

    def test_bedrock_tiers_from_settings(self):
        s = _settings(llm_provider="bedrock", bedrock_model_light="anthropic.claude-haiku-4-5-20251001-v1:0")
        assert resolve_model_name(s, "default/light") == "anthropic.claude-haiku-4-5-20251001-v1:0"

    def test_openai_tiers(self):
        s = _settings(llm_provider="openai")
        assert resolve_model_name(s, "default/reasoning") == s.openai_model_reasoning

    def test_concrete_names_pass_through(self):
        s = _settings(llm_provider="anthropic")
        assert resolve_model_name(s, "claude-opus-5") == "claude-opus-5"
        assert resolve_model_name(s, "gemini-2.5-pro") == "gemini-2.5-pro"

    def test_empty_model_resolves_to_complex(self):
        s = _settings(llm_provider="anthropic")
        assert resolve_model_name(s, "") == s.anthropic_model_complex

    def test_explicit_provider_argument(self):
        s = _settings(llm_provider="anthropic")
        assert resolve_model_name(s, "default/complex", provider="vertex") == "gemini-2.5-pro"

    def test_unknown_provider_returns_tier_unchanged(self):
        s = _settings(llm_provider="nonsense")
        assert tier_models(s) == {}
        assert resolve_model_name(s, "default/complex") == "default/complex"

    def test_every_tier_field_exists_on_settings(self):
        for fields in tiers.TIER_FIELDS.values():
            for field_name in fields.values():
                assert field_name in Settings.model_fields, field_name


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


class TestPricing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("claude-sonnet-4-20250514", "claude-sonnet-4"),
            ("claude-sonnet-4@20250514", "claude-sonnet-4"),
            ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
            ("anthropic.claude-sonnet-4-20250514-v1:0", "claude-sonnet-4"),
            ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
            ("claude-opus-5", "claude-opus-5"),
            ("GPT-4.1", "gpt-4.1"),
        ],
    )
    def test_normalize_model_id(self, raw: str, expected: str):
        assert normalize_model_id(raw) == expected

    def test_lookup_exact_and_normalised(self):
        assert lookup_price("claude-sonnet-5") == pricing.PRICE_TABLE["claude-sonnet-5"]
        assert lookup_price("claude-sonnet-4-20250514") == pricing.PRICE_TABLE["claude-sonnet-4"]
        assert lookup_price("anthropic.claude-haiku-4-5-20251001-v1:0") == pricing.PRICE_TABLE["claude-haiku-4-5"]

    def test_lookup_versioned_suffix_prefix_match(self):
        assert lookup_price("gemini-2.5-pro-preview-05-06") == pricing.PRICE_TABLE["gemini-2.5-pro"]
        assert lookup_price("gpt-4.1-2025-04-14") == pricing.PRICE_TABLE["gpt-4.1"]

    def test_lookup_does_not_confuse_families(self):
        # flash-lite has its own row and must not resolve to flash
        assert lookup_price("gemini-2.5-flash-lite") == pricing.PRICE_TABLE["gemini-2.5-flash-lite"]
        assert lookup_price("qwen2.5-coder:7b") is None
        assert lookup_price("") is None

    def test_haiku_45_price_is_current(self):
        price = lookup_price("claude-haiku-4-5")
        assert price is not None
        assert (price.input, price.output) == (1.0, 5.0)

    def test_estimate_cost_uncached(self):
        # Sonnet 5: $2 in / $10 out
        assert estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000) == pytest.approx(12.0)

    def test_estimate_cost_with_cache_read_and_write(self):
        # 1M total input: 400k cached reads (10%), 100k cache writes (125%), 500k uncached; 0 output
        cost = estimate_cost("claude-sonnet-5", 1_000_000, 0, cached_input_tokens=400_000, cache_write_tokens=100_000)
        assert cost == pytest.approx(0.5 * 2.0 + 0.4 * 0.2 + 0.1 * 2.5)

    def test_estimate_cost_cached_exceeding_input_is_clamped(self):
        cost = estimate_cost("claude-sonnet-5", 100, 0, cached_input_tokens=1_000)
        assert cost == pytest.approx(1_000 * 0.2 / 1_000_000)

    def test_unknown_model_costs_zero(self):
        assert estimate_cost("qwen2.5-coder:7b", 10_000, 10_000) == 0.0

    def test_gemini_cache_read_discount(self):
        price = lookup_price("gemini-2.5-pro")
        assert price is not None
        assert price.cache_read == pytest.approx(price.input * 0.25)

    def test_estimate_for_settings_resolves_tiers_per_provider(self):
        vertex = _settings(provider="gcp", gcp_project_id="p", llm_provider="")
        anthropic = _settings(llm_provider="anthropic")
        ollama = _settings(llm_provider="ollama")
        assert estimate_cost_for_settings(vertex, "default/complex", 1_000_000, 0) == pytest.approx(1.25)
        assert estimate_cost_for_settings(anthropic, "default/complex", 1_000_000, 0) == pytest.approx(2.0)
        assert estimate_cost_for_settings(ollama, "default/complex", 1_000_000, 0) == 0.0

    def test_every_default_tier_model_has_a_price_except_local(self):
        for provider in ("anthropic", "openai", "gcp", "aws"):
            s = _settings(provider="gcp", gcp_project_id="p", llm_provider=provider)
            for tier, model in tier_models(s).items():
                assert lookup_price(model) is not None, f"{provider} {tier}: {model}"
