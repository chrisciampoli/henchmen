"""Pytest configuration and shared fixtures."""

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


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
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
