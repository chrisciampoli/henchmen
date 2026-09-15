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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI

# uvicorn exits 3 when startup fails. Its constant is private and moves between
# releases (uvicorn.main in 0.41, uvicorn.config in 0.53, where Server.run also
# calls sys.exit(3) itself), so the value is pinned here rather than imported.
STARTUP_FAILURE = 3

if TYPE_CHECKING:
    from henchmen.config.settings import Settings
    from henchmen.console.services import ServiceHealth

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


def _close_after_build_failure(resource: object, name: str) -> None:
    """Close a resource ``build_serve_app`` already created before it failed to finish building.

    No lifespan will ever run for a store or broker built this early -- the failure happens
    before ``build_serve_app`` ever returns an app for uvicorn to run -- so it must be closed
    here instead of leaking. ``build_serve_app`` is a plain (non-async) function: with no
    running event loop this spins one up to await an async ``close``/``aclose``. Called from
    inside a running loop (async code building the app), the close is scheduled on that loop
    instead -- ``asyncio.run`` would refuse, and blocking on it would deadlock -- and a strong
    reference is kept until it finishes.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_aclose(resource, name))
        return
    task = loop.create_task(_aclose(resource, name))
    _pending_closes.add(task)
    task.add_done_callback(_pending_closes.discard)


# Closes scheduled by _close_after_build_failure on an already-running loop; the loop only
# keeps weak references to tasks, so these are held here until each finishes.
_pending_closes: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class DesktopRuntime:
    """What ``build_serve_app`` needs to harden a desktop (data-directory) install."""

    allowed_hostnames: frozenset[str]
    internal_push_token: str = field(default="", repr=False)


def build_serve_app(
    settings: Settings,
    port: int,
    console: FastAPI | None = None,
    *,
    desktop: DesktopRuntime | None = None,
    health: ServiceHealth | None = None,
) -> FastAPI:
    """Build the combined local app; mount ``console`` at / after the services when given.

    ``desktop`` (data-directory installs only) adds the whole-app Host allowlist
    and authenticates internal pushes. ``health`` records each service's state
    and a startup failure for ``/console/api/status`` and needs-attention mode.
    """
    from henchmen import __version__
    from henchmen.console.services import SERVICE_NAMES, ServiceHealth, ServiceState
    from henchmen.dispatch.server import app as dispatch_app
    from henchmen.forge.server import app as forge_app
    from henchmen.mastermind.server import app as mastermind_app
    from henchmen.providers.local.memory import (
        InMemoryMessageBroker,
        default_forward_map,
        get_shared_broker,
        set_shared_broker,
    )
    from henchmen.providers.registry import ProviderRegistry
    from henchmen.utils.lifespan import run_shutdown

    # One broker for every mounted service. InMemoryMessageBroker() returns the
    # shared instance once set, so a sub-app lifespan that builds "its own"
    # broker gets this one. The forward map simulates Pub/Sub push delivery.
    shared_broker = InMemoryMessageBroker()
    forward_map = default_forward_map(settings, f"http://localhost:{port}")
    shared_broker.set_forward_map(forward_map)
    # Desktop installs authenticate the simulated Pub/Sub pushes (D-P3); a
    # repository checkout sends none, as before.
    shared_broker.set_forward_token(desktop.internal_push_token if desktop is not None else None)
    set_shared_broker(shared_broker)
    logger.info("Shared broker configured with forward map for %d topics", len(forward_map))

    registry = ProviderRegistry(settings)
    shared_store = registry.get_document_store()

    try:
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
        tracker = health if health is not None else ServiceHealth()

        async def _close_shared_store() -> None:
            await _aclose(shared_store, "shared document store")

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            tracker.set_all(ServiceState.STARTING)
            original: BaseException | None = None
            try:
                async with AsyncExitStack() as stack:
                    with _uvicorn_owns_signals(asyncio.get_running_loop()):
                        for name, sub_app in zip(SERVICE_NAMES, sub_apps, strict=True):
                            try:
                                await stack.enter_async_context(sub_app.router.lifespan_context(sub_app))
                            except Exception as exc:
                                # Recorded, then re-raised: uvicorn reports a startup failure and
                                # `henchmen serve` decides whether to serve the needs-attention Console.
                                tracker.record_startup_failure(name, exc)
                                raise
                    # A sub-app lifespan may have replaced a shared provider with its
                    # own instance; close the duplicate and restore the shared one.
                    shared: dict[str, object] = {"message_broker": shared_broker, "document_store": shared_store}
                    for sub_app in sub_apps:
                        for attr, instance in shared.items():
                            current = getattr(sub_app.state, attr, None)
                            if current is not None and current is not instance:
                                await _aclose(current, f"{sub_app.title} {attr}")
                                setattr(sub_app.state, attr, instance)
                    tracker.set_all(ServiceState.RUNNING)
                    logger.info("All services initialized")
                    try:
                        yield
                    finally:
                        tracker.set_all(ServiceState.STOPPING)
                        await shared_broker.drain()
                        logger.info("Shutting down")
                    # Leaving the AsyncExitStack runs the sub-app shutdowns in reverse order.
            except BaseException as exc:
                original = exc
                raise
            finally:
                tracker.finish()
                # A failed startup (or a normal shutdown) must not leave this run's broker as
                # the process-wide singleton (ruling: no resource leak into a same-process
                # fallback): a later build_serve_app call already replaces it, but a
                # needs-attention app built after a startup failure builds no broker of its
                # own and must never reach this one. (A startup failure happens before the
                # success path's own drain() above ever runs, so there is nothing in flight
                # here to wait for -- only the singleton to release.) Released before the
                # store close, which a cancellation could interrupt.
                if get_shared_broker() is shared_broker:
                    set_shared_broker(None)
                await run_shutdown("serve", _close_shared_store, original=original)

        app = FastAPI(title="Henchmen (Local Dev)", version=__version__, lifespan=lifespan)
        app.mount("/dispatch", dispatch_app)
        app.mount("/mastermind", mastermind_app)
        app.mount("/forge", forge_app)

        @app.get("/health")
        async def health_status() -> dict[str, object]:
            return {"status": "ok", "mode": "local", "services": ["dispatch", "mastermind", "forge"]}

        if console is not None:
            # Mounted last so it only receives paths no service or /health claims.
            app.mount("/", console)

        if desktop is not None:
            from henchmen.console.auth import HostAllowlistGuard

            app.add_middleware(HostAllowlistGuard, allowed_hostnames=desktop.allowed_hostnames)
    except Exception:
        # No lifespan has been built (or entered) yet for this store: build_serve_app never
        # reached `return app`, so close it here instead of leaking the connection when the
        # caller falls back to needs-attention mode in the same process.
        _close_after_build_failure(shared_store, "shared document store")
        raise

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
    known secret) in a request line. The ``httpx`` logger is kept at WARNING.
    """
    from henchmen.utils.redaction import install_secret_redaction

    install_secret_redaction()
    logging.basicConfig(level=getattr(logging, log_level.upper()))
    # httpx logs every request line at INFO, including one-time values in URLs (a GitHub
    # manifest code, callback state); redaction covers those shapes too, but they have no
    # operational value in the serve log.
    logging.getLogger("httpx").setLevel(logging.WARNING)


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


