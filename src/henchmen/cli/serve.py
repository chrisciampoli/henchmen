"""Single-process app for ``henchmen serve``: Dispatch, Mastermind and Forge mounted on one FastAPI app.

Starlette never runs a *mounted* application's lifespan, so the parent
lifespan here enters each sub-app's lifespan explicitly and exits them in
reverse order. Without that, Dispatch never starts the Slack Socket Mode bot
and Mastermind never registers its ``/metrics`` router.

All three sub-apps share one message broker (the in-memory forwarding
singleton) and one document store, created here and closed on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, suppress
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI

# uvicorn exits 3 when startup fails. Its constant is private and moves between
# releases (uvicorn.main in 0.41, uvicorn.config in 0.53, where Server.run also
# calls sys.exit(3) itself), so the value is pinned here rather than imported.
STARTUP_FAILURE = 3

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger("henchmen")


@contextmanager
def _uvicorn_owns_signals(loop: asyncio.AbstractEventLoop) -> Iterator[None]:
    """Stop sub-app lifespans from taking over SIGTERM from uvicorn.

    Mastermind and Forge register a SIGTERM handler with
    ``loop.add_signal_handler``. In the combined process that would replace
    uvicorn's own handler, so ``docker stop`` / SIGTERM would no longer shut the
    server down; outside the main thread (a test client) it raises instead.
    uvicorn owns process signals here, so the call is a logged no-op while the
    sub-app lifespans start. Loops whose methods cannot be replaced (uvloop)
    get uvicorn's handler put back afterwards instead.
    """
    saved = signal.getsignal(signal.SIGTERM)

    def _skip(sig: int, callback: Any, *args: Any) -> None:  # Any: mirrors the asyncio signature
        logger.debug("[serve] Leaving signal %s to uvicorn (sub-app handler %r not installed)", sig, callback)

    try:
        loop.add_signal_handler = _skip  # type: ignore[method-assign, assignment]
        patched = True
    except (AttributeError, TypeError):
        patched = False
    try:
        yield
    finally:
        if patched:
            del loop.add_signal_handler
        elif (
            threading.current_thread() is threading.main_thread()
            and saved is not None
            and signal.getsignal(signal.SIGTERM) != saved
        ):
            with suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, saved)


async def _aclose(resource: object, name: str) -> None:
    """Close a provider if it exposes ``aclose`` (or ``close``), logging rather than masking failures."""
    closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.warning("[serve] Failed to close %s", name, exc_info=True)


def build_serve_app(settings: Settings, port: int, console: FastAPI | None = None) -> FastAPI:
    """Build the combined local app; mount ``console`` at / after the services when given."""
    from henchmen import __version__
    from henchmen.dispatch.server import app as dispatch_app
    from henchmen.forge.server import app as forge_app
    from henchmen.mastermind.server import app as mastermind_app
    from henchmen.providers.local.memory import InMemoryMessageBroker, default_forward_map, set_shared_broker
    from henchmen.providers.registry import ProviderRegistry

    # One broker for every mounted service. InMemoryMessageBroker() returns the
    # shared instance once set, so a sub-app lifespan that builds "its own"
    # broker gets this one. The forward map simulates Pub/Sub push delivery.
    shared_broker = InMemoryMessageBroker()
    forward_map = default_forward_map(settings, f"http://localhost:{port}")
    shared_broker.set_forward_map(forward_map)
    set_shared_broker(shared_broker)
    logger.info("Shared broker configured with forward map for %d topics", len(forward_map))

    registry = ProviderRegistry(settings)
    shared_store = registry.get_document_store()

    # Seed state before the sub-app lifespans run: Mastermind builds its agent
    # from whatever is already on app.state.
    dispatch_app.state.message_broker = shared_broker
    mastermind_app.state.message_broker = shared_broker
    mastermind_app.state.document_store = shared_store
    mastermind_app.state.container_orchestrator = registry.get_container_orchestrator()
    forge_app.state.message_broker = shared_broker
    forge_app.state.ci_provider = registry.get_ci_provider()
    forge_app.state.document_store = shared_store

    sub_apps: tuple[FastAPI, ...] = (dispatch_app, mastermind_app, forge_app)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            async with AsyncExitStack() as stack:
                with _uvicorn_owns_signals(asyncio.get_running_loop()):
                    for sub_app in sub_apps:
                        await stack.enter_async_context(sub_app.router.lifespan_context(sub_app))
                # A sub-app lifespan may have replaced a shared provider with its
                # own instance; close the duplicate and restore the shared one.
                shared: dict[str, object] = {"message_broker": shared_broker, "document_store": shared_store}
                for sub_app in sub_apps:
                    for attr, instance in shared.items():
                        current = getattr(sub_app.state, attr, None)
                        if current is not None and current is not instance:
                            await _aclose(current, f"{sub_app.title} {attr}")
                            setattr(sub_app.state, attr, instance)
                logger.info("All services initialized")
                try:
                    yield
                finally:
                    await shared_broker.drain()
                    logger.info("Shutting down")
                # Leaving the AsyncExitStack runs the sub-app shutdowns in reverse order.
        finally:
            await _aclose(shared_store, "shared document store")

    app = FastAPI(title="Henchmen (Local Dev)", version=__version__, lifespan=lifespan)
    app.mount("/dispatch", dispatch_app)
    app.mount("/mastermind", mastermind_app)
    app.mount("/forge", forge_app)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "mode": "local", "services": ["dispatch", "mastermind", "forge"]}

    if console is not None:
        # Mounted last so it only receives paths no service or /health claims.
        app.mount("/", console)

    return app


# ---------------------------------------------------------------------------
# Setup mode and restart-to-apply
# ---------------------------------------------------------------------------

RESTART_EXIT_CODE = 75  # EX_TEMPFAIL; distinct from uvicorn's own STARTUP_FAILURE (3)


class RestartSignal:
    """Lets the Console ask the running uvicorn server to stop so the container restarts it."""

    def __init__(self) -> None:
        self.requested = False
        self._server: Any = None

    def attach(self, server: Any) -> None:
        self._server = server
        if self.requested:
            server.should_exit = True

    def request(self) -> None:
        self.requested = True
        if self._server is not None:
            self._server.should_exit = True


def configure_serve_logging(log_level: str) -> None:
    """Configure logging for ``henchmen serve`` with secret redaction installed first.

    Installed before any service module is imported and in setup mode too, so
    uvicorn's access log never records the Console sign-in token (or any other
    known secret) in a request line.
    """
    from henchmen.utils.redaction import install_secret_redaction

    install_secret_redaction()
    logging.basicConfig(level=getattr(logging, log_level.upper()))


def console_url(port: int, setup_token: str) -> str:
    """The sign-in URL the launcher opens (and serve prints for engineers)."""
    return f"http://127.0.0.1:{port}/console/session?setup_token={setup_token}"


def build_setup_app(console: FastAPI) -> FastAPI:
    """Setup mode: /health plus the Console. No provider is built."""
    from henchmen import __version__

    app = FastAPI(title="Henchmen (setup)", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "setup"}

    app.mount("/", console)
    return app


def serve_app(app: FastAPI, *, host: str, port: int, log_level: str, restart: RestartSignal) -> int:
    """Run ``app`` until it stops; return the process exit code.

    Mirrors ``uvicorn.run``'s own handling, which bare ``Server.run()`` does not
    provide: a plain ``KeyboardInterrupt`` (Ctrl+C) is swallowed rather than
    left to print a traceback, and a server whose lifespan never started
    (``server.started`` still ``False``) exits with uvicorn's own
    ``STARTUP_FAILURE`` code so a genuine startup failure is distinguishable
    from a clean stop. A requested restart takes priority over both.
    """
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level=log_level))
    restart.attach(server)
    with suppress(KeyboardInterrupt):
        server.run()
    if restart.requested:
        return RESTART_EXIT_CODE
    if not server.started:
        return STARTUP_FAILURE
    return 0
