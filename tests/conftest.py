"""Pytest configuration and shared fixtures.

Conventions enforced here (see CONTRIBUTING.md for the full rationale):

* ``asyncio_mode = "strict"`` in ``pyproject.toml`` — every async test must be
  decorated with ``@pytest.mark.asyncio`` and every async fixture with
  ``@pytest_asyncio.fixture``.
* ``_isolate_settings`` is an autouse fixture that clears the
  ``get_settings`` ``lru_cache`` before and after every test so that
  ``monkeypatch.setenv`` mutations cannot leak between tests. Individual
  tests should never call ``get_settings.cache_clear()`` manually.
* ``mock_settings`` returns a real ``Settings`` instance constructed from
  environment variables, avoiding the duplicated ``_mock_settings()``
  helpers that used to live in ~8 test modules.
* No test may touch the developer's real ``~/.henchmen`` (the local
  DocumentStore, ObjectStore and eval history default there). Two layers keep
  it out of reach: :func:`pytest_configure` points ``Path.home()`` at a
  throwaway directory for the whole session -- before any test module is
  imported, so import-time defaults such as ``evals.storage._DEFAULT_DB_PATH``
  land there too -- and ``_isolate_local_state`` gives every test its own
  SQLite file, storage directory and eval database, so no state survives from
  one test (or one run) to the next. As a guard, the real directory is
  fingerprinted when the session starts and compared when it ends; any change
  fails the session.
"""

import os
import pathlib
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import suppress
from typing import Any

import pytest

from henchmen.config.settings import Settings, get_settings
from henchmen.models.dossier import Dossier, RuleFile
from henchmen.models.operative import OperativeConfig
from henchmen.models.scheme import (
    ArsenalRequirement,
    DossierRequirement,
    NodeType,
    SchemeDefinition,
    SchemeEdge,
    SchemeNode,
)
from henchmen.models.task import HenchmenTask, TaskContext, TaskPriority, TaskSource
from henchmen.utils.github_auth import reset_credentials_providers

# ---------------------------------------------------------------------------
# The developer's real ~/.henchmen is never touched
# ---------------------------------------------------------------------------

_REAL_HOME_HENCHMEN_KEY = pytest.StashKey[pathlib.Path]()
_REAL_HOME_SNAPSHOT_KEY = pytest.StashKey[dict[str, tuple[int, int]]]()
_FAKE_HOME_KEY = pytest.StashKey[pathlib.Path]()
_ORIGINAL_HOME_KEY = pytest.StashKey[Any]()


def _fingerprint(directory: pathlib.Path) -> dict[str, tuple[int, int]]:
    """``{relative path: (size, mtime_ns)}`` for every file under ``directory`` (empty when it does not exist)."""
    if not directory.is_dir():
        return {}
    result: dict[str, tuple[int, int]] = {}
    for path in directory.rglob("*"):
        with suppress(OSError):
            if path.is_file():
                info = path.stat()
                result[str(path.relative_to(directory))] = (info.st_size, info.st_mtime_ns)
    return result


