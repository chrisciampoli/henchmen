"""Tests for owner-only, atomic secret files in <data dir>/secrets."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from henchmen.config import secret_files
from henchmen.config.secret_files import (
    MIN_SECRET_BYTES,
    create_secret_file,
    ensure_secrets_dir,
    read_or_create_secret,
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
    path = tmp_path / "k"
    winner = b"w" * 32

    def racing_create(target: Path, data: bytes) -> None:
        target.write_bytes(winner)
        raise FileExistsError(str(target))

    monkeypatch.setattr(secret_files, "create_secret_file", racing_create)
    assert read_or_create_secret(path) == winner


@posix_only
def test_load_tightens_a_loose_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "k"
    path.write_bytes(b"x" * 32)
    os.chmod(path, 0o644)
    read_or_create_secret(path)
    assert _mode(path) == "0o600"
