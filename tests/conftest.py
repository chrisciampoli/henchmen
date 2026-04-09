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
"""

from collections.abc import Iterator

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
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Return a real ``Settings`` instance with test-safe defaults.

    Shared across unit and integration tests. Replaces per-module
    ``_mock_settings()`` helpers that built ``MagicMock`` settings objects
    — using the real class catches schema drift and keeps the tests honest.
    """
    monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
    monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
    monkeypatch.setenv("HENCHMEN_GCP_REGION", "us-central1")
    # `_isolate_settings` already cleared the cache; re-clear here defensively
    # so that the setenv calls above are picked up for this fixture's return.
    get_settings.cache_clear()
    return get_settings()


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
