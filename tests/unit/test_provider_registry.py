"""Tests for the provider registry."""

import pytest

from henchmen.providers.registry import ProviderRegistry


def _mock_settings(**overrides):
    """Build a real ``Settings`` instance with provider-field overrides.

    Seeds ``HENCHMEN_GCP_PROJECT_ID`` for the required field then applies
    per-call overrides via ``model_copy``. Default provider is ``local``
    so tests can flip individual service overrides.
    """
    import os

    from henchmen.config.settings import get_settings

    os.environ.setdefault("HENCHMEN_GCP_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    base_overrides = {"provider": "local"}
    base_overrides.update(overrides)
    return get_settings().model_copy(update=base_overrides)


def test_registry_resolves_provider_name_local():
    settings = _mock_settings(provider="local")
    registry = ProviderRegistry(settings)
    assert registry.resolve_provider_name("message_broker") == "local"


def test_registry_per_service_override():
    settings = _mock_settings(provider="gcp", llm_provider="ollama")
    registry = ProviderRegistry(settings)
    assert registry.resolve_provider_name("llm") == "ollama"
    assert registry.resolve_provider_name("message_broker") == "gcp"


def test_registry_unknown_provider_raises():
    settings = _mock_settings(provider="azure")
    registry = ProviderRegistry(settings)
    with pytest.raises(ValueError, match="Unknown provider"):
        registry.get_message_broker()
