"""Tests for the Console's setup token, sessions and localhost guard."""

import hashlib
import hmac
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from henchmen.console.auth import SESSION_COOKIE, ConsoleAuth, ConsoleGuard, is_local_host, is_local_origin

LOCAL = "http://127.0.0.1:8000"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8000", "localhost:49152", "[::1]:8000", "LOCALHOST"])
def test_local_hosts_are_accepted(host: str) -> None:
    assert is_local_host(host)


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "evil.example",
        "127.0.0.1.evil.example",
        "henchmen:8000",
        "10.0.0.5",
        "[",
        "evil@127.0.0.1:8000",
    ],
)
def test_other_hosts_are_rejected(host: str | None) -> None:
    assert not is_local_host(host)


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8000", "http://localhost:3000", "http://[::1]:8000"])
def test_local_origins_are_accepted(origin: str) -> None:
    assert is_local_origin(origin)


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "",
        "null",
        "https://evil.example",
        "http://127.0.0.1.evil.example",
        "http://[",
        "http://evil@127.0.0.1:8000",
    ],
)
def test_other_origins_are_rejected(origin: str | None) -> None:
    assert not is_local_origin(origin)


def test_setup_token_comparison() -> None:
    auth = ConsoleAuth(setup_token="s3cret", signing_key=b"k" * 32)
    assert auth.check_setup_token("s3cret")
    assert not auth.check_setup_token("s3cre")
    assert not auth.check_setup_token("")


def test_sessions_verify_and_expire() -> None:
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32, max_age_seconds=60)
    cookie = auth.issue_session(now=1_000.0)
    assert auth.verify_session(cookie, now=1_030.0)
    assert not auth.verify_session(cookie, now=1_061.0)


def test_tampered_or_foreign_sessions_fail() -> None:
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    other = ConsoleAuth(setup_token="t", signing_key=b"x" * 32)
    cookie = auth.issue_session(now=1_000.0)
    issued, signature = cookie.split(".")
    assert not auth.verify_session(f"{int(issued) + 1}.{signature}", now=1_000.0)
    assert not auth.verify_session(other.issue_session(now=1_000.0), now=1_000.0)
    assert not auth.verify_session(None)
    assert not auth.verify_session("garbage")


def test_non_ascii_session_signature_is_rejected_not_raised() -> None:
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    assert not auth.verify_session("1000.\xe9", now=1_000.0)
    assert not auth.verify_session("not-a-number.abc", now=1_000.0)


def test_non_ascii_digit_timestamp_is_rejected_not_raised() -> None:
    # "\xb2" is the superscript-two digit: str.isdigit() is True for it, but
    # int() rejects it, so the check must also require plain ASCII digits.
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    assert not auth.verify_session("\xb2.abc", now=1_000.0)


def test_oversized_timestamp_is_rejected_not_raised() -> None:
    huge_timestamp = "1" * 5000
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    assert not auth.verify_session(f"{huge_timestamp}.abc", now=1_000.0)


def test_max_age_seconds_exposes_the_accepted_session_lifetime() -> None:
    assert ConsoleAuth(setup_token="t", signing_key=b"k" * 32, max_age_seconds=60).max_age_seconds == 60
    assert ConsoleAuth(setup_token="t", signing_key=b"k" * 32).max_age_seconds == 30 * 24 * 3600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits do not apply on Windows")
def test_load_creates_the_secrets_directory_owner_only(tmp_path: Path) -> None:
    ConsoleAuth.load(tmp_path / "secrets", setup_token=None)
    assert oct(os.stat(tmp_path / "secrets").st_mode & 0o777) == "0o700"


