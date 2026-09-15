"""Shared scaffolding for Console step-router tests.

Step-router tests build their app through ``make_harness``/``build_console_app``
here rather than calling ``create_console_app`` (which discovers and mounts
every step router, A2) directly, so they get a signed-in client, a real
``ConfigStore`` and faked outbound HTTP for free. A handful of tests that
exercise ``create_console_app`` itself -- its signature, its wiring of
``app.state``, discovery/mounting behaviour -- call it directly instead (see
``tests/unit/test_console_app.py``, ``test_console_steps.py`` and
``test_cli_serve.py``); this module intentionally does not wrap those cases.
Outbound HTTP made by routers goes to ``handler`` (an ``httpx.MockTransport``
handler); with no handler, any outbound call gets a 599 so an unexpected
network call fails the test loudly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from henchmen.console.app import ConsoleMode, create_console_app
from henchmen.console.auth import SESSION_COOKIE, ConsoleAuth
from henchmen.console.config_store import ConfigStore
from henchmen.console.state import SetupStateStore

LOCAL = "http://127.0.0.1:8000"
ORIGIN = {"origin": LOCAL}

Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class ConsoleHarness:
    """A Console app, a client for it and the stores behind it."""

    app: FastAPI
    client: TestClient
    setup_store: SetupStateStore
    config_store: ConfigStore
    auth: ConsoleAuth
    outbound: list[httpx.Request] = field(default_factory=list)

    def get(self, path: str, **params: str) -> httpx.Response:
        return self.client.get(path, params=params)

    def post(self, path: str, json: dict[str, Any] | None = None) -> httpx.Response:
        return self.client.post(path, json=json, headers=ORIGIN)


def build_console_app(
    tmp_path: Path,
    *,
    mode: ConsoleMode = ConsoleMode.SETUP,
    config_store: ConfigStore | None = None,
) -> tuple[FastAPI, SetupStateStore, ConsoleAuth]:
    """Create the real Console app over files in ``tmp_path``."""
    setup_store = SetupStateStore(tmp_path / "setup-state.json")
    auth = ConsoleAuth(setup_token="tok", signing_key=b"k" * 32)
    app = create_console_app(
        mode=mode,
        store=setup_store,
        auth=auth,
        config_file=tmp_path / "henchmen.env",
        secrets_dir=tmp_path / "secrets",
        on_apply=lambda: None,
        config_store=config_store,
    )
    return app, setup_store, auth


def make_harness(
    tmp_path: Path,
    *,
    mode: ConsoleMode = ConsoleMode.SETUP,
    handler: Handler | None = None,
    signed_in: bool = True,
) -> ConsoleHarness:
    """A signed-in (by default) client for the Console with faked outbound HTTP."""
    config_store = ConfigStore(config_file=tmp_path / "henchmen.env", secrets_dir=tmp_path / "secrets")
    app, setup_store, auth = build_console_app(tmp_path, mode=mode, config_store=config_store)
    outbound: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        outbound.append(request)
        if handler is None:
            return httpx.Response(599, json={"message": "unexpected outbound request in a test"})
        return handler(request)

    app.state.http_client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(route), timeout=5.0)
    client = TestClient(app, base_url=LOCAL, follow_redirects=False)
    if signed_in:
        client.cookies.set(SESSION_COOKIE, auth.issue_session())
    return ConsoleHarness(
        app=app, client=client, setup_store=setup_store, config_store=config_store, auth=auth, outbound=outbound
    )
