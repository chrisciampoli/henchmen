"""Tests for math_utils. Fails until the missing return is added."""

from __future__ import annotations

from math_utils import find_max, find_min


def test_find_max_basic() -> None:
    assert find_max([1, 5, 3, 9, 2]) == 9


def test_find_max_single_element() -> None:
    assert find_max([42]) == 42


def test_find_max_empty() -> None:
    assert find_max([]) is None


def test_find_max_negative() -> None:
    assert find_max([-3, -1, -7]) == -1


def test_find_min_baseline() -> None:
    """Verify find_min works (it has the return statement)."""
    assert find_min([1, 5, 3, 9, 2]) == 1
