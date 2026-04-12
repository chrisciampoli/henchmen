"""Tests for inventory.py.

After the rename, the keyword argument ``lst=`` must become ``items=``.
These tests use keyword arguments so they will fail until both the
module and the tests are updated.
"""

from __future__ import annotations

from inventory import count_in_stock, filter_by_category, total_value

SAMPLE = [
    {"name": "Widget", "price": 9.99, "in_stock": True, "category": "A"},
    {"name": "Gadget", "price": 24.99, "in_stock": False, "category": "B"},
    {"name": "Doohickey", "price": 4.50, "in_stock": True, "category": "A"},
]


def test_count_in_stock() -> None:
    assert count_in_stock(items=SAMPLE) == 2


def test_total_value() -> None:
    assert total_value(items=SAMPLE) == 9.99 + 24.99 + 4.50


def test_filter_by_category() -> None:
    result = filter_by_category(items=SAMPLE, category="A")
    assert len(result) == 2
    assert all(e["category"] == "A" for e in result)


def test_count_in_stock_empty() -> None:
    assert count_in_stock(items=[]) == 0