def build_attention_app(console: FastAPI) -> FastAPI:
    """Needs-attention mode: a degraded /health plus the Console, whose status lists the problems."""
    from henchmen import __version__

    app = FastAPI(
        title="Henchmen (needs attention)", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "degraded", "mode": "attention"}

    app.mount("/", console)
    return app


def serve_app(app: FastAPI, *, host: str, port: int, log_level: str, restart: RestartSignal) -> int:
    """Run ``app`` until it stops; return the process exit code.

    Mirrors ``uvicorn.run``'s own handling, which bare ``Server.run()`` does not
    provide: a plain ``KeyboardInterrupt`` (Ctrl+C) is swallowed rather than
    left to print a traceback, and a server whose lifespan never started
    (``server.started`` still ``False``) exits with uvicorn's own
    ``STARTUP_FAILURE`` code so a genuine startup failure is distinguishable
    from a clean stop. A requested restart takes priority over both. A
    ``SystemExit`` raised inside uvicorn -- it exits 1 when it cannot bind and,
    in newer releases, 3 when startup fails -- is returned as the code instead
    of ending the process, so the caller can choose needs-attention mode.
    """
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level=log_level))
    restart.attach(server)
    exit_code: int | None = None
    try:
        with suppress(KeyboardInterrupt):
            server.run()
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else STARTUP_FAILURE
    if restart.requested:
        return RESTART_EXIT_CODE
    if exit_code:
        return exit_code
    if not server.started:
        return STARTUP_FAILURE
    return 0
