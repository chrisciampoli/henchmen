"""SchemeExecutor - walks a Scheme DAG and dispatches nodes."""

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from henchmen.mastermind.lair_manager import LairManager
from henchmen.models.dossier import Dossier
from henchmen.models.llm import ModelTier
from henchmen.models.operative import OperativeReport, OperativeStatus
from henchmen.models.scheme import NodeType, SchemeNode
from henchmen.models.task import HenchmenTask
from henchmen.schemes.base import SchemeGraph

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

# Conservative per-call output estimate for the pre-dispatch cost gate. The
# operative rarely emits anything close to ``operative_max_output_tokens`` on
# a tool-calling step, and over-estimating here blocks legitimate work.
_ESTIMATED_OUTPUT_TOKENS_PER_CALL = 2_000

# Nodes whose task description is enriched with the preceding check's output.
_FIX_NODES = frozenset({"fix_lint", "fix_tests"})

# Cap on the error text spliced into the fix node's task description.
_MAX_ERROR_CONTEXT_CHARS = 4_000

# How many times an INTERRUPTED operative is re-dispatched before its node
# fails. Interruption is external (SIGTERM), so one retry is worthwhile; more
# would mask a lair that is being killed every time.
_MAX_INTERRUPTED_REDISPATCHES = 1


def validate_deterministic_handlers(scheme_graph: SchemeGraph) -> list[str]:
    """Return a problem message per deterministic node that has no handler.

    A deterministic node without a handler is a fail-closed hazard: the gate it
    represents (lint, tests, verification) would never actually run. Callers
    surface this at startup and refuse to execute the scheme.
    """
    from henchmen.mastermind.scheme_executor.handlers import get_handler

    problems: list[str] = []
    for node in scheme_graph.definition.nodes:
        if node.node_type != NodeType.DETERMINISTIC:
            continue
        if get_handler(node.id) or get_handler(node.name):
            continue
        problems.append(
            f"Scheme '{scheme_graph.definition.id}' has deterministic node '{node.id}' with no registered handler"
        )
    return problems


