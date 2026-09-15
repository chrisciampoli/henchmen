"""A shutdown path never masks the exception a lifespan is unwinding with (D11)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from henchmen.utils.lifespan import run_shutdown


@pytest.mark.asyncio
async def test_an_exception_during_shutdown_is_logged_and_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    await run_shutdown("svc", AsyncMock(side_effect=RuntimeError("close failed")), original=None)
    assert "[svc] Shutdown raised" in caplog.text


@pytest.mark.asyncio
async def test_a_cancellation_during_shutdown_does_not_replace_the_original() -> None:
    @asynccontextmanager
    async def lifespan() -> Any:
        original: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            original = exc
            raise
        finally:
            await run_shutdown("svc", AsyncMock(side_effect=asyncio.CancelledError()), original=original)

    with pytest.raises(ValueError, match="sibling failed to start"):
        async with lifespan():
            raise ValueError("sibling failed to start")


@pytest.mark.asyncio
async def test_a_cancellation_during_a_clean_shutdown_still_propagates() -> None:
    with pytest.raises(asyncio.CancelledError):
        await run_shutdown("svc", AsyncMock(side_effect=asyncio.CancelledError()), original=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["henchmen.mastermind.server", "henchmen.forge.server", "henchmen.dispatch.server"])
async def test_service_lifespans_keep_the_original_when_shutdown_is_cancelled(
    module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    server = importlib.import_module(module)
    import henchmen.observability.tracing as tracing

    monkeypatch.setattr(tracing, "init_tracing", MagicMock())
    monkeypatch.setattr(tracing, "instrument_fastapi", MagicMock())
    monkeypatch.setattr(tracing, "shutdown_tracing", MagicMock(side_effect=asyncio.CancelledError()))
    if module == "henchmen.mastermind.server":
        agent = MagicMock()
        agent.tracker = MagicMock()
        agent._active_tasks = {}
        monkeypatch.setattr(server, "get_agent", lambda: agent)
        monkeypatch.setattr(server, "create_metrics_router", lambda tracker: __import__("fastapi").APIRouter())
    if module == "henchmen.dispatch.server":
        import henchmen.dispatch.slack_bot as slack_bot

        monkeypatch.setattr(slack_bot, "start_socket_mode", lambda settings, broker: None)
    app = server.app
    app.state.message_broker = MagicMock()
    try:
        with pytest.raises(ValueError, match="later sub-app failed"):
            async with server.lifespan(app):
                raise ValueError("later sub-app failed")
    finally:
        for attr in ("message_broker", "ci_provider", "document_store", "slack_socket_handler"):
            if hasattr(app.state, attr):
                delattr(app.state, attr)


def test_close_after_build_failure_without_a_loop_closes_synchronously() -> None:
    from henchmen.cli.serve import _close_after_build_failure

    resource = MagicMock()
    resource.aclose = AsyncMock()
    _close_after_build_failure(resource, "store")
    resource.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_after_build_failure_inside_a_running_loop_schedules_the_close() -> None:
    """D11: asyncio.run would refuse inside a loop; the close is scheduled on it instead, not dropped."""
    from henchmen.cli import serve

    resource = MagicMock()
    resource.aclose = AsyncMock()
    serve._close_after_build_failure(resource, "store")
    assert serve._pending_closes, "the scheduled close is held until it finishes"
    await asyncio.gather(*serve._pending_closes)
    resource.aclose.assert_awaited_once()
    await asyncio.sleep(0)
    assert not serve._pending_closes
