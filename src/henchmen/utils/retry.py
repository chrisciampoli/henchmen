"""Exponential backoff retry for Vertex AI and other async API calls.

Replaces the duplicated inline retry logic in agent_builder.py with a
reusable utility that handles rate limiting (429) and transient failures.

Classification strategy (K3 fix):
    1. Exception-type dispatch against a lazily-populated tuple of known
       transient types from google-api-core / anthropic / openai plus
       stdlib ``TimeoutError`` / ``ConnectionError``. This is the primary
       path — resilient to message wording changes.
    2. Fallback substring match against ``str(exc)`` for unknown types so
       callers using bespoke SDKs still benefit from retry.

Backoff uses full jitter (``random.uniform(0, capped_delay)``) so that
parallel operatives don't re-collide on the same upstream after a rate
limit. A ``max_total_elapsed`` deadline is supported to bound worst-case
retry time regardless of ``max_retries``.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Exception message substrings used as a fallback classifier for unknown
# exception types (e.g. bespoke SDKs that don't inherit from the lazily
# loaded vendor hierarchies below).
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

# Always-on retryable types from the standard library.
_BASE_RETRYABLE_EXC_TYPES: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
)

# Populated lazily on first call to ``_is_retryable`` so this module can be
# imported in environments that don't have every vendor SDK installed.
_retryable_exc_types: tuple[type[BaseException], ...] | None = None


def _load_retryable_exc_types() -> tuple[type[BaseException], ...]:
    """Discover and cache the set of retryable exception types.

    Each vendor import is wrapped in try/except ImportError so the helper
    remains usable in stripped-down environments (e.g. unit tests without
    google-cloud-*, workers that only use one LLM provider, etc.).
    """
    discovered: list[type[BaseException]] = list(_BASE_RETRYABLE_EXC_TYPES)

    try:
        from google.api_core import exceptions as gapi_exc

        discovered.extend(
            [
                gapi_exc.DeadlineExceeded,
                gapi_exc.ResourceExhausted,
                gapi_exc.ServiceUnavailable,
                gapi_exc.Aborted,
                gapi_exc.InternalServerError,
                gapi_exc.Unknown,
            ]
        )
    except ImportError:
        pass

    try:
        import anthropic

        discovered.extend(
            [
                anthropic.APIStatusError,
                anthropic.RateLimitError,
                anthropic.APIConnectionError,
                anthropic.APITimeoutError,
            ]
        )
    except ImportError:
        pass

    try:
        import openai

        discovered.extend(
            [
                openai.APIStatusError,
                openai.RateLimitError,
                openai.APIConnectionError,
                openai.APITimeoutError,
            ]
        )
    except ImportError:
        pass

    return tuple(discovered)


def _is_retryable(exc: Exception) -> bool:
    """Check if an exception is retryable.

    Uses exception-type dispatch first (stable across SDK version bumps)
    and falls back to substring matching only for unknown types. Logs the
    mechanism used so operators can tell from production logs why a retry
    was classified.
    """
    global _retryable_exc_types
    if _retryable_exc_types is None:
        _retryable_exc_types = _load_retryable_exc_types()

    # Primary: exception-type dispatch.
    if isinstance(exc, _retryable_exc_types):
        logger.debug(
            "Retry classifier matched: exception type %s",
            type(exc).__name__,
        )
        return True

    # Fallback: substring match for unknown types.
    msg = str(exc)
    for pattern in _RETRYABLE_PATTERNS:
        if pattern in msg:
            logger.debug(
                "Retry classifier matched: substring match on %r",
                pattern,
            )
            return True

    return False


async def retry_with_backoff[T](
    fn: Callable[..., Awaitable[T]],
    *args: object,
    max_retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    max_total_elapsed: float | None = None,
    **kwargs: object,
) -> T:
    """Call an async function with exponential backoff on retryable errors.

    Args:
        fn: Async function to call.
        *args: Positional arguments for fn.
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds (doubles each retry).
        max_delay: Maximum delay between retries (cap for the backoff
            window — actual sleep is drawn uniformly from ``[0, cap]``).
        max_total_elapsed: Optional hard deadline in seconds. If set and
            the elapsed wall-clock exceeds this before a successful call,
            the last exception is raised even if ``max_retries`` has not
            been reached.
        **kwargs: Keyword arguments for fn.

    Returns:
        The return value of fn.

    Raises:
        The last exception if all retries are exhausted or the
        ``max_total_elapsed`` deadline is reached.
    """
    last_exc: Exception | None = None
    start = time.monotonic()

    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt >= max_retries:
                raise

            # Honor the overall deadline before scheduling another sleep.
            if max_total_elapsed is not None:
                elapsed = time.monotonic() - start
                if elapsed > max_total_elapsed:
                    logger.warning(
                        "Retry deadline exceeded (%.1fs > %.1fs) after attempt %d/%d: %s",
                        elapsed,
                        max_total_elapsed,
                        attempt + 1,
                        max_retries,
                        exc,
                    )
                    raise

            # Full jitter: draw sleep uniformly from [0, capped exponential].
            cap = min(base_delay * (2**attempt), max_delay)
            delay = random.uniform(0, cap)
            logger.warning(
                "Retryable error (attempt %d/%d), waiting %.1fs (cap=%.1fs): %s",
                attempt + 1,
                max_retries,
                delay,
                cap,
                exc,
            )
            await asyncio.sleep(delay)

            # Re-check deadline after sleeping so we don't issue one more
            # call past the budget.
            if max_total_elapsed is not None:
                elapsed = time.monotonic() - start
                if elapsed > max_total_elapsed:
                    logger.warning(
                        "Retry deadline exceeded after sleep (%.1fs > %.1fs): %s",
                        elapsed,
                        max_total_elapsed,
                        exc,
                    )
                    raise

    # Should not reach here, but satisfy type checker
    raise last_exc  # type: ignore[misc]
