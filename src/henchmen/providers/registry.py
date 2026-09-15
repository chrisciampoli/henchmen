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
from henchmen.providers.tiers import CANONICAL_LLM_PROVIDERS, LLM_PROVIDER_ALIASES, normalize_llm_provider

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


def orchestrator_is_local(settings: Settings) -> bool:
    """True when the *effective* container orchestrator is local Docker.

    The single predicate for every "does this run on a desktop Docker host"
    decision (lair environment and image, local CI gates, Forge CI and
    ``fix_lint`` routing): it honours ``HENCHMEN_CONTAINER_ORCHESTRATOR_PROVIDER``
    rather than the coarse ``provider`` setting, so no two call sites can ever
    disagree about where operative-written code runs.
    """
    return ProviderRegistry(settings).resolve_provider_name("container_orchestrator") == "local"


class ProviderRegistry:
    """Resolves provider settings to concrete implementations.

    Uses HENCHMEN_PROVIDER as default, with per-service overrides like
    HENCHMEN_LLM_PROVIDER=anthropic. LLM provider names accept the aliases
    in :data:`henchmen.providers.tiers.LLM_PROVIDER_ALIASES` (``ollama`` for
    ``local``, ``vertex`` for ``gcp``, ``bedrock`` for ``aws``).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve_provider_name(self, service: str) -> str:
        """Determine which provider to use for a given service."""
        override_field = _SERVICE_OVERRIDE_FIELDS.get(service, "")
        override = getattr(self._settings, override_field, "") if override_field else ""
        name = str(override) if override else self._settings.provider
        if service == "llm":
            return normalize_llm_provider(name)
        return name

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

            return InMemoryMessageBroker(self._settings)
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
            if self._settings.operative_task_token.strip():
                # An operative launched by a desktop install: task state over HTTP, never the data volume.
                from henchmen.providers.local.http_store import HttpDocumentStore

                return HttpDocumentStore(self._settings)
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
        valid = ", ".join(CANONICAL_LLM_PROVIDERS)
        aliases = ", ".join(sorted(LLM_PROVIDER_ALIASES))
        raise ValueError(f"Unknown provider for llm: {name!r}. Valid: {valid} (aliases: {aliases})")

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
