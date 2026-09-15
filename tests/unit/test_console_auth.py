"""Tests for the Console's setup token, sessions and localhost guard."""

import hashlib
import hmac
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from henchmen.config import paths
from henchmen.config.settings import Settings
from henchmen.console.auth import (
    SESSION_COOKIE,
    SETUP_TOKEN_FILE_NAME,
    ConsoleAuth,
    ConsoleGuard,
    HostAllowlistGuard,
    SetupTokenStore,
    desktop_allowed_hostnames,
    forward_host_problem,
    is_allowed_host,
    is_local_host,
    is_local_origin,
)

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
        "127.0.0.1/x",
        "127.0.0.1?x",
        "127.0.0.1#x",
        "127.0.0.1\\x",
        "127.0.0.1 x",
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
    first_token = first.setup_token
    second = ConsoleAuth.load(tmp_path / "secrets", setup_token="given")
    cookie = first.issue_session()
    assert second.verify_session(cookie), "sessions must survive a restart"
    assert len(first_token) >= 32
    assert second.setup_token not in {"given", first_token}, "the seed applies only to the very first token"
    if sys.platform != "win32":
        assert oct(os.stat(tmp_path / "secrets" / "console-session.key").st_mode & 0o777) == "0o600"


def test_write_key_file_round_trips_newline_and_carriage_return_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the O_BINARY fix now in ``secret_files.create_secret_file``.

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


SEED = "s" * 43


def test_rotate_uses_a_valid_seed_only_for_the_very_first_token(tmp_path: Path) -> None:
    store = SetupTokenStore(tmp_path / "secrets" / SETUP_TOKEN_FILE_NAME)
    assert store.rotate(seed=SEED) == SEED
    second = store.rotate(seed=SEED)
    assert second != SEED
    assert len(second) >= 43
    assert store.current() == second


@pytest.mark.parametrize("seed", ["short", "has spaces " * 4, "é" * 40])
def test_rotate_ignores_an_unsafe_seed(tmp_path: Path, seed: str) -> None:
    token = SetupTokenStore(tmp_path / SETUP_TOKEN_FILE_NAME).rotate(seed=seed)
    assert token != seed
    assert len(token) >= 43


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits do not apply on Windows")
def test_token_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / SETUP_TOKEN_FILE_NAME
    SetupTokenStore(path).rotate()
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_consume_accepts_the_token_exactly_once(tmp_path: Path) -> None:
    store = SetupTokenStore(tmp_path / SETUP_TOKEN_FILE_NAME)
    token = store.rotate()
    assert store.consume(token) is True
    assert store.consume(token) is False
    replacement = store.current()
    assert replacement is not None and replacement != token


def test_consume_rejects_wrong_or_empty_tokens_without_burning_the_real_one(tmp_path: Path) -> None:
    store = SetupTokenStore(tmp_path / SETUP_TOKEN_FILE_NAME)
    token = store.rotate()
    assert store.consume("") is False
    assert store.consume(token[:-1]) is False
    assert store.consume(token + "x") is False
    assert store.current() == token


def test_a_second_process_cannot_consume_the_same_token(tmp_path: Path) -> None:
    path = tmp_path / SETUP_TOKEN_FILE_NAME
    token = SetupTokenStore(path).rotate()
    assert SetupTokenStore(path).consume(token) is True
    assert SetupTokenStore(path).consume(token) is False


def test_a_failed_claim_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SetupTokenStore(tmp_path / SETUP_TOKEN_FILE_NAME)
    token = store.rotate()

    def lost_race(src: object, dst: object) -> None:
        raise FileNotFoundError("claimed by another process")

    monkeypatch.setattr("henchmen.console.auth.os.replace", lost_race)
    assert store.consume(token) is False


def test_rotation_by_another_process_invalidates_the_old_link(tmp_path: Path) -> None:
    path = tmp_path / SETUP_TOKEN_FILE_NAME
    server_view = SetupTokenStore(path)
    old = server_view.rotate()
    new = SetupTokenStore(path).rotate()
    assert server_view.consume(old) is False
    assert server_view.consume(new) is True


def test_current_is_none_for_a_missing_or_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / SETUP_TOKEN_FILE_NAME
    assert SetupTokenStore(path).current() is None
    path.write_bytes(b"short")
    assert SetupTokenStore(path).current() is None


def test_in_memory_auth_consumes_once() -> None:
    auth = ConsoleAuth(setup_token="t" * 43, signing_key=b"k" * 32)
    assert auth.consume_setup_token("t" * 43) is True
    assert auth.consume_setup_token("t" * 43) is False
    assert auth.setup_token == ""