class SchemeExecutor:
    """Walks a Scheme DAG, dispatching deterministic and agentic nodes."""

    def __init__(self, scheme_graph: SchemeGraph, lair_manager: LairManager, settings: "Settings", tracker: Any = None):
        self.scheme_graph = scheme_graph
        self.lair_manager = lair_manager
        self.settings = settings
        self.tracker = tracker
        self.node_results: dict[str, dict[str, Any]] = {}  # node_id -> result
        self._retry_counts: dict[str, int] = {}  # node_id -> execution count
        self._max_node_retries = 2  # Max times a single node can be re-executed
        self._freshly_executed: set[str] = set()  # nodes executed in this session (excludes checkpoint-restored)
        self._resume_skipped: set[str] = set()  # checkpoint-restored nodes already replayed once
        self._visited_states: set[tuple[str, int]] = set()  # (node_id, retry_count) for cycle detection
        self._escalation_node: str | None = None  # node that caused escalation

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

            # ---- Resume: replay nodes already completed from checkpoint ----
            # A restored node is replayed at most once, and the replay is not a
            # visited state (nothing executed), so a loop-back such as run_tests
            # after fix_tests re-executes instead of replaying a stale result.
            # ``_retry_counts`` is restored alongside ``node_results`` on resume,
            # so keying the skip off it (as this used to) replayed nothing and
            # re-ran the whole scheme.
            existing = self.node_results.get(node_key)
            is_replay = (
                existing is not None and node_key not in self._freshly_executed and node_key not in self._resume_skipped
            )

            if is_replay:
                logger.info("Skipping already-completed node %s (resume from checkpoint)", node_key)
                self._resume_skipped.add(node_key)
                result = existing if existing is not None else {}
            else:
                # ---- Cycle detection: (node_id, retry_count) as visited state ----
                exec_count = self._retry_counts.get(node_key, 0)
                state_key = (node_key, exec_count)
                if state_key in self._visited_states:
                    logger.error(
                        "[SCHEME] Cycle detected: node %s with retry_count=%d already visited",
                        node_key,
                        exec_count,
                    )
                    self._escalation_node = node_key
                    self.node_results[node_key] = {
                        "condition": None,
                        "message": f"Cycle detected at {node_key} — escalating",
                        "escalated": True,
                    }
                    self._freshly_executed.add(node_key)
                    break
                self._visited_states.add(state_key)

                logger.info("Executing node %s (%s)", current_node.id, current_node.node_type.value)
                # Check if this node has been retried too many times (prevents infinite loops)
                if exec_count >= self._max_node_retries:
                    logger.error(
                        "[SCHEME] Node %s hit max retries (%d), forcing FAIL",
                        node_key,
                        self._max_node_retries,
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

            # A passing node with no explicit "pass" edge continues along the
            # scheme's unconditional edge (that is how implement_fix reaches
            # verify_changes). A FAILING node never falls back that way: doing
            # so routed a failed gate straight down the happy path to create_pr.
            if not next_nodes and condition == "pass":
                next_nodes = self.scheme_graph.get_next_nodes(current_node.id, None)

            if not next_nodes:
                # Dead-end with a "fail" condition means unhandled failure — escalate
                if condition == "fail":
                    logger.warning(
                        "Dead-end reached at node %s with condition='fail' — escalating task %s",
                        current_node.id,
                        task.id,
                    )
                    self._escalation_node = current_node.id
                    self.node_results[current_node.id]["escalated"] = True
                break  # Terminal node reached

            current_node = next_nodes[0]  # Schemes are linear with branches

        return self._build_execution_report()

    async def _execute_node(self, node: SchemeNode, task: HenchmenTask, dossier: Dossier) -> dict[str, Any]:
        """Execute a single scheme node."""
        if node.node_type == NodeType.DETERMINISTIC:
            return await self._execute_deterministic(node, task, dossier)
        return await self._execute_agentic(node, task, dossier)

    async def _execute_deterministic(self, node: SchemeNode, task: HenchmenTask, dossier: Dossier) -> dict[str, Any]:
        """Execute a deterministic node (lint, test, branch, PR creation)."""
        from henchmen.mastermind.scheme_executor.handlers import get_handler

        handler = get_handler(node.id) or get_handler(node.name)
        if handler:
            return await handler(self, node, task, dossier)
        # Fail-closed: a missing handler means the gate this node represents
        # (lint, tests, verification) never ran, so it must not report success.
        logger.error("[SCHEME] No handler registered for deterministic node %s (name=%r)", node.id, node.name)
        return {"condition": "fail", "message": f"No handler for deterministic node {node.id}"}

    def _estimate_dispatch_cost(self, node: SchemeNode, dossier: Dossier) -> float:
        """Estimate the cost of dispatching an agentic node.

        Bounded by what the operative actually sends: it trims every prompt to
        the configured system/message token budgets, so the old
        ``max_steps x full-dossier-JSON`` figure over-estimated by an order of
        magnitude and blocked realistic fix/implement nodes.
        """
        try:
            from henchmen.providers.pricing import estimate_cost_for_settings

            per_call_input = int(self.settings.operative_max_system_tokens) + int(
                self.settings.operative_max_message_tokens
            )
            per_call_output = min(int(self.settings.operative_max_output_tokens), _ESTIMATED_OUTPUT_TOKENS_PER_CALL)
            max_calls = max(1, int(node.get_effective_budget().max_steps))
            model_name = node.model_name or ModelTier.COMPLEX.value
            return estimate_cost_for_settings(
                self.settings,
                model_name,
                per_call_input * max_calls,
                per_call_output * max_calls,
            )
        except Exception as exc:
            logger.warning("Cost estimation failed for node %s: %s", getattr(node, "id", "?"), exc)
            return 0.0

    def _get_cumulative_cost(self) -> float:
        """Sum the cost_usd from all completed agentic node reports."""
        from henchmen.providers.pricing import estimate_cost_for_settings

        total = 0.0
        for _node_id, result in self.node_results.items():
            report_data = result.get("report")
            if isinstance(report_data, dict):
                model_name = report_data.get("model_name", "")
                input_tokens = report_data.get("total_input_tokens", 0)
                output_tokens = report_data.get("total_output_tokens", 0)
                total += estimate_cost_for_settings(self.settings, model_name, input_tokens, output_tokens)
        return total

    async def _heartbeat_during_wait(self, task_id: str, interval: int = 120) -> None:
        """Send periodic heartbeats while waiting for an operative to complete."""
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if self.tracker:
                await self.tracker.update_heartbeat(task_id)

    def _enrich_for_fix_node(self, node: SchemeNode, task: HenchmenTask) -> HenchmenTask:
        """Prepend the failing check's output to a fix node's task description.

        The error block goes first so that it survives the environment-variable
        cap applied when the description is handed to the container — the fix
        node is useless without it.
        """
        if node.id not in _FIX_NODES:
            return task
        prior_node = "run_lint" if node.id == "fix_lint" else "run_tests"
        prior_result = self.node_results.get(prior_node, {})
        error_output = prior_result.get("output", prior_result.get("message", ""))
        if not error_output:
            return task
        enriched = task.model_copy()
        enriched.description = (
            f"--- {prior_node.upper()} OUTPUT (FIX THESE ERRORS) ---\n"
            f"{str(error_output)[:_MAX_ERROR_CONTEXT_CHARS]}\n\n"
            f"--- ORIGINAL TASK ---\n{task.description}"
        )
        return enriched

    async def _dispatch_to_lair(
        self, enriched_task: HenchmenTask, node: SchemeNode, task: HenchmenTask, dossier: Dossier
    ) -> tuple[str, OperativeReport]:
        """Provision one Lair for *node*, wait for its report and record it."""
        lair_id = await self.lair_manager.create_lair(
            enriched_task, node, scheme_id=self.scheme_graph.definition.id, dossier=dossier
        )
        logger.info("[SCHEME] Lair %s created, waiting for completion...", lair_id)

        # Run heartbeat concurrently with wait_for_completion
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            heartbeat_task = asyncio.create_task(
                self._heartbeat_during_wait(task.id),
                name=f"executor-heartbeat-{task.id[:8]}",
            )
            report = await self.lair_manager.wait_for_completion(lair_id)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
        logger.info("[SCHEME] Lair %s completed with status: %s", lair_id, report.status)

        if self.tracker:
            await self.tracker.record_node_result(task.id, node.id, report)

        # Post-operative evaluation (feature-flagged)
        await self._maybe_evaluate(task, node, report)
        return lair_id, report

    async def _execute_agentic(self, node: SchemeNode, task: HenchmenTask, dossier: Dossier) -> dict[str, Any]:
        """Execute an agentic node by provisioning a Lair and running an Operative.

        In dev mode (when lair provisioning fails), simulates a successful completion
        so the rest of the pipeline can be tested end-to-end.
        """
        # Pre-dispatch cost budget validation
        cost_ceiling = float(self.settings.operative_task_cost_ceiling_usd)
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
                    f"estimated ${estimated_node_cost:.3f} > ceiling ${cost_ceiling:.2f} "
                    f"(raise HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD)"
                ),
            }

        # For fix nodes, enrich the task with the previous check's error output
        # so the operative knows exactly what to fix.
        enriched_task = self._enrich_for_fix_node(node, task)

        logger.info("[SCHEME] Dispatching agentic node '%s' to Lair for task %s", node.id, task.id)

        try:
            lair_id, report = await self._dispatch_to_lair(enriched_task, node, task, dossier)
            # An INTERRUPTED operative (SIGTERM: instance eviction, platform
            # shutdown) was stopped from outside, not by its own failure, so it
            # gets a bounded re-dispatch before the node is failed.
            redispatches = 0
            while report.status == OperativeStatus.INTERRUPTED and redispatches < _MAX_INTERRUPTED_REDISPATCHES:
                redispatches += 1
                logger.warning(
                    "[SCHEME] Node %s was interrupted — re-dispatching (%d/%d)",
                    node.id,
                    redispatches,
                    _MAX_INTERRUPTED_REDISPATCHES,
                )
                lair_id, report = await self._dispatch_to_lair(enriched_task, node, task, dossier)

            if report.status == OperativeStatus.COMPLETED:
                return {"condition": "pass", "report": report.model_dump(), "lair_id": lair_id}
            return {"condition": "fail", "report": report.model_dump(), "lair_id": lair_id}
        except Exception as exc:
            logger.error("[SCHEME] Lair provisioning failed for node %s: %s", node.id, exc)

            # Only simulate pass in dev mode for implementation nodes.
            # Fix nodes (fix_lint, fix_tests) must NEVER simulate pass —
            # skipping them means broken code gets promoted to PR.
            is_dev = getattr(self.settings, "environment", None)
            is_dev = is_dev and getattr(is_dev, "value", str(is_dev)) == "dev"
            is_fix_node = node.id in _FIX_NODES

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
        """Run post-operative evaluation if enabled in settings.

        The evaluator is Vertex AI's GenAI evaluation service, so it only runs
        when the deployment provider is GCP: on AWS or local it would fail on
        every node (no project, no credentials) and log a warning each time.
        """
        if not self.settings.vertex_ai_evaluation_enabled or self.settings.provider != "gcp":
            return
        try:
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
                logger.info(
                    "[EVAL] Node %s quality=%.2f (fulfillment=%.2f)",
                    node.id,
                    result.overall_quality,
                    result.fulfillment_score,
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
        for _node_id, result in self.node_results.items():
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
            "escalation_node": self._escalation_node,
            "node_results": self.node_results,
            "nodes_executed": list(self._freshly_executed),
        }
