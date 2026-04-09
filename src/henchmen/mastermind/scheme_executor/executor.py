"""SchemeExecutor - walks a Scheme DAG and dispatches nodes."""

import logging
import os
from typing import Any

from henchmen.mastermind.lair_manager import LairManager
from henchmen.models.dossier import Dossier
from henchmen.models.operative import OperativeStatus
from henchmen.models.scheme import NodeType, SchemeNode
from henchmen.models.task import HenchmenTask
from henchmen.observability.tracker import estimate_cost
from henchmen.schemes.base import SchemeGraph

logger = logging.getLogger(__name__)

# Default per-task cost ceiling in USD — prevents runaway agent spend.
# Override via HENCHMEN_COST_CEILING_USD env var.
_DEFAULT_COST_CEILING_USD = 2.0


class SchemeExecutor:
    """Walks a Scheme DAG, dispatching deterministic and agentic nodes."""

    def __init__(self, scheme_graph: SchemeGraph, lair_manager: LairManager, settings: Any, tracker: Any = None):
        self.scheme_graph = scheme_graph
        self.lair_manager = lair_manager
        self.settings = settings
        self.tracker = tracker
        self.node_results: dict[str, dict[str, Any]] = {}  # node_id -> result
        self._retry_counts: dict[str, int] = {}  # node_id -> execution count
        self._max_node_retries = 2  # Max times a single node can be re-executed
        self._freshly_executed: set[str] = set()  # nodes executed in this session (excludes checkpoint-restored)
        self._visited_states: set[tuple[str, int]] = set()  # (node_id, retry_count) for cycle detection

    async def execute(self, task: HenchmenTask, dossier: Dossier) -> dict[str, Any]:
        """Execute the scheme from root to completion.

        Supports resume-from-checkpoint: if ``node_results`` is pre-populated
        (e.g. loaded from Firestore), already-completed nodes are skipped and
        their cached results are reused.  After every node execution the state
        is checkpointed via ``self.tracker.update_execution_state`` so a
        Watchdog can restart from the last good checkpoint.

        Returns dict with final_status, pr_url, node_results, etc.
        """
        current_node: SchemeNode | None = self.scheme_graph.get_root_node()

        while current_node:
            node_key = current_node.id

            # ---- Cycle detection: (node_id, retry_count) as visited state ----
            exec_count = self._retry_counts.get(node_key, 0)
            state_key = (node_key, exec_count)
            if state_key in self._visited_states:
                print(
                    f"[SCHEME] Cycle detected: node {node_key} with retry_count={exec_count} already visited",
                    flush=True,
                )
                self.node_results[node_key] = {
                    "condition": None,
                    "message": f"Cycle detected at {node_key} — escalating",
                    "escalated": True,
                }
                self._freshly_executed.add(node_key)
                break
            self._visited_states.add(state_key)

            # ---- Resume: skip nodes already completed from checkpoint ----
            existing = self.node_results.get(node_key)
            if existing is not None and node_key not in self._retry_counts:
                logger.info("Skipping already-completed node %s (resume from checkpoint)", node_key)
                result = existing
            else:
                logger.info("Executing node %s (%s)", current_node.id, current_node.node_type.value)
                # Check if this node has been retried too many times (prevents infinite loops)
                if exec_count >= self._max_node_retries:
                    print(
                        f"[SCHEME] Node {node_key} hit max retries ({self._max_node_retries}), forcing FAIL",
                        flush=True,
                    )
                    result = {"condition": "fail", "message": f"Max retries reached for {node_key} — escalating"}
                else:
                    result = await self._execute_node(current_node, task, dossier)
                    self._retry_counts[node_key] = exec_count + 1

                self.node_results[current_node.id] = result
                self._freshly_executed.add(current_node.id)

                # ---- Checkpoint to Firestore after each executed node ----
                if self.tracker:
                    try:
                        await self.tracker.update_execution_state(
                            task_id=task.id,
                            current_node_id=current_node.id,
                            node_results=self.node_results,
                            retry_counts=self._retry_counts,
                        )
                    except Exception as exc:
                        logger.warning("Checkpoint failed for node %s: %s", current_node.id, exc)

            # Determine next node based on result condition
            condition = result.get("condition")  # "pass", "fail", or None
            next_nodes = self.scheme_graph.get_next_nodes(current_node.id, condition)

            if not next_nodes:
                # If a condition was returned but no matching conditional edge exists,
                # try unconditional edges as a fallback
                if condition is not None:
                    next_nodes = self.scheme_graph.get_next_nodes(current_node.id, None)

            if not next_nodes:
                # Dead-end with a "fail" condition means unhandled failure — escalate
                if condition == "fail":
                    logger.warning(
                        "Dead-end reached at node %s with condition='fail' — escalating task %s",
                        current_node.id,
                        task.id,
                    )
                    self.node_results[current_node.id]["escalated"] = True
                break  # Terminal node reached

            current_node = next_nodes[0]  # Schemes are linear with branches

        return self._build_execution_report()

    async def _execute_node(self, node: SchemeNode, task: HenchmenTask, dossier: Dossier) -> dict[str, Any]:
        """Execute a single scheme node."""
        if node.node_type == NodeType.DETERMINISTIC:
            return await self._execute_deterministic(node, task, dossier)
        else:
            return await self._execute_agentic(node, task, dossier)

    async def _execute_deterministic(self, node: SchemeNode, task: HenchmenTask, dossier: Dossier) -> dict[str, Any]:
        """Execute a deterministic node (lint, test, branch, PR creation)."""
        from henchmen.mastermind.scheme_executor.handlers import get_handler

        handler = get_handler(node.id) or get_handler(node.name)
        if handler:
            return await handler(self, node, task, dossier)
        return {"condition": "pass", "message": f"No handler for deterministic node {node.id}"}

    def _estimate_dispatch_cost(self, node: SchemeNode, dossier: Dossier) -> float:
        """Estimate the maximum cost of dispatching an agentic node."""
        try:
            from henchmen.operative.tokenizer import estimate_tokens

            dossier_text = dossier.model_dump_json()
            estimated_input_tokens_per_call = max(estimate_tokens(dossier_text), 2000)
            estimated_output_per_call = 500
            max_calls = int(node.max_steps)
            model_name = str(node.model_name or "gemini-2.5-pro")
            total_input = estimated_input_tokens_per_call * max_calls
            total_output = estimated_output_per_call * max_calls
            return estimate_cost(model_name, total_input, total_output)
        except Exception as exc:
            logger.warning("Cost estimation failed for node %s: %s", getattr(node, "id", "?"), exc)
            return 0.0

    def _get_cumulative_cost(self) -> float:
        """Sum the cost_usd from all completed agentic node reports."""
        total = 0.0
        for _node_id, result in self.node_results.items():
            report_data = result.get("report")
            if isinstance(report_data, dict):
                model_name = report_data.get("model_name", "")
                input_tokens = report_data.get("total_input_tokens", 0)
                output_tokens = report_data.get("total_output_tokens", 0)
                total += estimate_cost(model_name, input_tokens, output_tokens)
        return total

    async def _execute_agentic(self, node: SchemeNode, task: HenchmenTask, dossier: Dossier) -> dict[str, Any]:
        """Execute an agentic node by provisioning a Lair and running an Operative.

        In dev mode (when lair provisioning fails), simulates a successful completion
        so the rest of the pipeline can be tested end-to-end.
        """
        import sys

        # Pre-dispatch cost budget validation
        ceiling_env = os.environ.get("HENCHMEN_COST_CEILING_USD", "")
        cost_ceiling = float(ceiling_env) if ceiling_env else _DEFAULT_COST_CEILING_USD
        cumulative_cost = self._get_cumulative_cost()
        estimated_node_cost = self._estimate_dispatch_cost(node, dossier)

        if cumulative_cost + estimated_node_cost > cost_ceiling:
            from henchmen.observability.structured_logging import emit_cost_exceeded

            emit_cost_exceeded(task.id, cumulative_cost + estimated_node_cost, cost_ceiling)
            logger.warning(
                "Pre-dispatch budget exceeded for node %s (task %s): "
                "cumulative=$%.3f + estimated=$%.3f > ceiling=$%.2f",
                node.id,
                task.id,
                cumulative_cost,
                estimated_node_cost,
                cost_ceiling,
            )
            return {
                "condition": "fail",
                "message": (
                    f"Cost budget exceeded: cumulative ${cumulative_cost:.3f} + "
                    f"estimated ${estimated_node_cost:.3f} > ceiling ${cost_ceiling:.2f}"
                ),
            }

        # For fix nodes, enrich the task with the previous check's error output
        # so the operative knows exactly what to fix.
        enriched_task = task
        if node.id in ("fix_lint", "fix_tests"):
            prior_node = "run_lint" if node.id == "fix_lint" else "run_tests"
            prior_result = self.node_results.get(prior_node, {})
            error_output = prior_result.get("output", prior_result.get("message", ""))
            if error_output:
                enriched_task = task.model_copy()
                enriched_task.description = (
                    f"{task.description}\n\n"
                    f"--- {prior_node.upper()} OUTPUT (FIX THESE ERRORS) ---\n"
                    f"{str(error_output)[:2000]}"
                )

        print(f"[SCHEME] Dispatching agentic node '{node.id}' to Lair for task {task.id}", flush=True)

        try:
            lair_id = await self.lair_manager.create_lair(
                enriched_task, node, scheme_id=self.scheme_graph.definition.id
            )
            print(f"[SCHEME] Lair {lair_id} created, waiting for completion...", flush=True)
            report = await self.lair_manager.wait_for_completion(lair_id)
            print(f"[SCHEME] Lair {lair_id} completed with status: {report.status}", flush=True)

            if self.tracker:
                await self.tracker.record_node_result(task.id, node.id, report)

            # Post-operative evaluation (feature-flagged)
            await self._maybe_evaluate(task, node, report)

            if report.status == OperativeStatus.COMPLETED:
                return {"condition": "pass", "report": report.model_dump(), "lair_id": lair_id}
            else:
                return {"condition": "fail", "report": report.model_dump(), "lair_id": lair_id}
        except Exception as exc:
            print(f"[SCHEME] Lair provisioning failed for node {node.id}: {exc}", file=sys.stderr, flush=True)

            # Only simulate pass in dev mode for implementation nodes.
            # Fix nodes (fix_lint, fix_tests) must NEVER simulate pass —
            # skipping them means broken code gets promoted to PR.
            is_dev = getattr(self.settings, "environment", None)
            is_dev = is_dev and getattr(is_dev, "value", str(is_dev)) == "dev"
            is_fix_node = node.id in ("fix_lint", "fix_tests")

            if is_dev and not is_fix_node:
                logger.warning(
                    "Lair provisioning failed for node %s (task %s): %s — dev mode: simulating pass",
                    node.id,
                    task.id,
                    exc,
                )
                return {
                    "condition": "pass",
                    "dev_mode": True,
                    "message": f"Agentic node '{node.name}' simulated (lair unavailable): {exc}",
                }
            else:
                logger.error(
                    "Lair provisioning failed for node %s (task %s): %s — failing node",
                    node.id,
                    task.id,
                    exc,
                )
                return {
                    "condition": "fail",
                    "message": f"Agentic node '{node.name}' failed (lair unavailable): {exc}",
                }

    async def _maybe_evaluate(self, task: HenchmenTask, node: SchemeNode, report: Any) -> None:
        """Run post-operative evaluation if enabled in settings."""
        try:
            if not getattr(self.settings, "vertex_ai_evaluation_enabled", False):
                return

            from henchmen.observability.evaluator import OperativeEvaluator, evaluate_and_record

            evaluator = OperativeEvaluator(
                project_id=self.settings.gcp_project_id,
                region=self.settings.gcp_region,
            )
            result = await evaluate_and_record(
                evaluator=evaluator,
                tracker=self.tracker,
                task_id=task.id,
                task_title=task.title,
                task_description=task.description,
                report=report,
                node_instruction=node.instruction_template or "",
            )
            if result.evaluation_error:
                logger.warning("Evaluation failed for node %s: %s", node.id, result.evaluation_error)
            else:
                print(
                    f"[EVAL] Node {node.id} quality={result.overall_quality:.2f} "
                    f"(fulfillment={result.fulfillment_score:.2f})",
                    flush=True,
                )
        except Exception as exc:
            logger.warning("Evaluation skipped for node %s: %s", node.id, exc)

    # ------------------------------------------------------------------
    # Report building
    # ------------------------------------------------------------------

    def _build_execution_report(self) -> dict[str, Any]:
        """Build a summary report from all node execution results."""
        # Find PR URL if one was created
        pr_url = None
        escalated = False
        for node_id, result in self.node_results.items():
            if result.get("pr_url"):
                pr_url = result["pr_url"]
            if result.get("escalated"):
                escalated = True

        if escalated:
            final_status = "escalated"
        elif pr_url:
            final_status = "pr_created"
        else:
            final_status = "completed"

        return {
            "final_status": final_status,
            "pr_url": pr_url,
            "escalated": escalated,
            "node_results": self.node_results,
            "nodes_executed": list(self._freshly_executed),
        }