def test_load_rotates_the_token_on_every_start(tmp_path: Path) -> None:
    first = ConsoleAuth.load(tmp_path / "secrets", setup_token=SEED)
    first_token = first.setup_token
    second = ConsoleAuth.load(tmp_path / "secrets", setup_token=SEED)
    assert first_token == SEED
    assert second.setup_token != first_token
    assert first.consume_setup_token(first_token) is False, "a restart invalidates the previous link"


# ---------------------------------------------------------------------------
# Fix round 1 (B4 + quality): permanent seeded marker, races, retries, logging
# ---------------------------------------------------------------------------


def test_seed_is_not_reused_after_the_token_file_is_lost(tmp_path: Path) -> None:
    """A crash (or any path that leaves the token file missing) must not re-arm the seed."""
    path = tmp_path / SETUP_TOKEN_FILE_NAME
    store = SetupTokenStore(path)
    assert store.rotate(seed=SEED) == SEED
    assert store.consume(SEED) is True
    path.unlink(missing_ok=True)  # simulate the token file being lost after consumption
    assert store.rotate(seed=SEED) != SEED


def test_a_second_process_cannot_reuse_the_seed(tmp_path: Path) -> None:
    path = tmp_path / SETUP_TOKEN_FILE_NAME
    assert SetupTokenStore(path).rotate(seed=SEED) == SEED
    # A second store instance, simulating a second process racing (or following) the
    # first rotate call, must never treat this as "the very first token" again.
    assert SetupTokenStore(path).rotate(seed=SEED) != SEED


def test_seed_write_failure_falls_back_to_a_random_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Marker created, then the seed write itself fails (fix round 2, item 2)."""
    from henchmen.console import auth as auth_module

    path = tmp_path / SETUP_TOKEN_FILE_NAME
    store = SetupTokenStore(path)
    real_write = auth_module.write_secret_file
    calls = {"n": 0}

    def flaky_write(target: Path, data: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        real_write(target, data)

    monkeypatch.setattr(auth_module, "write_secret_file", flaky_write)

    token = store.rotate(seed=SEED)

    assert token != SEED, "the seed write failed, so a random token must have been written instead"
    assert calls["n"] == 2, "exactly one fallback write after the seed write failed"
    assert store.current() == token

    later = store.rotate(seed=SEED)
    assert later != SEED, "the marker already exists, so a later rotate must still ignore the seed"


def test_a_rotation_between_read_and_claim_does_not_destroy_the_new_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / SETUP_TOKEN_FILE_NAME
    store = SetupTokenStore(path)
    old_token = store.rotate()
    real_replace = os.replace
    new_token_holder: dict[str, str] = {}

    def racing_replace(src: object, dst: object) -> None:
        # Simulate another process rotating the token between our current() read and
        # our claim rename: by the time our own replace runs, `path` already holds a
        # brand new token that must not be thrown away. Restore the real os.replace
        # first so the nested rotate()'s own write does not recurse back into this stub.
        monkeypatch.setattr("henchmen.console.auth.os.replace", real_replace)
        new_token_holder["new"] = SetupTokenStore(path).rotate()
        real_replace(src, dst)

    monkeypatch.setattr("henchmen.console.auth.os.replace", racing_replace)
    assert store.consume(old_token) is False

    new_token = new_token_holder["new"]
    assert SetupTokenStore(path).consume(new_token) is True, "the rotated-in token must survive the failed claim"


def test_hard_link_restore_failure_leaves_neither_token_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Same race as above, but the restore itself also fails (fix round 2, item 7)."""
    path = tmp_path / SETUP_TOKEN_FILE_NAME
    store = SetupTokenStore(path)
    old_token = store.rotate()
    real_replace = os.replace
    new_token_holder: dict[str, str] = {}

    def racing_replace(src: object, dst: object) -> None:
        monkeypatch.setattr("henchmen.console.auth.os.replace", real_replace)
        new_token_holder["new"] = SetupTokenStore(path).rotate()
        real_replace(src, dst)

    def failing_link(src: object, dst: object) -> None:
        raise PermissionError("cannot restore")

    monkeypatch.setattr("henchmen.console.auth.os.replace", racing_replace)
    monkeypatch.setattr("henchmen.console.auth.os.link", failing_link)
    caplog.set_level(logging.WARNING)

    assert store.consume(old_token) is False

    new_token = new_token_holder["new"]
    assert SetupTokenStore(path).consume(old_token) is False
    assert SetupTokenStore(path).consume(new_token) is False
    assert caplog.records, "a failed restore must be logged, not silently swallowed"
    for record in caplog.records:
        assert old_token not in record.getMessage()
        assert new_token not in record.getMessage()


