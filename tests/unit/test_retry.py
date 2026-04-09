"""Unit tests for ``henchmen.utils.retry``.

Covers ``retry_with_backoff`` and ``_is_retryable`` — both critical hot-path
helpers that wrap every Vertex AI / Anthropic / OpenAI call. Previously
untested; see expert-panel finding R6.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from henchmen.utils.retry import _is_retryable, retry_with_backoff


class TestRetryWithBackoff:
    """Behavioural tests for ``retry_with_backoff``."""

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_first_attempt(self):
        """A function that returns immediately is not retried."""
        fn = AsyncMock(return_value="ok")

        result = await retry_with_backoff(fn, max_retries=3, base_delay=0.001)

        assert result == "ok"
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_eventually_succeeds_after_transient_errors(self):
        """Two ``TimeoutError`` raises then a success returns the success."""
        call_count = {"n": 0}

        async def flaky() -> str:
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise TimeoutError("transient")
            return "finally"

        result = await retry_with_backoff(flaky, max_retries=5, base_delay=0.001, max_delay=0.002)

        assert result == "finally"
        assert call_count["n"] == 3

    @pytest.mark.asyncio
    async def test_retry_gives_up_after_max_retries(self):
        """An always-failing retryable function raises the last exception."""
        call_count = {"n": 0}

        async def always_fails() -> None:
            call_count["n"] += 1
            raise TimeoutError(f"attempt #{call_count['n']}")

        with pytest.raises(TimeoutError) as excinfo:
            await retry_with_backoff(always_fails, max_retries=2, base_delay=0.001, max_delay=0.002)

        # max_retries=2 means up to 3 attempts total (0, 1, 2)
        assert call_count["n"] == 3
        assert "attempt #3" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_retry_respects_max_total_elapsed(self):
        """The ``max_total_elapsed`` deadline short-circuits further retries."""
        call_count = {"n": 0}

        async def slow_fail() -> None:
            call_count["n"] += 1
            # Burn a little wall-clock so the deadline is crossed quickly.
            await asyncio.sleep(0.02)
            raise TimeoutError("always")

        with pytest.raises(TimeoutError):
            await retry_with_backoff(
                slow_fail,
                max_retries=100,  # intentionally very high
                base_delay=0.001,
                max_delay=0.005,
                max_total_elapsed=0.01,
            )

        # The deadline should have cut us off well before 100 retries.
        assert call_count["n"] < 10


class TestIsRetryable:
    """Behavioural tests for ``_is_retryable`` classification."""

    def test_is_retryable_classifies_timeout_error_as_retryable(self):
        assert _is_retryable(TimeoutError("deadline exceeded")) is True

    def test_is_retryable_classifies_valueerror_as_non_retryable(self):
        assert _is_retryable(ValueError("bad argument")) is False

    def test_is_retryable_uses_substring_fallback_for_unknown_types(self):
        """Unknown exception type whose message contains a known substring
        (e.g. '429' or 'rate') falls back to substring-based classification."""

        class BespokeError(Exception):
            pass

        assert _is_retryable(BespokeError("429 Too Many Requests")) is True
        assert _is_retryable(BespokeError("rate limit hit")) is True
        assert _is_retryable(BespokeError("totally unrelated")) is False


class TestFullJitter:
    """Verify ``retry_with_backoff`` applies full-jitter backoff."""

    @pytest.mark.asyncio
    async def test_full_jitter_varies_delay(self, monkeypatch):
        """Run many retry cycles and assert the recorded sleep durations are
        not all identical — that's the only way to prove random jitter is
        actually being applied on top of the exponential cap.
        """
        sleeps: list[float] = []

        real_sleep = asyncio.sleep

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            # Still yield to the event loop, but don't actually wait.
            await real_sleep(0)

        monkeypatch.setattr("henchmen.utils.retry.asyncio.sleep", fake_sleep)

        async def failing() -> None:
            raise TimeoutError("retry me")

        # 100 cycles each with 1 retry = 100 recorded sleeps.
        for _ in range(100):
            with pytest.raises(TimeoutError):
                await retry_with_backoff(failing, max_retries=1, base_delay=2.0, max_delay=10.0)

        assert len(sleeps) == 100
        # Full jitter draws from uniform(0, cap). The odds of every draw in
        # 100 independent trials being identical are effectively zero.
        assert len(set(sleeps)) > 1, "Expected jittered delays to vary between retries"
        assert all(0 <= s <= 10.0 for s in sleeps), "All delays must lie within [0, max_delay]"
