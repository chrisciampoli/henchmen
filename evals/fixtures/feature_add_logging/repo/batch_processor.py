"""Fixture module — a tiny batch processor with no logging.

The feature_add_logging eval expects the operative to import ``logging``
and emit a DEBUG-level log line per item without changing behaviour.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable


def process_batch(items: Iterable[int], transform: Callable[[int], int]) -> list[int]:
    """Apply ``transform`` to each element and return the results."""
    results: list[int] = []
    for item in items:
        results.append(transform(item))
    return results


def double(x: int) -> int:
    return x * 2
