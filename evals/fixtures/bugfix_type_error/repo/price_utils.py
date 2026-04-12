"""Price formatting utilities.

BUG: ``format_price`` concatenates the currency symbol with the raw value
before formatting, which raises ``TypeError`` when ``amount`` is an int.
The fix is to format the number first, then concatenate.
"""

from __future__ import annotations


def format_price(amount: float | int, currency: str = "$") -> str:
    """Return a formatted price string like ``$12.99``.

    BUG: the string concatenation ``currency + amount`` fails when
    ``amount`` is not already a string. The fix is to use f-string
    formatting: ``f"{currency}{amount:.2f}"``.
    """
    return currency + amount  # type: ignore[operator]  # bug: should format first


def parse_price(text: str) -> float:
    """Parse a price string like ``$12.99`` and return the numeric value."""
    cleaned = text.lstrip("$").strip()
    return float(cleaned)