def test_load_creates_and_reuses_the_signing_key(tmp_path: Path) -> None:
    first = ConsoleAuth.load(tmp_path / "secrets", setup_token=None)
    second = ConsoleAuth.load(tmp_path / "secrets", setup_token="given")
    cookie = first.issue_session()
    assert second.verify_session(cookie), "sessions must survive a restart"
    assert second.setup_token == "given"
    assert len(first.setup_token) >= 32
    if sys.platform != "win32":
        assert oct(os.stat(tmp_path / "secrets" / "console-session.key").st_mode & 0o777) == "0o600"


def test_write_key_file_round_trips_newline_and_carriage_return_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the O_BINARY fix in ``_write_key_file``.

    Opening the key file without ``os.O_BINARY`` puts the descriptor in text
    mode on Windows, which silently rewrites a lone ``\\n`` (0x0A) byte to
    ``\\r\\n`` on write. That grows the on-disk key past its in-memory value,
    so a second ``ConsoleAuth.load()`` reads a different key than the one that
    signed a cookie moments earlier, and every session breaks across a
    restart. The forged key here always contains both ``\\n`` and ``\\r`` so
    the corruption reproduces deterministically instead of the ~1-in-8 chance
    of it landing in a real random key.
    """
    key = bytes(range(30)) + b"\n\r"
    assert len(key) == 32
    monkeypatch.setattr("henchmen.console.auth.secrets.token_bytes", lambda n: key)

    secrets_dir = tmp_path / "secrets"
    first = ConsoleAuth.load(secrets_dir, setup_token=None)
    key_path = secrets_dir / "console-session.key"

    assert key_path.read_bytes() == key, "the byte-for-byte key must round-trip through the file"

    second = ConsoleAuth.load(secrets_dir, setup_token="given")
    cookie = first.issue_session()
    assert second.verify_session(cookie), "a session signed before a restart must still verify after one"


@pytest.mark.parametrize("bad_key", [b"", b"short"])
def test_load_regenerates_an_empty_or_short_signing_key(tmp_path: Path, bad_key: bytes) -> None:
    secrets_path = tmp_path / "secrets"
    secrets_path.mkdir(parents=True)
    key_path = secrets_path / "console-session.key"
    key_path.write_bytes(bad_key)

    auth = ConsoleAuth.load(secrets_path, setup_token="t")

    regenerated = key_path.read_bytes()
    assert len(regenerated) >= 32
    assert regenerated != bad_key

    forged_signature = hmac.new(bad_key, b"console-session:1000", hashlib.sha256).hexdigest()
    assert not auth.verify_session(f"1000.{forged_signature}", now=1_000.0)

    if sys.platform != "win32":
        assert oct(os.stat(key_path).st_mode & 0o777) == "0o600"


def _guarded_app(auth: ConsoleAuth) -> TestClient:
    inner = FastAPI()

    @inner.get("/console/api/status")
    async def status() -> dict[str, str]:
        return {"ok": "status"}

    @inner.get("/console/api/private")
    async def private() -> dict[str, str]:
        return {"ok": "private"}

    @inner.post("/console/api/private")
    async def private_post() -> dict[str, str]:
        return {"ok": "posted"}

    @inner.websocket("/console/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    inner.add_middleware(ConsoleGuard, auth=auth, public_paths=frozenset({"/console/api/status"}))
    return TestClient(inner, base_url=LOCAL)


def test_guard_rejects_non_local_hosts() -> None:
    client = _guarded_app(ConsoleAuth(setup_token="t", signing_key=b"k" * 32))
    response = client.get("/console/api/status", headers={"host": "evil.example"})
    assert response.status_code == 403


def test_guard_rejects_host_with_userinfo() -> None:
    client = _guarded_app(ConsoleAuth(setup_token="t", signing_key=b"k" * 32))
    response = client.get("/console/api/status", headers={"host": "evil@127.0.0.1:8000"})
    assert response.status_code == 403


def test_guard_rejects_malformed_host_header() -> None:
    client = _guarded_app(ConsoleAuth(setup_token="t", signing_key=b"k" * 32))
    response = client.get("/console/api/status", headers={"host": "["})
    assert response.status_code == 403


def test_guard_allows_public_paths_without_a_session() -> None:
    client = _guarded_app(ConsoleAuth(setup_token="t", signing_key=b"k" * 32))
    assert client.get("/console/api/status").status_code == 200


def test_guard_requires_a_session_for_other_api_paths() -> None:
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    client = _guarded_app(auth)
    assert client.get("/console/api/private").status_code == 401
    client.cookies.set(SESSION_COOKIE, auth.issue_session())
    assert client.get("/console/api/private").status_code == 200


def test_guard_requires_a_local_origin_for_state_changes() -> None:
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    client = _guarded_app(auth)
    client.cookies.set(SESSION_COOKIE, auth.issue_session())
    assert client.post("/console/api/private").status_code == 403
    assert client.post("/console/api/private", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/console/api/private", headers={"origin": LOCAL}).status_code == 200


def test_guard_requires_a_matching_origin_port_for_state_changes() -> None:
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    client = _guarded_app(auth)
    client.cookies.set(SESSION_COOKIE, auth.issue_session())

    mismatched = client.post("/console/api/private", headers={"origin": "http://127.0.0.1:3000"})
    assert mismatched.status_code == 403

    matching = client.post("/console/api/private", headers={"origin": LOCAL})
    assert matching.status_code == 200

    other_loopback_name_same_port = client.post("/console/api/private", headers={"origin": "http://localhost:8000"})
    assert other_loopback_name_same_port.status_code == 200


def test_guard_rejects_malformed_origin_on_state_change() -> None:
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    client = _guarded_app(auth)
    client.cookies.set(SESSION_COOKIE, auth.issue_session())
    response = client.post("/console/api/private", headers={"origin": "http://["})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_guard_rejects_non_ascii_cookie_without_raising() -> None:
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    guard = ConsoleGuard(_ok_app, auth=auth, public_paths=frozenset())
    scope = _http_scope(
        path="/console/api/private",
        headers={"host": "127.0.0.1:8000", "cookie": "henchmen_console=1000.\xe9"},
    )
    events = await _run_asgi(guard, scope)
    assert events[0]["status"] == 401


@pytest.mark.asyncio
async def test_guard_rejects_superscript_digit_timestamp_without_raising() -> None:
    # Cookie byte 0xB2 decodes (latin-1) to the superscript-two digit "\xb2":
    # str.isdigit() is True for it but int() rejects it with ValueError.
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    guard = ConsoleGuard(_ok_app, auth=auth, public_paths=frozenset())
    scope = _http_scope(
        path="/console/api/private",
        headers={"host": "127.0.0.1:8000", "cookie": "henchmen_console=\xb2.abc"},
    )
    events = await _run_asgi(guard, scope)
    assert events[0]["status"] == 401


def test_guard_rejects_websocket_without_a_session() -> None:
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    client = _guarded_app(auth)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/console/ws", headers={"host": "127.0.0.1:8000", "origin": LOCAL}),
    ):
        pass


def test_guard_rejects_websocket_with_foreign_origin() -> None:
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    client = _guarded_app(auth)
    client.cookies.set(SESSION_COOKIE, auth.issue_session())
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/console/ws", headers={"host": "127.0.0.1:8000", "origin": "https://evil.example"}),
    ):
        pass


def test_guard_accepts_websocket_with_valid_session_and_origin() -> None:
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    client = _guarded_app(auth)
    client.cookies.set(SESSION_COOKIE, auth.issue_session())
    with client.websocket_connect("/console/ws", headers={"host": "127.0.0.1:8000", "origin": LOCAL}) as websocket:
        assert websocket.receive_text() == "hello"


async def _ok_app(scope: Any, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _http_scope(*, method: str = "GET", path: str, headers: dict[str, str], scheme: str = "http") -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "scheme": scheme,
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
    }


async def _run_asgi(app: Any, scope: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        events.append(message)

    await app(scope, receive, send)
    return events
