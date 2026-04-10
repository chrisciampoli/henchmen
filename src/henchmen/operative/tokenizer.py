"""Token counting for the Operative runtime.

Replaces all ``len(text) // 4`` heuristics with accurate token counts.
When an LLMProvider is supplied, delegates to ``provider.count_tokens()`` so
token counting is provider-agnostic. Falls back to the google-genai SDK tokenizer
when available, and ultimately falls back to the character heuristic (4 chars ≈
1 token) when neither is accessible (e.g. during unit tests or local dev).
"""

import asyncio
import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from henchmen.providers.interfaces import LLMProvider

logger = logging.getLogger(__name__)

# Cache tokenizer instances per model to avoid repeated initialization
_tokenizer_cache: dict[str, Any] = {}


@lru_cache(maxsize=1)
def _sdk_available() -> bool:
    """Check if google-genai SDK with tokenizer support is available."""
    try:
        from google import genai  # noqa: F401

        return True
    except ImportError:
        logger.info("google-genai SDK not available, using character-based token estimates")
        return False


def _get_tokenizer(model_name: str) -> Any:
    """Get or create a cached SDK tokenizer client for the given model."""
    if model_name in _tokenizer_cache:
        return _tokenizer_cache[model_name]

    try:
        from google import genai

        client = genai.Client(vertexai=True)
        _tokenizer_cache[model_name] = client
        return client
    except Exception as exc:
        logger.debug("Failed to create tokenizer for %s: %s", model_name, exc)
        _tokenizer_cache[model_name] = None
        return None


def count_tokens(
    text: str,
    model_name: str = "gemini-2.5-pro",
    provider: "LLMProvider | None" = None,
) -> int:
    """Count tokens in text, using the LLMProvider when available.

    Resolution order:
    1. If ``provider`` is supplied, call ``provider.count_tokens()`` via asyncio.
    2. If the google-genai SDK is available, use its tokenizer.
    3. Fallback: ``len(text) // 4`` (4 chars ≈ 1 token).

    Args:
        text: The text to count tokens for.
        model_name: The model whose tokenizer to use.
        provider: Optional LLMProvider; when given, delegates to it.

    Returns:
        Approximate token count.
    """
    if not text:
        return 0

    # Provider path: run the async method synchronously via asyncio
    if provider is not None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Inside an async context — create a task via run_coroutine_threadsafe

                future = asyncio.run_coroutine_threadsafe(provider.count_tokens(text, model_name), loop)
                return int(future.result(timeout=10))
            return int(loop.run_until_complete(provider.count_tokens(text, model_name)))
        except Exception as exc:
            logger.debug("Provider token counting failed, using fallback: %s", exc)
            return len(text) // 4

    # SDK path
    if not _sdk_available():
        return len(text) // 4

    client = _get_tokenizer(model_name)
    if client is None:
        return len(text) // 4

    try:
        response = client.models.count_tokens(model=model_name, contents=text)
        return int(getattr(response, "total_tokens", 0))
    except Exception as exc:
        logger.debug("Token counting failed for model %s, using fallback: %s", model_name, exc)
        return len(text) // 4


def estimate_tokens(text: str) -> int:
    """Quick token estimate without SDK call (4 chars ≈ 1 token).

    Use this for non-critical estimates where speed matters more than
    accuracy (e.g. progress logging). For budget/ceiling decisions,
    use ``count_tokens()`` instead.
    """
    return len(text) // 4 if text else 0
