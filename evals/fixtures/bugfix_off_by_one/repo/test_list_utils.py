"""Tests for list_utils — currently failing due to the off-by-one bug."""

from list_utils import first_n, last_n


def test_last_n_returns_exactly_n_items() -> None:
    assert last_n([1, 2, 3, 4, 5], 2) == [4, 5]


def test_last_n_handles_n_equal_length() -> None:
    assert last_n([1, 2, 3], 3) == [1, 2, 3]


def test_last_n_zero_returns_empty() -> None:
    assert last_n([1, 2, 3], 0) == []


def test_first_n_baseline() -> None:
    assert first_n([1, 2, 3, 4, 5], 2) == [1, 2]
