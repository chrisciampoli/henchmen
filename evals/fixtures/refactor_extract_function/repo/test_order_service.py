"""Tests for order_service.

``test_validate_email_extracted`` fails today because ``validate_email``
doesn't exist yet — the agent must extract it as part of the refactor.
The existing ``create_order`` behaviour must continue to work.
"""

from __future__ import annotations

import pytest
from order_service import create_order


def test_create_order_happy_path() -> None:
    result = create_order("alice@example.com", [{"qty": 2, "price": 5}])
    assert result["status"] == "ok"
    assert result["total"] == 10


def test_create_order_rejects_missing_at_sign() -> None:
    assert create_order("alice", [{"qty": 1, "price": 5}])["status"] == "error"


def test_create_order_rejects_empty_cart() -> None:
    assert create_order("alice@example.com", [])["status"] == "error"


def test_validate_email_extracted() -> None:
    """The refactor must expose a top-level ``validate_email`` function."""
    try:
        from order_service import validate_email
    except ImportError:
        pytest.fail("validate_email has not been extracted into order_service")

    assert validate_email("alice@example.com") is True
    assert validate_email("alice") is False
    assert validate_email("") is False
