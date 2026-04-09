"""Tests for the provider registry."""

from unittest.mock import MagicMock

import pytest

from henchmen.providers.registry import ProviderRegistry


def _mock_settings(**overrides):
    defaults = {
        "provider": "local",
        "message_broker_provider": "",
        "document_store_provider": "",
        "object_store_provider": "",
        "container_orchestrator_provider": "",
        "llm_provider": "",
        "ci_provider": "",
        "gcp_project_id": "test-project",
        "gcp_region": "us-central1",
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


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
