"""Tests for the Console's setup token, sessions and localhost guard."""

import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from henchmen.console.auth import SESSION_COOKIE, ConsoleAuth, ConsoleGuard, is_local_host, is_local_origin

LOCAL = "http://127.0.0.1:8000"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8000", "localhost:49152", "[::1]:8000", "LOCALHOST"])
def test_local_hosts_are_accepted(host: str) -> None:
    assert is_local_host(host)


@pytest.mark.parametrize("host", [None, "", "evil.example", "127.0.0.1.evil.example", "henchmen:8000", "10.0.0.5"])
def test_other_hosts_are_rejected(host: str | None) -> None:
    assert not is_local_host(host)


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8000", "http://localhost:3000", "http://[::1]:8000"])
def test_local_origins_are_accepted(origin: str) -> None:
    assert is_local_origin(origin)


@pytest.mark.parametrize("origin", [None, "", "null", "https://evil.example", "http://127.0.0.1.evil.example"])
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


def test_load_creates_and_reuses_the_signing_key(tmp_path: Path) -> None:
    first = ConsoleAuth.load(tmp_path / "secrets", setup_token=None)
    second = ConsoleAuth.load(tmp_path / "secrets", setup_token="given")
    cookie = first.issue_session()
    assert second.verify_session(cookie), "sessions must survive a restart"
    assert second.setup_token == "given"
    assert len(first.setup_token) >= 32
    if sys.platform != "win32":
        assert oct(os.stat(tmp_path / "secrets" / "console-session.key").st_mode & 0o777) == "0o600"


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

    inner.add_middleware(ConsoleGuard, auth=auth, public_paths=frozenset({"/console/api/status"}))
    return TestClient(inner, base_url=LOCAL)


def test_guard_rejects_non_local_hosts() -> None:
    client = _guarded_app(ConsoleAuth(setup_token="t", signing_key=b"k" * 32))
    response = client.get("/console/api/status", headers={"host": "evil.example"})
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
