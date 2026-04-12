"""Tests for user_service validation. Fails until validation is added."""

from __future__ import annotations

import pytest
from user_service import register_user


def test_register_valid_user() -> None:
    result = register_user("alice123", 25)
    assert result["username"] == "alice123"
    assert result["age"] == 25
    assert result["active"] is True


def test_reject_short_username() -> None:
    with pytest.raises(ValueError):
        register_user("ab", 25)


def test_reject_long_username() -> None:
    with pytest.raises(ValueError):
        register_user("a" * 21, 25)


def test_reject_non_alphanumeric_username() -> None:
    with pytest.raises(ValueError):
        register_user("alice!@#", 25)


def test_reject_negative_age() -> None:
    with pytest.raises(ValueError):
        register_user("alice", -1)


def test_reject_age_over_150() -> None:
    with pytest.raises(ValueError):
        register_user("alice", 151)


def test_accept_boundary_age_zero() -> None:
    result = register_user("alice", 0)
    assert result["age"] == 0


def test_accept_boundary_age_150() -> None:
    result = register_user("alice", 150)
    assert result["age"] == 150
