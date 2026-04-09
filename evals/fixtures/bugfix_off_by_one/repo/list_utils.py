"""Small list utility module used as an evaluation fixture.

Contains a deliberate off-by-one bug in ``last_n`` so the bugfix_standard
scheme has something to repair. Do not fix this file outside the harness.
"""

from __future__ import annotations


def last_n(items: list[int], n: int) -> list[int]:
    """Return the final ``n`` items from ``items``.

    BUG: the slice uses ``-n-1`` so it returns ``n + 1`` elements instead
    of ``n``. The fix is to replace the slice with ``items[-n:]``.
    """
    if n <= 0:
        return []
    return items[-n - 1 :]


def first_n(items: list[int], n: int) -> list[int]:
    """Return the first ``n`` items from ``items`` (reference implementation)."""
    if n <= 0:
        return []
    return items[:n]