def pytest_configure(config: pytest.Config) -> None:
    """Redirect ``Path.home()`` to a session-scoped temp dir and fingerprint the real ``~/.henchmen``."""
    real_henchmen = pathlib.Path.home() / ".henchmen"
    config.stash[_REAL_HOME_HENCHMEN_KEY] = real_henchmen
    config.stash[_REAL_HOME_SNAPSHOT_KEY] = _fingerprint(real_henchmen)
    fake_home = pathlib.Path(tempfile.mkdtemp(prefix="henchmen-test-home-"))
    config.stash[_FAKE_HOME_KEY] = fake_home
    config.stash[_ORIGINAL_HOME_KEY] = pathlib.Path.__dict__["home"]
    pathlib.Path.home = classmethod(lambda cls: fake_home)  # type: ignore[assignment,method-assign]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the session if anything under the developer's real ``~/.henchmen`` changed."""
    config = session.config
    real_henchmen = config.stash.get(_REAL_HOME_HENCHMEN_KEY, None)
    if real_henchmen is None:
        return
    before = config.stash[_REAL_HOME_SNAPSHOT_KEY]
    after = _fingerprint(real_henchmen)
    if after != before:
        changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
        sys.stderr.write(
            f"\nERROR: the test session modified the developer's real {real_henchmen}: {changed}. "
            "Tests must use the isolated home and per-test stores from tests/conftest.py.\n"
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_unconfigure(config: pytest.Config) -> None:
    original = config.stash.get(_ORIGINAL_HOME_KEY, None)
    if original is not None:
        pathlib.Path.home = original  # type: ignore[method-assign]
    fake_home = config.stash.get(_FAKE_HOME_KEY, None)
    if fake_home is not None:
        shutil.rmtree(fake_home, ignore_errors=True)


def real_home_henchmen_dir(config: pytest.Config) -> pathlib.Path:
    """The developer's real ``~/.henchmen`` as it was before the session redirected ``Path.home()``."""
    return config.stash[_REAL_HOME_HENCHMEN_KEY]


_SERVER_MODULES = ("henchmen.forge.server", "henchmen.mastermind.server", "henchmen.dispatch.server")


@pytest.fixture(autouse=True)
def _isolate_local_state(
    _hermetic_settings_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Give every test its own local DocumentStore file, ObjectStore directory and eval database.

    Runs after ``_hermetic_settings_env`` strips ``HENCHMEN_*`` (it depends on it), and
    drops any document store a server module created lazily on its ``app.state``
    during the test, so the next test cannot reuse it.
    """
    state_dir = tmp_path_factory.mktemp("local-state")
    monkeypatch.setenv("HENCHMEN_LOCAL_SQLITE_PATH", str(state_dir / "henchmen.db"))
    monkeypatch.setenv("HENCHMEN_LOCAL_STORAGE_DIR", str(state_dir / "storage"))
    monkeypatch.setenv("HENCHMEN_EVAL_DB_PATH", str(state_dir / "eval-results.db"))
    had_store = {
        name: hasattr(sys.modules[name].app.state, "document_store")
        for name in _SERVER_MODULES
        if name in sys.modules and hasattr(sys.modules[name], "app")
    }
    yield
    for name in _SERVER_MODULES:
        module = sys.modules.get(name)
        app = getattr(module, "app", None)
        if app is None or had_store.get(name, False) or not hasattr(app.state, "document_store"):
            continue
        store = app.state.document_store
        connection = getattr(store, "_conn", None)
        with suppress(Exception):
            if connection is not None:
                connection.close()
        with suppress(AttributeError):
            delattr(app.state, "document_store")


@pytest.fixture(autouse=True)
def _isolate_settings() -> Iterator[None]:
    """Clear the ``get_settings`` cache before and after every test.

    Autouse: runs for every test in the suite. Without this fixture, a
    ``monkeypatch.setenv`` in one test would silently leak into the next
    because ``get_settings`` is wrapped in ``functools.lru_cache`` and the
    first call freezes the environment. By clearing the cache on both sides
    of ``yield`` we guarantee hermetic state.

    This replaces the 53+ hand-rolled ``get_settings.cache_clear()`` calls
    that previously lived across ``test_config.py``, ``test_dispatch.py``
    and various conftest files.

    Cached GitHub credentials providers are dropped the same way, since they are keyed on Settings values,
    and so are an operative's cached GitHub credentials (only when that module is already imported, so the
    operative package is not imported into every test session).
    """
    get_settings.cache_clear()
    reset_credentials_providers()
    operative_credentials = sys.modules.get("henchmen.operative.github_credentials")
    if operative_credentials is not None:
        operative_credentials.reset_operative_credentials()
    yield
    get_settings.cache_clear()
    reset_credentials_providers()
    operative_credentials = sys.modules.get("henchmen.operative.github_credentials")
    if operative_credentials is not None:
        operative_credentials.reset_operative_credentials()


@pytest.fixture(autouse=True)
def _hermetic_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ``HENCHMEN_*`` variable from the environment.

    ``Settings`` reads the process environment, so without this a developer's
    exported ``HENCHMEN_LLM_PROVIDER`` — or one a previous test set and did not
    clean up — silently changes what the next test resolves. That is how the
    tier-pricing assertions became order-dependent: under one ordering the
    active provider was Ollama, whose models are free, so "every tier has a
    price" failed for reasons that had nothing to do with the code under test.

    ``HENCHMEN_PROVIDER`` is then pinned to ``local``. The default is ``gcp``,
    so without this a stripped environment sends any test that builds a
    provider at a real Firestore/Pub/Sub client — which is exactly what
    happens on a machine with no ``.env.local``, such as CI. A test that wants
    another provider sets it itself.
    """
    for name in [n for n in os.environ if n.startswith("HENCHMEN_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HENCHMEN_PROVIDER", "local")


@pytest.fixture
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Return a real ``Settings`` instance with test-safe defaults.

    Shared across unit and integration tests. Replaces per-module
    ``_mock_settings()`` helpers that built ``MagicMock`` settings objects
    — using the real class catches schema drift and keeps the tests honest.

    ``_env_file=None`` keeps the developer's ``.env.local`` out of the test
    run; combined with ``_hermetic_settings_env`` the result depends only on
    what the test itself sets.
    """
    monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
    monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
    monkeypatch.setenv("HENCHMEN_GCP_REGION", "us-central1")
    # `_isolate_settings` already cleared the cache; re-clear here defensively
    # so that the setenv calls above are picked up for this fixture's return.
    get_settings.cache_clear()
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Alias for backwards compatibility with older tests."""
    monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def sample_task_context() -> TaskContext:
    return TaskContext(
        repo="acme-corp/backend",
        branch="main",
        thread_messages=["Fix the authentication bug in login endpoint"],
    )


@pytest.fixture
def sample_task(sample_task_context: TaskContext) -> HenchmenTask:
    return HenchmenTask(
        source=TaskSource.SLACK,
        source_id="C01234567/1700000000.000001",
        title="Fix authentication bug",
        description="The login endpoint is returning 500 errors for users with special characters in passwords.",
        context=sample_task_context,
        priority=TaskPriority.HIGH,
        created_by="U01234567",
    )


@pytest.fixture
def sample_scheme_node() -> SchemeNode:
    return SchemeNode(
        id="investigate",
        name="Investigate Bug",
        node_type=NodeType.AGENTIC,
        arsenal_requirement=ArsenalRequirement(
            tool_sets=["code_intel", "git_ops"],
            allow_destructive=False,
        ),
        dossier_requirement=DossierRequirement(
            fetch_files=True,
            fetch_rules=True,
            fetch_related_prs=False,
            fetch_related_issues=True,
            code_search_symbols=[],
        ),
        max_steps=15,
        timeout_seconds=300,
    )


@pytest.fixture
def sample_scheme(sample_scheme_node: SchemeNode) -> SchemeDefinition:
    fix_node = SchemeNode(
        id="fix",
        name="Apply Fix",
        node_type=NodeType.AGENTIC,
        arsenal_requirement=ArsenalRequirement(
            tool_sets=["code_intel", "code_edit", "git_ops"],
            allow_destructive=False,
        ),
        max_steps=20,
        timeout_seconds=600,
    )
    return SchemeDefinition(
        id="bug-fix-v1",
        name="Bug Fix",
        description="Investigate and fix a reported bug",
        version="1.0.0",
        nodes=[sample_scheme_node, fix_node],
        edges=[SchemeEdge(from_node="investigate", to_node="fix", condition="pass")],
    )


@pytest.fixture
def sample_operative_config(sample_task: HenchmenTask) -> OperativeConfig:
    return OperativeConfig(
        task_id=sample_task.id,
        node_id="investigate",
        scheme_id="bug-fix-v1",
    )


@pytest.fixture
def sample_dossier(sample_task: HenchmenTask) -> Dossier:
    return Dossier(
        task_id=sample_task.id,
        rule_files=[
            RuleFile(
                path="CLAUDE.md",
                scope=".",
                content="# Project Rules\nFollow PEP 8 style guidelines.",
            )
        ],
        relevant_files=["src/auth/login.py", "tests/test_auth.py"],
    )
