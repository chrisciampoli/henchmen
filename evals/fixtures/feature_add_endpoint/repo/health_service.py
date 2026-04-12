"""Minimal health-check service.

Provides a /health endpoint. The task is to add a /status endpoint
with version and uptime information.
"""

from __future__ import annotations


def health() -> dict[str, str]:
    """Return a basic health check response."""
    return {"status": "ok"}
