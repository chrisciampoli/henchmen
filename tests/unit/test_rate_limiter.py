"""Unit tests for the ``RateLimitMiddleware`` in ``henchmen.dispatch.server``.

Exercises the sliding-window per-IP limiter that guards ``/webhooks/*`` and
``/api/v1/*`` routes. Previously untested; see expert-panel finding R6.
"""

from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from henchmen.dispatch.server import RateLimitMiddleware


def _make_app() -> FastAPI:
    """Build a minimal FastAPI app with RateLimitMiddleware for isolation.

    We deliberately do NOT import ``henchmen.dispatch.server.app`` because
    that full app registers a lifespan handler that reaches into tracing
    and provider registry — too much for a focused middleware test.
    """
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/test")
    async def webhook() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/api/v1/tasks")
    async def api_task() -> dict[str, str]:
        return {"ok": "yes"}

    return app


class _SpoofClientIPMiddleware:
    """ASGI middleware that rewrites ``scope['client']`` to a fixed IP.

    ``httpx.AsyncClient`` with ``ASGITransport`` does not set the client
    address, which would make every request look like it came from the same
    ``None``/``unknown`` origin. We inject a middleware of our own BEFORE
    the app's middleware stack so we can pretend different requests come
    from different IPs.
    """

    def __init__(self, app: Callable[..., Awaitable[None]], client_ip: str) -> None:
        self.app = app
        self.client_ip = client_ip

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (self.client_ip, 12345)
        await self.app(scope, receive, send)


@pytest.mark.asyncio
async def test_rate_limiter_allows_under_limit():
    """Requests under the per-window limit must all succeed."""
    app = _make_app()
    transport = ASGITransport(app=_SpoofClientIPMiddleware(app, "1.2.3.4"))  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Send 10 requests — well under the 60/min limit.
        for _ in range(10):
            resp = await client.post("/webhooks/test")
            assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rate_limiter_rejects_over_limit():
    """The 61st request in a window from the same IP must return 429."""
    app = _make_app()
    transport = ASGITransport(app=_SpoofClientIPMiddleware(app, "1.2.3.5"))  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Exhaust the allowance (60 requests).
        for _ in range(60):
            resp = await client.post("/webhooks/test")
            assert resp.status_code == 200
        # Next one must be rejected.
        resp = await client.post("/webhooks/test")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers


@pytest.mark.asyncio
async def test_rate_limiter_scoped_to_webhooks_and_api():
    """``GET /health`` is outside the limiter's scope and must not count."""
    app = _make_app()
    transport = ASGITransport(app=_SpoofClientIPMiddleware(app, "1.2.3.6"))  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Blast /health well past the 60 budget — should never 429.
        for _ in range(120):
            resp = await client.get("/health")
            assert resp.status_code == 200
        # Immediately after, webhooks should still have a full budget.
        resp = await client.post("/webhooks/test")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_rate_limiter_per_client_ip():
    """Two distinct client IPs must not share a bucket."""
    app = _make_app()

    # Client A exhausts its own quota.
    transport_a = ASGITransport(app=_SpoofClientIPMiddleware(app, "10.0.0.1"))  # type: ignore[arg-type]
    async with AsyncClient(transport=transport_a, base_url="http://test") as client_a:
        for _ in range(60):
            resp = await client_a.post("/webhooks/test")
            assert resp.status_code == 200
        resp = await client_a.post("/webhooks/test")
        assert resp.status_code == 429

    # Client B (different IP) must still be allowed.
    transport_b = ASGITransport(app=_SpoofClientIPMiddleware(app, "10.0.0.2"))  # type: ignore[arg-type]
    async with AsyncClient(transport=transport_b, base_url="http://test") as client_b:
        resp = await client_b.post("/webhooks/test")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Direct-dispatch sanity check: exercise RateLimitMiddleware.dispatch without
# an HTTP round-trip. This guards against regressions in the bookkeeping code
# that only show up under tight per-IP load.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_bookkeeping_direct():
    """Construct the middleware and call ``dispatch`` directly."""
    app = FastAPI()
    middleware = RateLimitMiddleware(app)

    async def call_next(_request: Request) -> Response:
        return JSONResponse({"ok": True}, status_code=200)

    # Build a synthetic Request scope targeting /webhooks/foo from 5.5.5.5.
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/foo",
        "headers": [],
        "query_string": b"",
        "client": ("5.5.5.5", 1234),
    }
    request = Request(scope)  # type: ignore[arg-type]

    # First call should pass.
    resp = await middleware.dispatch(request, call_next)
    assert resp.status_code == 200

    # Force the bucket to its limit and verify the next call is rejected.
    middleware._requests["5.5.5.5"] = [0.0] * 60  # 60 "recent" requests
    import time

    middleware._requests["5.5.5.5"] = [time.monotonic()] * 60
    resp = await middleware.dispatch(request, call_next)
    assert resp.status_code == 429
