"""Tests for owner-only, atomic secret files in <data dir>/secrets."""

from __future__ import annotations

import base64
import logging
import os
import sys
import time
from pathlib import Path

import pytest

from henchmen.config import secret_files
from henchmen.config.secret_files import (
    MIN_SECRET_BYTES,
    create_secret_file,
    ensure_secrets_dir,
    read_or_create_secret,
    replace_with_retry,
    write_secret_file,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits do not apply on Windows")


def _mode(path: Path) -> str:
    return oct(os.stat(path).st_mode & 0o777)


def test_create_round_trips_newline_and_carriage_return_bytes(tmp_path: Path) -> None:
    """O_BINARY: a text-mode descriptor on Windows would rewrite 0x0A to CRLF and corrupt the secret."""
    data = bytes(range(30)) + b"\n\r"
    path = tmp_path / "k"
    create_secret_file(path, data)
    assert path.read_bytes() == data


def test_create_refuses_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "k"
    create_secret_file(path, b"a" * 32)
    with pytest.raises(FileExistsError):
        create_secret_file(path, b"b" * 32)
    assert path.read_bytes() == b"a" * 32


@posix_only
def test_files_and_directory_are_owner_only(tmp_path: Path) -> None:
    directory = tmp_path / "secrets"
    ensure_secrets_dir(directory)
    create_secret_file(directory / "k", b"a" * 32)
    write_secret_file(directory / "r", b"b" * 32)
    assert _mode(directory) == "0o700"
    assert _mode(directory / "k") == "0o600"
    assert _mode(directory / "r") == "0o600"


def test_replace_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "k"
    path.write_bytes(b"old")
    write_secret_file(path, b"n" * 32)
    assert path.read_bytes() == b"n" * 32
    assert [p.name for p in tmp_path.iterdir()] == ["k"]


def test_load_creates_then_reuses(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "k"
    first = read_or_create_secret(path)
    assert len(first) >= MIN_SECRET_BYTES
    assert read_or_create_secret(path) == first


@pytest.mark.parametrize("bad", [b"", b"short"])
def test_load_regenerates_a_short_secret(tmp_path: Path, bad: bytes) -> None:
    path = tmp_path / "k"
    path.write_bytes(bad)
    fresh = read_or_create_secret(path)
    assert len(fresh) >= MIN_SECRET_BYTES
    assert fresh != bad
    assert path.read_bytes() == fresh


def test_load_uses_the_file_another_process_created_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """By the time we try to publish our own temp file, another process's full file is already there."""
    path = tmp_path / "k"
    winner = b"w" * 32

    def racing_link(_src: str, dst: str) -> None:
        Path(dst).write_bytes(winner)
        raise FileExistsError(dst)

    monkeypatch.setattr(os, "link", racing_link)
    assert read_or_create_secret(path) == winner


@posix_only
def test_load_tightens_a_loose_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "k"
    path.write_bytes(b"x" * 32)
    os.chmod(path, 0o644)
    read_or_create_secret(path)
    assert _mode(path) == "0o600"


# --- Fix round 1 -------------------------------------------------------------


def test_create_secret_file_leaves_no_partial_file_when_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "k"

    def failing_write(_fd: int, _data: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", failing_write)
    with pytest.raises(OSError):
        create_secret_file(path, b"a" * 32)
    assert list(tmp_path.iterdir()) == []


def test_write_secret_file_leaves_no_partial_file_when_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "k"
    path.write_bytes(b"old")

    def failing_write(_fd: int, _data: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", failing_write)
    with pytest.raises(OSError):
        write_secret_file(path, b"n" * 32)
    assert path.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["k"]


def test_write_secret_file_leaves_no_temp_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "k"
    path.write_bytes(b"old")

    def failing_replace(_src: object, _dst: object) -> None:
        raise OSError("boom")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        write_secret_file(path, b"n" * 32)
    assert path.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["k"]


def test_read_or_create_secret_leaves_no_partial_file_when_the_first_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets" / "k"

    def failing_write(_fd: int, _data: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", failing_write)
    with pytest.raises(OSError):
        read_or_create_secret(path)
    assert list(path.parent.iterdir()) == []


def test_read_or_create_secret_waits_out_a_slow_concurrent_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A racing creator's file can appear empty for a moment before its bytes land; we must not clobber it.

    Deterministic version: instead of racing a real background thread against a
    wall-clock timeout, the retry loop's own sleep hook is what makes the
    winner's bytes "land" on its first call.
    """
    path = tmp_path / "k"
    path.write_bytes(b"")  # a concurrent creator's directory entry exists but its bytes have not landed yet
    winner = b"w" * 32
    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        path.write_bytes(winner)

    def always_exists(_src: str, _dst: str) -> None:
        raise FileExistsError("exists")

    monkeypatch.setattr(os, "link", always_exists)
    monkeypatch.setattr(secret_files, "_sleep", fake_sleep)

    result = read_or_create_secret(path)

    assert result == winner
    assert sleep_calls, "the retry loop's sleep hook was never called"


def test_read_or_create_secret_falls_back_when_hard_links_are_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "k"

    def unsupported_link(_src: str, _dst: str) -> None:
        raise OSError("hard links not supported on this filesystem")

    monkeypatch.setattr(os, "link", unsupported_link)
    secret = read_or_create_secret(path)
    assert len(secret) >= MIN_SECRET_BYTES
    assert path.read_bytes() == secret
    assert [p.name for p in tmp_path.iterdir()] == ["k"]


def test_read_or_create_secret_regenerates_a_short_file_even_when_nbytes_is_smaller(tmp_path: Path) -> None:
    path = tmp_path / "k"
    stale = b"x" * 16
    path.write_bytes(stale)
    fresh = read_or_create_secret(path, nbytes=16)
    assert len(fresh) >= MIN_SECRET_BYTES
    assert fresh != stale
    assert path.read_bytes() == fresh


@posix_only
def test_ensure_secrets_dir_tightens_an_existing_loose_directory(tmp_path: Path) -> None:
    directory = tmp_path / "secrets"
    directory.mkdir()
    os.chmod(directory, 0o755)
    ensure_secrets_dir(directory)
    assert _mode(directory) == "0o700"


@posix_only
def test_tightening_a_loose_file_logs_only_the_file_name(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "k"
    path.write_bytes(b"x" * 32)
    os.chmod(path, 0o644)
    with caplog.at_level(logging.WARNING, logger="henchmen.config.secret_files"):
        read_or_create_secret(path)
    assert caplog.records, "the tightening warning was never logged"
    messages = [record.getMessage() for record in caplog.records]
    assert any(path.name in message for message in messages)
    assert not any(str(path.parent) in message for message in messages)


@posix_only
def test_tightening_a_loose_file_never_logs_the_secret(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "k"
    original = b"y" * 32
    path.write_bytes(original)
    os.chmod(path, 0o644)
    with caplog.at_level(logging.WARNING, logger="henchmen.config.secret_files"):
        secret = read_or_create_secret(path)
    assert secret == original
    assert caplog.records, "the tightening warning was never logged"
    for record in caplog.records:
        assert original.decode("latin-1") not in record.getMessage()


def test_log_records_never_contain_the_secret_or_its_encoded_forms(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "k"
    path.write_bytes(b"short")
    with caplog.at_level(logging.WARNING, logger="henchmen.config.secret_files"):
        secret = read_or_create_secret(path)

    assert caplog.records, "the regeneration warning was never logged"
    encoded_forms = [
        secret.hex(),
        base64.b64encode(secret).decode(),
        base64.urlsafe_b64encode(secret).decode(),
    ]
    for record in caplog.records:
        message = record.getMessage()
        assert secret.decode("latin-1") not in message
        for form in encoded_forms:
            assert form not in message


# --- Fix round 2 -------------------------------------------------------------


def test_regeneration_returns_whatever_a_racing_replace_left_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two processes finding the same short file both regenerate; whichever `os.replace` lands last wins on disk.

    Each process must return what is actually on disk afterward, not its own
    generated bytes, or the two processes end up disagreeing about the secret.
    """
    path = tmp_path / "k"
    path.write_bytes(b"short")
    second_writer = b"s" * 32
    real_replace = os.replace

    def replace_then_a_second_writer_lands(src: object, dst: object) -> None:
        real_replace(src, dst)
        Path(dst).write_bytes(second_writer)

    monkeypatch.setattr(os, "replace", replace_then_a_second_writer_lands)

    result = read_or_create_secret(path)

    assert result == second_writer
    assert path.read_bytes() == second_writer


@posix_only
def test_create_secret_file_does_not_delete_a_file_that_replaced_it_mid_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent process can `os.replace` its own file onto `path` while our fd is still open.

    Our failure-path cleanup must not unlink that other file just because it
    now sits at the name we originally created. POSIX-only: Windows file
    sharing denies renaming onto a path with an open, unshared handle (which
    is what `os.open` without extra sharing flags produces), so this exact
    interleaving cannot occur there and the scenario cannot be reproduced.
    """
    path = tmp_path / "k"
    other = b"o" * 32

    def failing_write(_fd: int, _data: object) -> int:
        replacement = tmp_path / "replacement"
        replacement.write_bytes(other)
        os.replace(replacement, path)
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", failing_write)
    with pytest.raises(OSError):
        create_secret_file(path, b"a" * 32)
    assert path.read_bytes() == other


def test_publish_new_secret_ignores_cleanup_failure_of_the_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "k"
    real_unlink = Path.unlink

    def flaky_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name.endswith(".tmp"):
            raise PermissionError("cannot delete temp file")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    secret = read_or_create_secret(path)

    assert len(secret) >= MIN_SECRET_BYTES
    assert path.read_bytes() == secret


# --- Fix round 3 -------------------------------------------------------------


def test_create_secret_file_does_not_delete_when_the_file_id_is_unreliable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem that always reports st_ino == 0 (some FAT/exFAT drivers) cannot tell "our file"
    from "someone else's" apart, so identity can never be confirmed and nothing is unlinked.
    """
    path = tmp_path / "k"

    class _UnreliableStat:
        st_dev = 1
        st_ino = 0

    def failing_write(_fd: int, _data: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", failing_write)
    monkeypatch.setattr(os, "fstat", lambda _fd: _UnreliableStat())
    monkeypatch.setattr(os, "stat", lambda _path, *args, **kwargs: _UnreliableStat())

    with pytest.raises(OSError):
        create_secret_file(path, b"a" * 32)

    monkeypatch.undo()  # restore real os.write/os.fstat/os.stat before inspecting the filesystem
    assert path.exists()
    assert path.read_bytes() == b""


def test_sweep_removes_an_old_matching_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "k"
    stale = path.with_name(f"{path.name}.deadbeef.tmp")
    stale.write_bytes(b"leftover")
    old = time.time() - 400  # older than the 5-minute floor
    os.utime(stale, (old, old))

    read_or_create_secret(path)

    assert not stale.exists()


def test_sweep_keeps_a_fresh_matching_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "k"
    fresh = path.with_name(f"{path.name}.cafebabe.tmp")
    fresh.write_bytes(b"leftover")  # default mtime is "now", well under the 5-minute floor

    read_or_create_secret(path)

    assert fresh.exists()


def test_sweep_keeps_a_non_matching_old_file(tmp_path: Path) -> None:
    path = tmp_path / "k"
    unrelated = tmp_path / "other.deadbeef.tmp"
    unrelated.write_bytes(b"unrelated")
    old = time.time() - 400
    os.utime(unrelated, (old, old))

    read_or_create_secret(path)

    assert unrelated.exists()


def test_sweep_keeps_an_old_sibling_whose_random_part_is_not_hex(tmp_path: Path) -> None:
    """``<name>.other.tmp`` looks similar but never came from this module's own temp-naming scheme."""
    path = tmp_path / "k"
    look_alike = path.with_name(f"{path.name}.other.tmp")
    look_alike.write_bytes(b"leftover")
    old = time.time() - 400
    os.utime(look_alike, (old, old))

    read_or_create_secret(path)

    assert look_alike.exists()


def test_write_secret_file_also_sweeps_stale_temp_siblings(tmp_path: Path) -> None:
    path = tmp_path / "k"
    path.write_bytes(b"old")
    stale = path.with_name(f"{path.name}.deadbeef.tmp")
    stale.write_bytes(b"leftover")
    old = time.time() - 400
    os.utime(stale, (old, old))

    write_secret_file(path, b"n" * 32)

    assert not stale.exists()
    assert path.read_bytes() == b"n" * 32


def test_write_secret_file_retries_a_transient_windows_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "k"
    path.write_bytes(b"old")
    real_replace = os.replace
    attempts = {"n": 0}

    def flaky_replace(src: object, dst: object) -> None:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise PermissionError("in use")
        real_replace(src, dst)

    sleeps: list[float] = []
    monkeypatch.setattr(os, "replace", flaky_replace)
    monkeypatch.setattr(secret_files, "_sleep", sleeps.append)

    write_secret_file(path, b"n" * 32)

    assert path.read_bytes() == b"n" * 32
    assert attempts["n"] == 3
    assert sleeps == [0.02, 0.02]


def test_write_secret_file_reraises_after_replace_stays_permission_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "k"
    path.write_bytes(b"old")

    def always_denied(src: object, dst: object) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr(os, "replace", always_denied)
    monkeypatch.setattr(secret_files, "_sleep", lambda _seconds: None)

    with pytest.raises(PermissionError):
        write_secret_file(path, b"n" * 32)
    assert path.read_bytes() == b"old"


def test_replace_with_retry_succeeds_immediately_without_sleeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_bytes(b"data")
    sleeps: list[float] = []
    monkeypatch.setattr(secret_files, "_sleep", sleeps.append)

    replace_with_retry(src, dst)

    assert dst.read_bytes() == b"data"
    assert sleeps == []


symlinks_supported = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


@symlinks_supported
@pytest.mark.parametrize("dangling", [True, False])
def test_a_symlinked_secret_is_a_readable_error_never_followed(tmp_path: Path, dangling: bool) -> None:
    """D3: neither a dangling nor a live symbolic link is read, replaced through, or trusted."""
    secrets_dir = tmp_path / "secrets"
    ensure_secrets_dir(secrets_dir)
    target = tmp_path / "attacker-chosen"
    if not dangling:
        target.write_bytes(b"x" * 64)
    link = secrets_dir / "internal-push.token"
    link.symlink_to(target)
    with pytest.raises(secret_files.SecretFileError, match="symbolic link") as exc_info:
        read_or_create_secret(link)
    assert isinstance(exc_info.value, OSError)
    assert str(link) in str(exc_info.value)
    with pytest.raises(secret_files.SecretFileError):
        write_secret_file(link, b"y" * 32)
    assert link.is_symlink()
    assert dangling or target.read_bytes() == b"x" * 64


@posix_only
def test_a_secret_owned_by_another_user_is_a_readable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_dir = tmp_path / "secrets"
    path = secrets_dir / "operative-task.key"
    read_or_create_secret(path)
    monkeypatch.setattr(secret_files.os, "geteuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(secret_files.SecretFileError, match="owned by another user"):
        read_or_create_secret(path)


def test_a_directory_where_a_secret_should_be_is_a_readable_error(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "console-session.key"
    path.mkdir(parents=True)
    with pytest.raises(secret_files.SecretFileError, match="not a regular file"):
        read_or_create_secret(path)


def test_secret_bytes_are_fsynced_before_they_are_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D3: the temp file is flushed to disk before os.replace publishes it under the final name."""
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def _fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def _replace(src: object, dst: object) -> None:
        events.append("replace")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(secret_files.os, "fsync", _fsync)
    monkeypatch.setattr(secret_files.os, "replace", _replace)
    path = tmp_path / "secrets" / "setup-token"
    ensure_secrets_dir(path.parent)
    write_secret_file(path, b"z" * 32)
    assert "replace" in events
    assert events.index("fsync") < events.index("replace")
    assert path.read_bytes() == b"z" * 32
