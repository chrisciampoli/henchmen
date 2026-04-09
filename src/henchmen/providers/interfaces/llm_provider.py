"""LLMProvider interface — text generation across model providers."""

from typing import Protocol, runtime_checkable

from henchmen.models.llm import LLMResponse, Message, ToolDefinition


@runtime_checkable
class LLMProvider(Protocol):
    """Abstraction over LLM APIs (Vertex AI, OpenAI, Anthropic, Ollama)."""

    async def generate(
        self,
        messages: list[Message],
        model: str,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        system_prompt: str | None = None,
    ) -> LLMResponse:
        """Generate a response from the model."""
        ...

    async def count_tokens(self, text: str, model: str) -> int:
        """Count tokens in text for the given model."""
        ...

    def supported_models(self) -> list[str]:
        """Return list of model identifiers this provider supports."""
        ...

    def resolve_tier(self, tier: str) -> str:
        """Map a model tier (default/complex, default/light, etc.) to a concrete model name."""
        ...
