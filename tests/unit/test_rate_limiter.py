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


# ---------------------------------------------------------------------------
# Regressions: mounted apps, proxied clients, bucket eviction, /pubsub scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_applies_when_app_is_mounted():
    """``henchmen serve`` mounts the app at /dispatch; the limiter must still fire.

    ``request.url.path`` carries the mount prefix, so matching on it made the
    limiter inert under ``henchmen serve``. The route-relative path is used
    instead.
    """
    inner = _make_app()
    root = FastAPI()
    root.mount("/dispatch", inner)

    transport = ASGITransport(app=_SpoofClientIPMiddleware(root, "7.7.7.7"))  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(60):
            resp = await client.post("/dispatch/api/v1/tasks")
            assert resp.status_code == 200
        resp = await client.post("/dispatch/api/v1/tasks")
        assert resp.status_code == 429


@pytest.mark.asyncio
async def test_rate_limiter_covers_pubsub_paths():
    """Pub/Sub push endpoints are inside the limiter's scope."""
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.post("/pubsub/example")
    async def pubsub_example() -> dict[str, str]:
        return {"ok": "yes"}

    transport = ASGITransport(app=_SpoofClientIPMiddleware(app, "8.8.8.8"))  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(60):
            assert (await client.post("/pubsub/example")).status_code == 200
        assert (await client.post("/pubsub/example")).status_code == 429


@pytest.mark.asyncio
async def test_forwarded_for_separates_clients_behind_a_proxy():
    """Behind Cloud Run every request has the same peer address."""
    app = _make_app()
    transport = ASGITransport(app=_SpoofClientIPMiddleware(app, "169.254.1.1"))  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(60):
            resp = await client.post("/webhooks/test", headers={"X-Forwarded-For": "203.0.113.1, 169.254.1.1"})
            assert resp.status_code == 200
        resp = await client.post("/webhooks/test", headers={"X-Forwarded-For": "203.0.113.1, 169.254.1.1"})
        assert resp.status_code == 429

        # A different real client still has its own budget.
        resp = await client.post("/webhooks/test", headers={"X-Forwarded-For": "203.0.113.2, 169.254.1.1"})
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_idle_buckets_are_evicted():
    """The per-IP dict must not grow for the life of the process."""
    app = FastAPI()
    middleware = RateLimitMiddleware(app, window_seconds=0)

    async def call_next(_request: Request) -> Response:
        return JSONResponse({"ok": True}, status_code=200)

    for i in range(25):
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/webhooks/foo",
            "root_path": "",
            "headers": [],
            "query_string": b"",
            "client": (f"10.1.0.{i}", 1234),
        }
        resp = await middleware.dispatch(Request(scope), call_next)  # type: ignore[arg-type]
        assert resp.status_code == 200

    # With a zero-length window every previous bucket is stale by the next call.
    assert len(middleware._requests) <= 1


@pytest.mark.asyncio
async def test_limits_are_configurable():
    app = FastAPI()
    middleware = RateLimitMiddleware(app, limit=2, window_seconds=60)

    async def call_next(_request: Request) -> Response:
        return JSONResponse({"ok": True}, status_code=200)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/foo",
        "root_path": "",
        "headers": [],
        "query_string": b"",
        "client": ("6.6.6.6", 1234),
    }
    assert (await middleware.dispatch(Request(scope), call_next)).status_code == 200  # type: ignore[arg-type]
    assert (await middleware.dispatch(Request(scope), call_next)).status_code == 200  # type: ignore[arg-type]
    assert (await middleware.dispatch(Request(scope), call_next)).status_code == 429  # type: ignore[arg-type]
