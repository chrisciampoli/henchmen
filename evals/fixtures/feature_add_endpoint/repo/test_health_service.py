"""Tests for health_service. test_status_endpoint fails until the endpoint is added."""

from __future__ import annotations

from health_service import health


def test_health_returns_ok() -> None:
    assert health() == {"status": "ok"}


def test_status_endpoint() -> None:
    """The status function must exist and return the expected shape."""
    from health_service import status

    result = status()
    assert result["status"] == "ok"
    assert result["version"] == "1.0.0"
    assert isinstance(result["uptime_seconds"], int)
    assert result["uptime_seconds"] >= 0
