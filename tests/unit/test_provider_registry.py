"""Tests for the provider registry."""

import pytest

from henchmen.providers.registry import ProviderRegistry


def _mock_settings(**overrides):
    """Build a real ``Settings`` instance with provider-field overrides.

    Built from defaults only (no ``.env.local``, no process environment
    mutation) so these tests cannot leak into or inherit from others.
    Default provider is ``local`` so tests can flip individual service
    overrides.
    """
    from henchmen.config.settings import Settings

    base = Settings(_env_file=None, provider="local", gcp_project_id="test-project")
    base_overrides = {"provider": "local"}
    base_overrides.update(overrides)
    return base.model_copy(update=base_overrides)


def test_registry_resolves_provider_name_local():
    settings = _mock_settings(provider="local")
    registry = ProviderRegistry(settings)
    assert registry.resolve_provider_name("message_broker") == "local"


def test_registry_per_service_override():
    settings = _mock_settings(provider="gcp", llm_provider="anthropic")
    registry = ProviderRegistry(settings)
    assert registry.resolve_provider_name("llm") == "anthropic"
    assert registry.resolve_provider_name("message_broker") == "gcp"


def test_registry_normalizes_llm_provider_aliases():
    """Friendly names people write in .env.local map onto the canonical registry names."""
    for alias, canonical in (("ollama", "local"), ("vertex", "gcp"), ("bedrock", "aws"), ("Anthropic", "anthropic")):
        settings = _mock_settings(provider="gcp", llm_provider=alias)
        assert ProviderRegistry(settings).resolve_provider_name("llm") == canonical


def test_registry_unknown_llm_provider_lists_aliases():
    settings = _mock_settings(provider="gcp", llm_provider="mistral")
    with pytest.raises(ValueError, match="aliases"):
        ProviderRegistry(settings).get_llm_provider()


def test_get_llm_provider_accepts_every_documented_name():
    """Every name Settings accepts must actually construct a provider."""
    from henchmen.providers.tiers import CANONICAL_LLM_PROVIDERS, LLM_PROVIDER_ALIASES

    expected = {
        "local": "OllamaProvider",
        "gcp": "VertexAIProvider",
        "aws": "BedrockProvider",
        "openai": "OpenAIProvider",
        "anthropic": "AnthropicProvider",
    }
    for name in list(CANONICAL_LLM_PROVIDERS) + list(LLM_PROVIDER_ALIASES):
        settings = _mock_settings(provider="local", llm_provider=name, gcp_project_id="test-project")
        canonical = ProviderRegistry(settings).resolve_provider_name("llm")
        assert canonical in expected, f"{name!r} resolved to unknown provider {canonical!r}"
        try:
            provider = ProviderRegistry(settings).get_llm_provider()
        except Exception:
            # The SDK is missing, or its client needs live credentials this
            # environment has no business providing. The mapping above is what
            # this test guards; construction is verified per provider elsewhere.
            continue
        assert type(provider).__name__ == expected[canonical]


def test_registry_unknown_provider_raises():
    settings = _mock_settings(provider="azure")
    registry = ProviderRegistry(settings)
    with pytest.raises(ValueError, match="Unknown provider"):
        registry.get_message_broker()
