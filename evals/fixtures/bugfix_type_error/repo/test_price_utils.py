"""Tests for price_utils. Fails until the TypeError is fixed."""

from __future__ import annotations

from price_utils import format_price, parse_price


def test_format_price_float() -> None:
    assert format_price(12.99) == "$12.99"


def test_format_price_int() -> None:
    assert format_price(10) == "$10.00"


def test_format_price_custom_currency() -> None:
    assert format_price(5.5, currency="EUR ") == "EUR 5.50"


def test_parse_price_round_trip() -> None:
    formatted = format_price(42.0)
    assert parse_price(formatted) == 42.0