def test_claim_unlink_failure_still_returns_the_right_result_and_a_valid_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SetupTokenStore(tmp_path / SETUP_TOKEN_FILE_NAME)
    token = store.rotate()

    def failing_unlink(self: Path, *, missing_ok: bool = False) -> None:
        raise OSError("boom")

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    assert store.consume(token) is True
    # unlink() is patched everywhere for this test, so read the replacement directly.
    monkeypatch.undo()
    replacement = store.current()
    assert replacement is not None and replacement != token


def test_no_claim_files_remain_after_normal_consume_paths(tmp_path: Path) -> None:
    path = tmp_path / SETUP_TOKEN_FILE_NAME
    store = SetupTokenStore(path)
    token = store.rotate()
    assert store.consume(token) is True
    assert store.consume(token) is False  # a second, doomed-to-fail attempt too
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".claim")]
    assert leftovers == []


def test_utime_is_applied_to_the_claim_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix round 2, item 3: refresh the claim's mtime so a concurrent sweep never treats
    an in-flight claim as stale (the rename preserves the token file's own, possibly old,
    mtime)."""
    store = SetupTokenStore(tmp_path / SETUP_TOKEN_FILE_NAME)
    token = store.rotate()
    real_utime = os.utime
    touched: list[str] = []

    def recording_utime(path: object, *args: object, **kwargs: object) -> None:
        touched.append(Path(str(path)).name)
        real_utime(path, *args, **kwargs)

    monkeypatch.setattr("henchmen.console.auth.os.utime", recording_utime)
    assert store.consume(token) is True
    assert len(touched) == 1
    assert touched[0].endswith(".claim")


def test_replacement_write_failure_still_removes_the_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix round 2, item 4: the claim must never linger even if the replacement write fails."""
    store = SetupTokenStore(tmp_path / SETUP_TOKEN_FILE_NAME)
    token = store.rotate()

    def failing_create(path: object, data: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("henchmen.console.auth.create_secret_file", failing_create)

    with pytest.raises(OSError):
        store.consume(token)

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".claim")]
    assert leftovers == []


def test_consume_retries_the_claim_rename_on_windows_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SetupTokenStore(tmp_path / SETUP_TOKEN_FILE_NAME)
    token = store.rotate()
    real_replace = os.replace
    attempts = {"n": 0}

    def flaky_replace(src: object, dst: object) -> None:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise PermissionError("in use")
        real_replace(src, dst)

    sleeps: list[float] = []
    monkeypatch.setattr("henchmen.console.auth.os.replace", flaky_replace)
    monkeypatch.setattr("henchmen.config.secret_files._sleep", sleeps.append)

    assert store.consume(token) is True
    assert attempts["n"] == 3
    assert sleeps == [0.02, 0.02]


def test_rotate_logs_but_never_leaks_the_token_or_an_ignored_seed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store = SetupTokenStore(tmp_path / SETUP_TOKEN_FILE_NAME)
    first_token = store.rotate()  # a real, valid-looking token: also exercises the "no log yet" path
    caplog.clear()
    # A second rotate, seeded with a real (but now stale) token: valid-looking, but
    # already seeded once, so it is ignored and logged -- and must never appear in it.
    second_token = store.rotate(seed=first_token)
    assert second_token != first_token
    assert caplog.records, "ignoring a stale seed must be logged, not silently swallowed"
    for record in caplog.records:
        assert first_token not in record.getMessage()
        assert second_token not in record.getMessage()


def test_consume_never_logs_the_token(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch) -> None:
    caplog.set_level(logging.DEBUG)
    store = SetupTokenStore(tmp_path / SETUP_TOKEN_FILE_NAME)
    token = store.rotate()

    def lost_race(src: object, dst: object) -> None:
        raise FileNotFoundError("claimed by another process")

    monkeypatch.setattr("henchmen.console.auth.os.replace", lost_race)
    caplog.clear()
    assert store.consume(token) is False
    assert caplog.records, "a failed claim must be logged, not silently swallowed"
    for record in caplog.records:
        assert token not in record.getMessage()


def test_concurrent_consume_from_multiple_threads_exactly_one_succeeds(tmp_path: Path) -> None:
    path = tmp_path / SETUP_TOKEN_FILE_NAME
    token = SetupTokenStore(path).rotate()
    results: list[bool] = []
    results_lock = threading.Lock()

    def attempt() -> None:
        result = SetupTokenStore(path).consume(token)
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 19


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


DESKTOP_HOSTS = desktop_allowed_hostnames("henchmen")


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost", "[::1]:8000", "henchmen:8000", "HENCHMEN"])
def test_desktop_hosts_are_allowed(host: str) -> None:
    assert is_allowed_host(host, DESKTOP_HOSTS)


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "evil.example",
        "henchmen.evil.example",
        "127.0.0.1.evil.example",
        "user@henchmen:8000",
        "[",
        "10.0.0.5",
        "henchmen:8000/x",
        "henchmen?x",
        "henchmen#x",
        "henchmen\\x",
        "henchmen 8000",
    ],
)
def test_other_hosts_are_not_allowed(host: str | None) -> None:
    assert not is_allowed_host(host, DESKTOP_HOSTS)


