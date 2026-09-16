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

from henchmen.console.callback_state import CallbackStateStore
from henchmen.console.config_store import ConfigStore
from henchmen.console.task_gateway import TaskGateway

HttpClientFactory = Callable[[], httpx.AsyncClient]
DEFAULT_HTTP_TIMEOUT = 10.0


def default_http_client() -> httpx.AsyncClient:
    """Client for outbound API calls made directly by step routers.

    ``trust_env=False``: no proxy or netrc configuration from the environment
    sees what these calls carry (a GitHub manifest code, an app JWT).
    """
    return httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT, trust_env=False)


def get_config_store(request: Request) -> ConfigStore:
    """The Console's configuration writer."""
    return cast(ConfigStore, request.app.state.config_store)


def get_http_client_factory(request: Request) -> HttpClientFactory:
    """Factory for outbound ``httpx.AsyncClient`` instances (replaced in tests)."""
    factory = getattr(request.app.state, "http_client_factory", None)
    return cast(HttpClientFactory, factory) if factory is not None else default_http_client


def get_seeded_env(request: Request) -> dict[str, str]:
    """Defaults ``henchmen serve`` seeded into this process's environment (masked where the file sets a key)."""
    seeded = getattr(request.app.state, "seeded_env", None)
    return dict(seeded) if isinstance(seeded, dict) else {}


def get_callback_states(request: Request) -> CallbackStateStore:
    """Single-use state values for the public GitHub callbacks."""
    return cast(CallbackStateStore, request.app.state.callback_states)


def get_task_gateway(request: Request) -> TaskGateway | None:
    """The run-mode task gateway (injected by ``build_serve_app``), or ``None`` in setup mode.

    ``None`` is a real answer, not a failure: setup mode runs no services, so
    the first-task step explains that Henchmen has to be started first (A6)
    rather than hanging or raising.
    """
    return cast("TaskGateway | None", getattr(request.app.state, "task_gateway", None))
