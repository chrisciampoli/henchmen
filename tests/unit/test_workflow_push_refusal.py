"""Tests for recognising and reporting pushes GitHub refuses because they change CI workflows (amendment A4)."""

from __future__ import annotations

from typing import Any

import pytest

from henchmen.config.settings import Settings
from henchmen.mastermind.agent import escalation_reason
from henchmen.utils.git import (
    WORKFLOW_PUSH_REFUSED_MESSAGE,
    WorkflowPushRefusedError,
    is_workflow_push_refusal,
)

REFUSAL = (
    "To https://github.com/acme/webapp.git\n"
    " ! [remote rejected] henchmen/abcd1234 -> henchmen/abcd1234 (refusing to allow a GitHub App to create or "
    "update workflow `.github/workflows/ci.yml` without `workflows` permission)\n"
    "error: failed to push some refs to 'https://github.com/acme/webapp.git'"
)


@pytest.mark.parametrize(
    "stderr",
    [
        REFUSAL,
        "refusing to allow an OAuth App to create or update workflow "
        "`.github/workflows/x.yml` without `workflow` scope",
    ],
)
def test_recognises_workflow_refusals(stderr: str) -> None:
    assert is_workflow_push_refusal(stderr)


@pytest.mark.parametrize(
    "stderr",
    ["", "remote: Permission to acme/webapp.git denied", " ! [rejected] henchmen/x -> henchmen/x (non-fast-forward)"],
)
def test_other_push_failures_are_not_workflow_refusals(stderr: str) -> None:
    assert not is_workflow_push_refusal(stderr)


def test_message_names_the_workflow_directory() -> None:
    assert ".github/workflows/" in WORKFLOW_PUSH_REFUSED_MESSAGE


def _fake_git(push_stderr: str, push_rc: int) -> Any:
    async def run_git(workspace_dir: str, *args: str) -> tuple[str, str, int]:
        if args[:1] == ("push",):
            return "", push_stderr, push_rc
        return "", "", 0

    return run_git


@pytest.mark.asyncio
async def test_branch_push_raises_a_workflow_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    from henchmen.operative import bootstrap

    monkeypatch.setattr(bootstrap, "run_git", _fake_git(REFUSAL, 1))
    with pytest.raises(WorkflowPushRefusedError):
        await bootstrap._create_branch_and_push("/workspace/t", "henchmen/abcd1234", Settings(**{"_env_file": None}))


@pytest.mark.asyncio
async def test_other_push_failures_still_raise_runtime_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from henchmen.operative import bootstrap

    monkeypatch.setattr(bootstrap, "run_git", _fake_git("fatal: Authentication failed", 128))
    with pytest.raises(RuntimeError) as exc_info:
        await bootstrap._create_branch_and_push("/workspace/t", "henchmen/abcd1234", Settings(**{"_env_file": None}))
    assert not isinstance(exc_info.value, WorkflowPushRefusedError)


@pytest.mark.asyncio
async def test_push_changes_blocks_the_node_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from henchmen.operative import bootstrap

    monkeypatch.setattr(bootstrap, "run_git", _fake_git(REFUSAL, 1))
    result: dict[str, Any] = {"summary": "done"}
    await bootstrap._push_changes("/workspace/t", "henchmen/abcd1234", Settings(**{"_env_file": None}), result)
    assert result["blocked"] is True
    assert result["block_reason"] == WORKFLOW_PUSH_REFUSED_MESSAGE
    assert "branch_pushed" not in result


@pytest.mark.asyncio
async def test_push_changes_records_a_successful_push(monkeypatch: pytest.MonkeyPatch) -> None:
    from henchmen.operative import bootstrap

    monkeypatch.setattr(bootstrap, "run_git", _fake_git("", 0))
    result: dict[str, Any] = {}
    await bootstrap._push_changes("/workspace/t", "henchmen/abcd1234", Settings(**{"_env_file": None}), result)
    assert result == {"branch_pushed": "henchmen/abcd1234"}


def test_escalation_reason_prefers_the_nodes_own_reason() -> None:
    result = {
        "escalation_node": "implement_fix",
        "node_results": {"implement_fix": {"escalation_reason": WORKFLOW_PUSH_REFUSED_MESSAGE}},
    }
    assert escalation_reason(result) == WORKFLOW_PUSH_REFUSED_MESSAGE
    assert escalation_reason({"escalation_node": "run_tests", "node_results": {"run_tests": {}}}) == (
        "Escalated at node: run_tests"
    )
    assert escalation_reason({}) == "Escalated during execution"
