"""LLMProvider interface — text generation across model providers."""

from typing import Protocol, runtime_checkable

from henchmen.models.llm import LLMResponse, Message, ToolDefinition


@runtime_checkable
class LLMProvider(Protocol):
    """Abstraction over LLM APIs (Vertex AI, Bedrock, OpenAI, Anthropic, Ollama).

    Contract every implementation must honour:

    * ``model`` accepts either a concrete vendor model id or a
      :class:`~henchmen.models.llm.ModelTier` value (``default/complex``,
      ``default/light``, ``default/reasoning``). Tier names are resolved from
      ``Settings`` *inside* ``generate`` and ``count_tokens`` — a tier name must
      never reach a vendor API.
    * ``LLMResponse.model`` reports the concrete model that answered.
    * ``LLMResponse.finish_reason`` is a normalised
      :class:`~henchmen.models.llm.FinishReason` value.
    * ``LLMResponse.usage.input_tokens`` is the TOTAL prompt size, including
      ``cached_tokens`` and ``cache_write_tokens``; ``estimated_cost_usd`` comes
      from :mod:`henchmen.providers.pricing`.
    """

    async def generate(
        self,
        messages: list[Message],
        model: str,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        system_prompt: str | None = None,
    ) -> LLMResponse:
        """Generate a response from the model (or tier)."""
        ...

    async def count_tokens(self, text: str, model: str) -> int:
        """Count tokens in text for the given model (or tier)."""
        ...

    def supported_models(self) -> list[str]:
        """Return the concrete model identifiers configured for this provider."""
        ...

    def resolve_tier(self, tier: str) -> str:
        """Map a model tier (default/complex, default/light, ...) to a concrete model name."""
        ...
