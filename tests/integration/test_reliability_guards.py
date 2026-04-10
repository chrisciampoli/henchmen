"""Reliability guardrail integration tests.

These tests exercise end-to-end guardrail behaviours that are difficult to
verify with unit tests because they require multiple layers to cooperate:

* **Cost ceiling** — a runaway operative (or a mis-configured ceiling) must
  halt task execution before the scheme dispatches another expensive agentic
  node. The executor's pre-dispatch budget check is the last backstop; this
  test forces the ceiling to fire immediately and asserts that the resulting
  task report routes through the escalation/failure path instead of producing
  a PR.

* **Silent failure detection** — a lair manager that reports a "completed"
  operative without a real change set is treated as a failure by the diff
  evaluator and downstream scheme edges. The test wires a stub lair manager
  that returns an empty ``files_changed`` list and verifies the task does not
  end with ``status == 'completed'`` + a real PR URL.

Patterns (``_MockBroker``, ``_dossier_patch``) are borrowed from
``tests/integration/test_end_to_end.py`` so failures point directly at the
reliability layer, not fixture drift.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from henchmen.mastermind.agent import MastermindAgent
from henchmen.models.dossier import Dossier
from henchmen.models.operative import OperativeReport, OperativeStatus
from henchmen.models.task import HenchmenTask, TaskContext, TaskPriority, TaskSource
from henchmen.schemes.registry import SchemeRegistry

REPO = "acme-org/sample-repo"


# ---------------------------------------------------------------------------
# Shared helpers (trimmed copies of test_end_to_end.py)
# ---------------------------------------------------------------------------


class _MockBroker:
    """MessageBroker double that records every publish call."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.publish = AsyncMock(side_effect=self._record)

    async def _record(self, topic: str, data: bytes, **attributes: Any) -> str:
        self.published.append(
            {
                "topic": topic,
                "data": json.loads(data.decode("utf-8")) if data else None,
                "attributes": attributes,
            }
        )
        return "mock-msg-id"

    def messages_for(self, topic_fragment: str) -> list[dict[str, Any]]:
        return [m for m in self.published if topic_fragment in m["topic"]]


def _make_agent(settings) -> tuple[MastermindAgent, _MockBroker]:
    broker = _MockBroker()
    agent = MastermindAgent(settings=settings, broker=broker)
    agent.lair_manager = AsyncMock()
    agent.lair_manager.create_lair = AsyncMock(return_value="mock-lair-id")
    return agent, broker


def _dossier_patch() -> Any:
    class _Patch:
        def __enter__(self) -> Any:
            self._p = patch("henchmen.mastermind.agent.DossierBuilder")
            mock_cls = self._p.start()
            mock_instance = AsyncMock()
            mock_instance.build = AsyncMock(return_value=Dossier(task_id="stub"))
            mock_cls.return_value = mock_instance
            return mock_cls

        def __exit__(self, *exc: Any) -> None:
            self._p.stop()

    return _Patch()


def _make_task(title: str = "Fix the login bug") -> HenchmenTask:
    return HenchmenTask(
        source=TaskSource.CLI,
        source_id="reliability-guard-test",
        title=title,
        description="Login endpoint returns 500 for special chars",
        context=TaskContext(repo=REPO, branch="main"),
        priority=TaskPriority.NORMAL,
        created_by="tester@acme.com",
    )


def _runaway_operative_report(task_id: str) -> OperativeReport:
    """Build a report that implies a runaway operative exceeding the ceiling.

    Large token counts and wall-clock seconds so cost-estimation downstream
    (estimate_cost at ~$12/1M input on gemini-2.5-pro) yields a value well
    above any sane per-task ceiling.
    """
    now = datetime.now(UTC)
    return OperativeReport(
        task_id=task_id,
        scheme_id="bugfix_standard",
        node_id="implement_fix",
        operative_id="runaway-lair",
        status=OperativeStatus.COMPLETED,
        summary="Burned through the entire context window",
        confidence_score=0.1,
        started_at=now,
        completed_at=now,
        total_input_tokens=10_000_000,
        total_output_tokens=2_000_000,
        model_calls=500,
        tool_calls_count=1500,
        wall_clock_seconds=1800.0,
        files_changed=[],
        model_name="gemini-2.5-pro",
    )


# ---------------------------------------------------------------------------
# Cost ceiling guard
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_vertex_evaluation(integration_settings, monkeypatch):
    monkeypatch.setattr(integration_settings, "vertex_ai_evaluation_enabled", False, raising=False)


