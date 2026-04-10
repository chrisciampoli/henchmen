"""Tests for date_helpers. These will fail until the missing import is added."""

from __future__ import annotations

from datetime import datetime, timedelta

from date_helpers import days_from_now, weeks_from_now


def test_days_from_now_returns_future_datetime() -> None:
    result = days_from_now(3)
    assert isinstance(result, datetime)
    assert result > datetime.now()
    assert result < datetime.now() + timedelta(days=4)


def test_weeks_from_now_returns_future_datetime() -> None:
    result = weeks_from_now(2)
    assert isinstance(result, datetime)
    assert result > datetime.now() + timedelta(days=13)
