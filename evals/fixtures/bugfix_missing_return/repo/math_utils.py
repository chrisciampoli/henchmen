"""Simple math utilities.

BUG: ``find_max`` computes the correct result but never returns it.
The fix is to add ``return result`` at the end of the function.
"""

from __future__ import annotations


def find_max(numbers: list[int]) -> int | None:
    """Return the largest number in the list, or None if empty.

    BUG: the function computes ``result`` correctly but never returns it.
    """
    if not numbers:
        return None
    result = numbers[0]
    for n in numbers[1:]:
        if n > result:
            result = n
    # BUG: missing ``return result`` here


def find_min(numbers: list[int]) -> int | None:
    """Return the smallest number in the list, or None if empty."""
    if not numbers:
        return None
    result = numbers[0]
    for n in numbers[1:]:
        if n < result:
            result = n
    return result
