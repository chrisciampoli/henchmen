"""Inventory management utilities.

Uses the variable name ``lst`` throughout, which should be renamed to
``items`` for clarity. The rename must be consistent across parameter
names, local variables, and docstrings.
"""

from __future__ import annotations


def count_in_stock(lst: list[dict[str, object]]) -> int:
    """Return the number of entries in ``lst`` with ``in_stock`` == True."""
    total = 0
    for entry in lst:
        if entry.get("in_stock"):
            total += 1
    return total


def total_value(lst: list[dict[str, object]]) -> float:
    """Sum the ``price`` field of every entry in ``lst``."""
    return sum(float(entry.get("price", 0)) for entry in lst)


def filter_by_category(lst: list[dict[str, object]], category: str) -> list[dict[str, object]]:
    """Return entries from ``lst`` whose ``category`` matches."""
    return [entry for entry in lst if entry.get("category") == category]
