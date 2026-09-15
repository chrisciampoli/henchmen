"""Tests for the single-use, expiring state values behind the public GitHub callbacks."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from henchmen.console import callback_state
from henchmen.console.callback_state import DEFAULT_TTL_SECONDS, MAX_PENDING_STATES, CallbackStateStore


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_state_is_consumed_exactly_once(tmp_path: Path) -> None:
    store = CallbackStateStore(tmp_path / "states.json")
    state = store.issue("github-manifest", {"account_type": "personal"})
    assert len(state) >= 40
    assert store.consume("github-manifest", state) == {"account_type": "personal"}
    assert store.consume("github-manifest", state) is None


def test_states_are_random(tmp_path: Path) -> None:
    store = CallbackStateStore(tmp_path / "states.json")
    assert len({store.issue("github-manifest", {}) for _ in range(10)}) == 10


def test_wrong_purpose_is_refused_and_burns_the_state(tmp_path: Path) -> None:
    store = CallbackStateStore(tmp_path / "states.json")
    state = store.issue("github-manifest", {})
    assert store.consume("github-install", state) is None
    assert store.consume("github-manifest", state) is None


def test_expired_state_is_refused(tmp_path: Path) -> None:
    clock = _Clock()
    store = CallbackStateStore(tmp_path / "states.json", ttl_seconds=60, clock=clock)
    state = store.issue("github-install", {"slug": "henchmen-x"})
    clock.now += 61
    assert store.consume("github-install", state) is None


def test_default_lifetime_is_one_hour(tmp_path: Path) -> None:
    clock = _Clock()
    store = CallbackStateStore(tmp_path / "states.json", clock=clock)
    fresh = store.issue("github-manifest", {})
    stale = store.issue("github-manifest", {})
    clock.now += DEFAULT_TTL_SECONDS - 1
    assert store.consume("github-manifest", fresh) == {}
    clock.now += 1
    assert store.consume("github-manifest", stale) is None
    assert DEFAULT_TTL_SECONDS == 3600


def test_blank_unknown_and_oversized_states_are_refused(tmp_path: Path) -> None:
    store = CallbackStateStore(tmp_path / "states.json")
    store.issue("github-manifest", {})
    assert store.consume("github-manifest", "") is None
    assert store.consume("github-manifest", "not-issued") is None
    assert store.consume("github-manifest", "x" * 1000) is None


def test_file_holds_digests_not_states_and_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "states.json"
    state = CallbackStateStore(path).issue("github-manifest", {"organization": "acme"})
    text = path.read_text(encoding="utf-8")
    assert state not in text
    assert len(json.loads(text)) == 1
    if sys.platform != "win32":
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_issue_creates_an_owner_only_secrets_dir_on_a_fresh_data_directory(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "fresh-data" / "secrets"
    store = CallbackStateStore(secrets_dir / callback_state.STATE_FILE_NAME)
    state = store.issue("github-manifest", {"a": "b"})
    assert secrets_dir.is_dir()
    if sys.platform != "win32":
        assert oct(os.stat(secrets_dir).st_mode & 0o777) == "0o700"
    assert store.consume("github-manifest", state) == {"a": "b"}


def test_states_survive_a_restart(tmp_path: Path) -> None:
    state = CallbackStateStore(tmp_path / "states.json").issue("github-manifest", {"a": "b"})
    assert CallbackStateStore(tmp_path / "states.json").consume("github-manifest", state) == {"a": "b"}


def test_corrupt_file_is_treated_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "states.json"
    path.write_text("{not json", encoding="utf-8")
    store = CallbackStateStore(path)
    assert store.consume("github-manifest", "anything") is None
    state = store.issue("github-manifest", {})
    assert store.consume("github-manifest", state) == {}


def test_malformed_entries_are_never_honoured(tmp_path: Path) -> None:
    path = tmp_path / "states.json"
    store = CallbackStateStore(path)
    state = store.issue("github-manifest", {})
    entries = json.loads(path.read_text(encoding="utf-8"))
    digest = next(iter(entries))
    for bad_expiry in ("never", None, True, float("inf")):
        entries[digest]["expires_at"] = bad_expiry
        path.write_text(json.dumps(entries), encoding="utf-8")
        assert store.consume("github-manifest", state) is None


def test_a_symlinked_state_file_is_never_followed(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    issuer = CallbackStateStore(target)
    state = issuer.issue("github-manifest", {})
    link = tmp_path / "states.json"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links cannot be created here")
    store = CallbackStateStore(link)
    assert store.consume("github-manifest", state) is None
    with pytest.raises(OSError):
        store.issue("github-manifest", {})
    assert issuer.consume("github-manifest", state) == {}


def test_a_consumed_state_whose_removal_cannot_be_saved_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CallbackStateStore(tmp_path / "states.json")
    state = store.issue("github-manifest", {})

    def fail(path: Path, data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(callback_state, "write_secret_file", fail)
    assert store.consume("github-manifest", state) is None


def test_concurrent_consumers_of_one_state_succeed_exactly_once(tmp_path: Path) -> None:
    path = tmp_path / "states.json"
    state = CallbackStateStore(path).issue("github-manifest", {"a": "b"})
    # Separate instances over the same file, as two app builds in one process would have.
    stores = [CallbackStateStore(path) for _ in range(8)]
    barrier = threading.Barrier(len(stores))
    results: list[dict[str, str] | None] = []
    results_lock = threading.Lock()

    def consume(store: CallbackStateStore) -> None:
        barrier.wait()
        outcome = store.consume("github-manifest", state)
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=consume, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results, key=lambda item: item is None) == [{"a": "b"}] + [None] * (len(stores) - 1)


def test_pending_states_are_capped_oldest_first(tmp_path: Path) -> None:
    clock = _Clock()
    store = CallbackStateStore(tmp_path / "states.json", clock=clock)
    states = []
    for _ in range(MAX_PENDING_STATES + 1):
        states.append(store.issue("github-manifest", {}))
        clock.now += 1
    assert store.consume("github-manifest", states[0]) is None
    assert store.consume("github-manifest", states[-1]) == {}


def test_repr_carries_no_state(tmp_path: Path) -> None:
    store = CallbackStateStore(tmp_path / "states.json")
    assert repr(store) == f"CallbackStateStore(path={tmp_path / 'states.json'})"


def test_a_non_positive_lifetime_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        CallbackStateStore(tmp_path / "states.json", ttl_seconds=0)