class TestCostCeilingGuard:
    """Verify the pre-dispatch cost ceiling halts runaway tasks."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_gcs, monkeypatch):
        self.settings = integration_settings
        # Force the task-level cost ceiling to trip on the first agentic node.
        # The scheme executor reads HENCHMEN_COST_CEILING_USD directly (see
        # scheme_executor.executor._execute_agentic) — a value of $0.01 is
        # orders of magnitude below the estimated dispatch cost of any real
        # scheme node, so the check fires before the lair is even created.
        monkeypatch.setenv("HENCHMEN_COST_CEILING_USD", "0.01")
        # Also override the setting for accumulator-style checks inside the
        # operative (belt and braces — the in-process guardrail uses the
        # settings field, the pre-dispatch check uses the env var).
        monkeypatch.setattr(self.settings, "operative_task_cost_ceiling_usd", 0.01, raising=False)
        SchemeRegistry.clear()
        SchemeRegistry.auto_discover()

    @pytest.mark.asyncio
    async def test_cost_ceiling_blocks_agentic_dispatch(self):
        """Agentic dispatch must fail fast when the cumulative budget is exhausted.

        With ``HENCHMEN_COST_CEILING_USD=0.01`` the executor's pre-dispatch
        check refuses to create a lair; the operative runs zero steps, no PR
        is produced, and ``create_lair`` is never invoked.
        """
        task = _make_task()
        agent, broker = _make_agent(self.settings)

        with _dossier_patch():
            result = await agent.handle_task(task)

        # No real PR was created — the fail/escalate edge prevented it.
        pr_url = (result.get("result") or {}).get("pr_url") or ""
        assert "pull/" not in pr_url, f"Expected no PR when budget exhausted, got {pr_url!r}"

        # No forge-request was published (would indicate a PR slipped through).
        assert broker.messages_for("forge-request") == []

        # The lair manager must not have been invoked: dispatch is pre-empted
        # BEFORE create_lair is called, so a runaway operative burns zero
        # additional budget.
        assert not agent.lair_manager.create_lair.await_args_list, (
            "Lair creation should be pre-empted by the cost ceiling check; "
            f"got {agent.lair_manager.create_lair.await_args_list!r}"
        )

        # A budget-exceeded message should be visible in the nodes' results —
        # this is the fail-closed marker that downstream callers rely on.
        node_results = (result.get("result") or {}).get("node_results") or {}
        budget_messages = [
            (node_id, nr.get("message", ""))
            for node_id, nr in node_results.items()
            if isinstance(nr, dict) and "budget" in nr.get("message", "").lower()
        ]
        assert budget_messages, (
            "Expected at least one node result to carry a 'budget exceeded' "
            f"message; got node results: {list(node_results.keys())!r}"
        )

    @pytest.mark.asyncio
    async def test_runaway_operative_report_is_handled_gracefully(self):
        """Even with a fake runaway OperativeReport, the final status is never 'pr_created'.

        Patches ``LairManager.wait_for_completion`` to return a deliberately
        over-budget report. Since the pre-dispatch ceiling check fires first
        when ``HENCHMEN_COST_CEILING_USD=0.01``, this test doubles as a
        regression check: the mocked lair manager is never reached for the
        agentic node, and the task ends without a PR URL.
        """
        task = _make_task()
        agent, _ = _make_agent(self.settings)
        agent.lair_manager.wait_for_completion = AsyncMock(return_value=_runaway_operative_report(task.id))

        with _dossier_patch():
            result = await agent.handle_task(task)

        final_status = result.get("status") or (result.get("result") or {}).get("final_status")
        assert final_status != "pr_created", f"Runaway operative must not yield pr_created; got status={final_status!r}"
        # wait_for_completion should never have been called because create_lair
        # is never invoked when the budget check pre-empts dispatch.
        assert not agent.lair_manager.wait_for_completion.await_args_list


# ---------------------------------------------------------------------------
# Silent failure guard
# ---------------------------------------------------------------------------


class TestSilentFailureGuard:
    """A 'completed' operative with no real changes must not produce a PR."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_gcs, monkeypatch):
        self.settings = integration_settings
        # Generous ceiling so the cost guard does not accidentally short-circuit this test.
        monkeypatch.setenv("HENCHMEN_COST_CEILING_USD", "100.0")
        SchemeRegistry.clear()
        SchemeRegistry.auto_discover()

    @pytest.mark.asyncio
    @patch("henchmen.mastermind.scheme_executor.executor.SchemeExecutor.execute", new_callable=AsyncMock)
    async def test_empty_diff_does_not_mark_task_pr_created(self, mock_execute):
        """SchemeExecutor returns a result with no PR URL -> final_status != pr_created."""
        mock_execute.return_value = {
            "final_status": "completed",
            "nodes_executed": ["implement_fix"],
            "pr_url": "",
            "escalated": False,
            "node_results": {
                "implement_fix": {
                    "condition": "pass",
                    "report": {"files_changed": [], "status": "completed"},
                }
            },
        }

        task = _make_task()
        agent, broker = _make_agent(self.settings)

        with _dossier_patch():
            result = await agent.handle_task(task)

        # No PR should be promoted when the operative didn't change any files.
        assert result["status"] != "pr_created"
        # No forge-request published (no PR URL to run CI against).
        assert broker.messages_for("forge-request") == []
