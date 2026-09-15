"""Guard: no test reaches the developer's real ``~/.henchmen`` (see tests/conftest.py).

The session redirects ``Path.home()`` before any test module is imported and
fingerprints the real directory, failing the session if it changes; every
test also gets its own SQLite file, storage directory and eval database.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from henchmen.config.settings import Settings
from tests.conftest import _fingerprint, real_home_henchmen_dir


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def test_path_home_is_redirected_for_the_whole_session(request: pytest.FixtureRequest) -> None:
    real = real_home_henchmen_dir(request.config)
    assert Path.home().resolve() != real.parent.resolve()
    assert not _is_within(Path.home() / ".henchmen", real)


def test_import_time_home_defaults_never_point_at_the_real_directory(request: pytest.FixtureRequest) -> None:
    from henchmen.evals import storage
    from henchmen.providers.local import filesystem, sqlite

    real = real_home_henchmen_dir(request.config)
    assert not _is_within(storage._DEFAULT_DB_PATH, real)
    assert not _is_within(filesystem._DEFAULT_STORAGE_DIR, real)
    blank = Settings(_env_file=None, local_sqlite_path="")  # type: ignore[call-arg]
    assert not _is_within(sqlite.default_db_path(blank), real)


def test_every_test_gets_its_own_local_stores(tmp_path_factory: pytest.TempPathFactory) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    base = tmp_path_factory.getbasetemp()
    for configured in (settings.local_sqlite_path, settings.local_storage_dir, settings.eval_db_path):
        assert configured and _is_within(Path(configured), base)


def test_forge_request_dedup_store_is_per_test() -> None:
    """The regression behind this guard: forge-request dedup markers landed in the real home DB."""
    from henchmen.forge import server

    store = server._get_document_store()
    configured = Path(Settings(_env_file=None).local_sqlite_path)  # type: ignore[call-arg]
    assert store.path.resolve() == configured.resolve()


def test_the_fingerprint_detects_a_new_or_modified_file(tmp_path: Path) -> None:
    before = _fingerprint(tmp_path)
    (tmp_path / "henchmen_dev.db").write_bytes(b"x")
    after_create = _fingerprint(tmp_path)
    assert after_create != before
    time.sleep(0.02)
    (tmp_path / "henchmen_dev.db").write_bytes(b"xy")
    assert _fingerprint(tmp_path) != after_create
    assert _fingerprint(tmp_path / "missing") == {}