def test_blank_container_hostname_allows_only_loopback() -> None:
    assert desktop_allowed_hostnames("  ") == frozenset({"127.0.0.1", "localhost", "::1"})


def test_host_allowlist_guard_refuses_websockets_from_other_hosts() -> None:
    inner = FastAPI()

    @inner.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.close()

    inner.add_middleware(HostAllowlistGuard, allowed_hostnames=DESKTOP_HOSTS)
    client = TestClient(inner, base_url="http://evil.example:8000")
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/ws"):
        pass
    assert exc.value.code == 1008


def _duplicate_host_scope(
    *, path: str = "/x", first: str = "evil.example", second: str = "localhost"
) -> dict[str, Any]:
    """An ASGI scope with two ``Host`` headers — a dict can't represent this, so build it by hand."""
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "scheme": "http",
        "headers": [(b"host", first.encode("latin-1")), (b"host", second.encode("latin-1"))],
    }


@pytest.mark.asyncio
async def test_host_allowlist_guard_refuses_duplicate_host_headers() -> None:
    """A duplicate Host header must be refused outright, not resolved to whichever the last one names."""
    guard = HostAllowlistGuard(_ok_app, allowed_hostnames=DESKTOP_HOSTS)
    events = await _run_asgi(guard, _duplicate_host_scope())
    assert events[0]["status"] == 403


@pytest.mark.asyncio
async def test_console_guard_refuses_duplicate_host_headers() -> None:
    auth = ConsoleAuth(setup_token="t", signing_key=b"k" * 32)
    guard = ConsoleGuard(_ok_app, auth=auth, public_paths=frozenset())
    events = await _run_asgi(guard, _duplicate_host_scope(path="/console/api/public"))
    assert events[0]["status"] == 403


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"provider": "local"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class TestForwardHostProblem:
    """P3: the Host allowlist must not silently refuse operatives calling back on the default forward base."""

    def test_non_desktop_returns_none(self) -> None:
        assert paths.is_desktop_install() is False
        assert forward_host_problem(_settings()) is None

    def test_desktop_with_the_default_reports_the_problem(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
        problem = forward_host_problem(_settings())
        assert problem is not None
        assert "host.docker.internal" in problem
        assert "HENCHMEN_LOCAL_FORWARD_BASE_URL=http://henchmen:8000" in problem

    def test_desktop_with_the_container_hostname_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
        settings = _settings(local_forward_base_url="http://henchmen:8000")
        assert forward_host_problem(settings) is None

    @pytest.mark.parametrize("loopback", ["127.0.0.1", "localhost", "[::1]"])
    def test_desktop_with_a_loopback_forward_base_reports_a_problem(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, loopback: str
    ) -> None:
        """DockerOrchestrator (providers/local/docker.py) never runs an operative container with
        `--network host` -- only the default bridge network or a named `local_docker_network` --
        so a loopback forward base names the operative's own container, never this machine.
        Task 4 originally treated this as fine; Task 11 (ruling P3) corrects it."""
        monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
        settings = _settings(local_forward_base_url=f"http://{loopback}:8000")
        problem = forward_host_problem(settings)
        assert problem is not None
        assert "HENCHMEN_LOCAL_FORWARD_BASE_URL=http://henchmen:8000" in problem

    def test_desktop_with_a_loopback_forward_base_reports_a_problem_even_on_a_named_docker_network(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Unconditional, not gated on local_docker_network: DockerOrchestrator has no
        `--network host` mode, so a named user-defined network does not make the operative
        container's own loopback reach the host either."""
        monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
        settings = _settings(local_forward_base_url="http://127.0.0.1:8000", local_docker_network="henchmen-net")
        assert forward_host_problem(settings) is not None

    def test_malformed_url_reports_a_problem(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
        settings = _settings(local_forward_base_url="http://[")
        assert forward_host_problem(settings) is not None

    def test_blank_container_hostname_reports_a_problem(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
        settings = _settings(local_container_hostname="   ")
        problem = forward_host_problem(settings)
        assert problem is not None
        assert "HENCHMEN_LOCAL_CONTAINER_HOSTNAME" in problem

    def test_container_hostname_with_a_port_reports_a_problem(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
        settings = _settings(local_container_hostname="henchmen:8000")
        problem = forward_host_problem(settings)
        assert problem is not None
        assert "HENCHMEN_LOCAL_CONTAINER_HOSTNAME" in problem

    def test_bad_port_in_the_forward_url_reports_a_problem(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
        settings = _settings(local_forward_base_url="http://henchmen:abc")
        assert forward_host_problem(settings) is not None
