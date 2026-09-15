"""FastAPI dependencies shared by the Console step routers.

Routers never build stores or HTTP clients themselves: ``create_console_app``
puts them on ``app.state`` and these functions hand them out, which is also
where tests substitute fakes (``app.state.http_client_factory``).

The setup-state store accessor lives in ``henchmen.console.steps.get_setup_store``
(ruling C11) -- it is not redefined here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import httpx
from fastapi import Request

from henchmen.console.config_store import ConfigStore

HttpClientFactory = Callable[[], httpx.AsyncClient]
DEFAULT_HTTP_TIMEOUT = 10.0


def default_http_client() -> httpx.AsyncClient:
    """Client for outbound API calls made directly by step routers."""
    return httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT)


def get_config_store(request: Request) -> ConfigStore:
    """The Console's configuration writer."""
    return cast(ConfigStore, request.app.state.config_store)


def get_http_client_factory(request: Request) -> HttpClientFactory:
    """Factory for outbound ``httpx.AsyncClient`` instances (replaced in tests)."""
    factory = getattr(request.app.state, "http_client_factory", None)
    return cast(HttpClientFactory, factory) if factory is not None else default_http_client
