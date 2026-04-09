"""Provider registry — resolves settings to concrete provider instances."""

from __future__ import annotations

from typing import TYPE_CHECKING

from henchmen.providers.interfaces import (
    CIProvider,
    ContainerOrchestrator,
    DocumentStore,
    LLMProvider,
    MessageBroker,
    ObjectStore,
)

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

_VALID_PROVIDERS = {"gcp", "aws", "local"}

_SERVICE_OVERRIDE_FIELDS = {
    "message_broker": "message_broker_provider",
    "document_store": "document_store_provider",
    "object_store": "object_store_provider",
    "container_orchestrator": "container_orchestrator_provider",
    "llm": "llm_provider",
    "ci": "ci_provider",
}


class ProviderRegistry:
    """Resolves provider settings to concrete implementations.

    Uses HENCHMEN_PROVIDER as default, with per-service overrides like
    HENCHMEN_LLM_PROVIDER=ollama.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve_provider_name(self, service: str) -> str:
        """Determine which provider to use for a given service."""
        override_field = _SERVICE_OVERRIDE_FIELDS.get(service, "")
        override = getattr(self._settings, override_field, "") if override_field else ""
        return override if override else self._settings.provider

    def get_message_broker(self) -> MessageBroker:
        """Create the configured MessageBroker implementation."""
        name = self.resolve_provider_name("message_broker")
        if name == "gcp":
            from henchmen.providers.gcp.pubsub import PubSubMessageBroker

            return PubSubMessageBroker(self._settings)
        if name == "aws":
            from henchmen.providers.aws.sns import SNSMessageBroker

            return SNSMessageBroker(self._settings)
        if name == "local":
            from henchmen.providers.local.memory import InMemoryMessageBroker

            return InMemoryMessageBroker()
        raise ValueError(f"Unknown provider for message_broker: {name!r}. Valid: {_VALID_PROVIDERS}")

    def get_document_store(self) -> DocumentStore:
        """Create the configured DocumentStore implementation."""
        name = self.resolve_provider_name("document_store")
        if name == "gcp":
            from henchmen.providers.gcp.firestore import FirestoreDocumentStore

            return FirestoreDocumentStore(self._settings)
        if name == "aws":
            from henchmen.providers.aws.dynamodb import DynamoDBDocumentStore

            return DynamoDBDocumentStore(self._settings)
        if name == "local":
            from henchmen.providers.local.sqlite import SQLiteDocumentStore

            return SQLiteDocumentStore(self._settings)
        raise ValueError(f"Unknown provider for document_store: {name!r}. Valid: {_VALID_PROVIDERS}")

    def get_object_store(self) -> ObjectStore:
        """Create the configured ObjectStore implementation."""
        name = self.resolve_provider_name("object_store")
        if name == "gcp":
            from henchmen.providers.gcp.gcs import GCSObjectStore

            return GCSObjectStore(self._settings)
        if name == "aws":
            from henchmen.providers.aws.s3 import S3ObjectStore

            return S3ObjectStore(self._settings)
        if name == "local":
            from henchmen.providers.local.filesystem import FilesystemObjectStore

            return FilesystemObjectStore(self._settings)
        raise ValueError(f"Unknown provider for object_store: {name!r}. Valid: {_VALID_PROVIDERS}")

    def get_container_orchestrator(self) -> ContainerOrchestrator:
        """Create the configured ContainerOrchestrator implementation."""
        name = self.resolve_provider_name("container_orchestrator")
        if name == "gcp":
            from henchmen.providers.gcp.cloud_run import CloudRunOrchestrator

            return CloudRunOrchestrator(self._settings)
        if name == "aws":
            from henchmen.providers.aws.ecs import ECSOrchestrator

            return ECSOrchestrator(self._settings)
        if name == "local":
            from henchmen.providers.local.docker import DockerOrchestrator

            return DockerOrchestrator(self._settings)
        raise ValueError(f"Unknown provider for container_orchestrator: {name!r}. Valid: {_VALID_PROVIDERS}")

    def get_llm_provider(self) -> LLMProvider:
        """Create the configured LLMProvider implementation."""
        name = self.resolve_provider_name("llm")
        if name == "gcp":
            from henchmen.providers.gcp.vertex_ai import VertexAIProvider

            return VertexAIProvider(self._settings)
        if name == "aws":
            from henchmen.providers.aws.bedrock import BedrockProvider

            return BedrockProvider(self._settings)
        if name == "local":
            from henchmen.providers.local.ollama import OllamaProvider

            return OllamaProvider(self._settings)
        if name == "openai":
            from henchmen.providers.openai import OpenAIProvider

            return OpenAIProvider(self._settings)
        if name == "anthropic":
            from henchmen.providers.anthropic import AnthropicProvider

            return AnthropicProvider(self._settings)
        raise ValueError(f"Unknown provider for llm: {name!r}. Valid: {_VALID_PROVIDERS | {'openai', 'anthropic'}}")

    def get_ci_provider(self) -> CIProvider:
        """Create the configured CIProvider implementation."""
        name = self.resolve_provider_name("ci")
        if name == "gcp":
            from henchmen.providers.gcp.cloud_build import CloudBuildCIProvider

            return CloudBuildCIProvider(self._settings)
        if name == "aws":
            from henchmen.providers.aws.codebuild import CodeBuildCIProvider

            return CodeBuildCIProvider(self._settings)
        if name == "local":
            from henchmen.providers.local.shell_ci import ShellCIProvider

            return ShellCIProvider(self._settings)
        raise ValueError(f"Unknown provider for ci: {name!r}. Valid: {_VALID_PROVIDERS}")
