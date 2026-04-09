"""Exponential backoff retry for Vertex AI API calls.

Replaces the duplicated inline retry logic in agent_builder.py with a
reusable utility that handles rate limiting (429) and transient failures.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Exception message substrings that indicate retryable errors
_RETRYABLE_PATTERNS = (
    "429",
    "Resource exhausted",
    "RESOURCE_EXHAUSTED",
    "ServiceUnavailable",
    "503",
    "rate",
    "quota",
    "overloaded",
)


def _is_retryable(exc: Exception) -> bool:
    """Check if an exception is retryable (rate limit or transient)."""
    msg = str(exc)
    return any(pattern in msg for pattern in _RETRYABLE_PATTERNS)


async def retry_with_backoff[T](
    fn: Callable[..., Awaitable[T]],
    *args: object,
    max_retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    **kwargs: object,
) -> T:
    """Call an async function with exponential backoff on retryable errors.

    Args:
        fn: Async function to call.
        *args: Positional arguments for fn.
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds (doubles each retry).
        max_delay: Maximum delay between retries.
        **kwargs: Keyword arguments for fn.

    Returns:
        The return value of fn.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt >= max_retries:
                raise

            delay = min(base_delay * (2**attempt), max_delay)
            logger.warning(
                "Retryable error (attempt %d/%d), waiting %.1fs: %s",
                attempt + 1,
                max_retries,
                delay,
                exc,
            )
            await asyncio.sleep(delay)

    # Should not reach here, but satisfy type checker
    raise last_exc  # type: ignore[misc]
