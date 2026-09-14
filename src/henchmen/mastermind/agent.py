"""MastermindAgent - central orchestrator that receives tasks, selects schemes, and manages execution.

Task lifecycle state lives in Firestore ``task_executions/{task_id}``
documents, managed by :class:`SchemeExecutor` (node-level results,
retry counts, and execution_state checkpoints) and
:class:`~henchmen.observability.tracker.TaskTracker` (cost / cumulative
metrics, heartbeat, finalization).  There is no in-memory state
machine — an earlier ``TaskStateMachine`` was decorative (built per
request, mutated, discarded, never persisted) and has been removed.
Crash recovery flows through ``resume_task`` reading the Firestore
checkpoint fields written by the executor.
"""

import json
import logging
import re
from typing import Any

from henchmen.config.settings import Settings, get_settings
from henchmen.dossier.builder import DossierBuilder
from henchmen.dossier.embedder import query_similar_chunks
from henchmen.forge.error_extractor import extract_ci_errors, format_errors_for_operative
from henchmen.mastermind.lair_manager import LairManager
from henchmen.mastermind.scheme_executor import SchemeExecutor, validate_deterministic_handlers
from henchmen.models.dossier import CodeSearchResult, Dossier, RelatedIssue
from henchmen.models.scheme import NodeType
from henchmen.models.task import HenchmenTask, TaskType
from henchmen.observability.tracker import TaskTracker
from henchmen.providers.interfaces.container_orchestrator import ContainerOrchestrator
from henchmen.providers.interfaces.document_store import DocumentStore
from henchmen.providers.interfaces.llm_provider import LLMProvider
from henchmen.providers.interfaces.message_broker import MessageBroker
from henchmen.schemes.base import SchemeGraph
from henchmen.schemes.registry import SchemeRegistry
from henchmen.utils.git import get_github_token

logger = logging.getLogger(__name__)

# Maximum number of in-flight task IDs to retain in the local tracking set.
# Used only for in-memory dedup hints — authoritative state is in Firestore.
_MAX_ACTIVE_TASKS = 200

# Semantic chunks kept after LLM reranking of the vector-search results.
_RERANK_TOP_K = 10

# Scheme selection keywords. Matched on word boundaries so "address" does not
# read as "add", "prefix" as "fix", and "debug" as "bug".
_FEATURE_KEYWORDS = (
    "feature",
    "implement",
    "build",
    "create",
    "add",
    "new module",
    "new endpoint",
    "new page",
    "new component",
    "set up",
    "setup",
    "scaffold",
    "portal",
    "dashboard",
    "homepage",
)
_BUGFIX_KEYWORDS = ("bug", "fix", "error", "crash", "broken")
# Checked against the title only, and before the single-word lists, so the
# multi-word goal phrases are reachable at all ("fix all" would otherwise be
# swallowed by the "fix" bugfix keyword).
_GOAL_KEYWORDS = (
    "improve",
    "optimize",
    "refactor all",
    "fix all",
    "update all",
    "increase coverage",
    "reduce",
    "clean up all",
    "migrate",
)


def _matches_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    """True when any keyword appears in *text* as a whole word/phrase."""
    return any(re.search(rf"\b{re.escape(kw)}\b", text) for kw in keywords)


class MastermindAgent:
    """Central orchestrator agent. Receives tasks, selects schemes, manages execution."""

    def __init__(
        self,
        settings: Settings | None = None,
        broker: MessageBroker | None = None,
        document_store: DocumentStore | None = None,
        container_orchestrator: ContainerOrchestrator | None = None,
        llm_provider: LLMProvider | None = None,
    ):
        self.settings = settings or get_settings()
        self._broker = broker
        # Used only to rerank semantic chunks; resolved lazily so a Mastermind
        # without LLM credentials still starts and still retrieves context.
        self._llm_provider = llm_provider
        self.lair_manager = LairManager(
            self.settings,
            container_orchestrator=container_orchestrator,
            document_store=document_store,
        )
        # In-memory set of task IDs currently being handled by this process.
        # Authoritative state lives in Firestore `task_executions/{task_id}`;
        # this set is only a hint for local cleanup and test introspection.
        self._active_tasks: set[str] = set()
        self.tracker = TaskTracker(self.settings, document_store=document_store)

    def _get_broker(self) -> MessageBroker:
        """Lazy-init MessageBroker from GCP Pub/Sub when not injected."""
        if self._broker is None:
            from henchmen.providers.gcp.pubsub import PubSubMessageBroker

            self._broker = PubSubMessageBroker(self.settings)
        return self._broker

    def _get_llm_provider(self) -> LLMProvider:
        """Lazy-init the configured LLMProvider when not injected."""
        if self._llm_provider is None:
            from henchmen.providers.registry import ProviderRegistry

            self._llm_provider = ProviderRegistry(self.settings).get_llm_provider()
        return self._llm_provider

    def _cleanup_in_memory_state(self) -> None:
        """Evict stale entries from in-memory dicts to prevent unbounded growth.

        Authoritative task lifecycle state lives in Firestore; these in-memory
        structures are only used for request-scoped bookkeeping.
        """
        # Bound the active-task set. Without a per-task terminal marker in
        # memory, simply trim arbitrarily when we exceed the cap — the real
        # source of truth is Firestore.
        if len(self._active_tasks) > _MAX_ACTIVE_TASKS:
            excess = len(self._active_tasks) - _MAX_ACTIVE_TASKS
            for tid in list(self._active_tasks)[:excess]:
                self._active_tasks.discard(tid)
            logger.info("[cleanup] Evicted %d entries from _active_tasks", excess)

    async def handle_task(self, task: HenchmenTask) -> dict[str, Any]:
        """Process a task through its full lifecycle.

        Lifecycle state is written to Firestore by ``TaskTracker`` and the
        ``SchemeExecutor`` as execution progresses; this method does not
        maintain any separate in-memory state machine.
        """
        self._cleanup_in_memory_state()

        # Layer 3: Task-level dedup — don't process if already running or stalled
        existing = await self.tracker.get_task(task.id)
        if existing and existing.get("execution_state") in ("running", "stalled"):
            logger.info("Task %s already in state '%s', skipping", task.id, existing["execution_state"])
            return {"status": "already_running", "task_id": task.id}

        self._active_tasks.add(task.id)

        try:
            # 1. Select scheme and persist start-of-task marker
            scheme_id = await self._select_scheme(task)
            await self.tracker.start_task(task, scheme_id)

            # 2. Get scheme graph
            scheme_graph = SchemeRegistry.get(scheme_id)
            if not scheme_graph:
                reason = f"Unknown scheme: {scheme_id}"
                await self.tracker.finalize_task(task.id, "escalated")
                return {"status": "escalated", "reason": reason, "task_id": task.id}

            # 2b. Refuse to run a scheme whose deterministic gates have no
            #     handler — those gates would silently be skipped.
            handler_problems = validate_deterministic_handlers(scheme_graph)
            if handler_problems:
                reason = "; ".join(handler_problems)
                logger.error("[MASTERMIND] %s", reason)
                await self.tracker.finalize_task(task.id, "escalated")
                await self.tracker.mark_escalated(task.id, reason=reason)
                return {"status": "escalated", "reason": reason, "task_id": task.id}

            # 3. Build dossier
            dossier = await self._build_dossier(task, scheme_graph)

            # 4. Execute scheme — SchemeExecutor writes node_results and
            #    execution_state checkpoints to Firestore as it runs.
            executor = SchemeExecutor(scheme_graph, self.lair_manager, self.settings, tracker=self.tracker)
            result = await executor.execute(task, dossier)

            # 5. Trigger CI if we have a real PR
            pr_url = result.get("pr_url", "")
            if pr_url and "pull/" in pr_url:
                try:
                    broker = self._get_broker()
                    ci_data = json.dumps({"pr_url": pr_url, "action": "run_ci", "task_id": task.id}).encode("utf-8")
                    await broker.publish(self.settings.pubsub_topic_forge_request, ci_data)
                    logger.info("[MASTERMIND] CI triggered for %s", pr_url)
                except Exception as ci_exc:
                    logger.error("Failed to trigger CI for task %s: %s", task.id, ci_exc)
                    logger.error("[MASTERMIND] Failed to trigger CI: %s", ci_exc)

            pr_url_final = result.get("pr_url", "")
            pr_number = result.get("node_results", {}).get("create_pr", {}).get("pr_number")
            final_status = result.get("final_status", "completed")
            await self.tracker.finalize_task(task.id, final_status, pr_url_final, pr_number)

            # Record escalation whenever the run escalated — even when no
            # single node can be named (cost ceiling, dead-end with no node id),
            # otherwise the task never gets an escalation record.
            if final_status == "escalated":
                escalation_node = result.get("escalation_node")
                reason = f"Escalated at node: {escalation_node}" if escalation_node else "Escalated during execution"
                await self.tracker.mark_escalated(task.id, reason=reason, escalation_node=escalation_node)

            # Log to Vertex AI Experiments if enabled
            try:
                from henchmen.observability.experiments import maybe_log_experiment

                task_data = await self.tracker.get_task(task.id)
                if task_data:
                    await maybe_log_experiment(self.settings, task_data)
            except Exception as exp_exc:
                logger.debug("Experiment logging failed (non-fatal): %s", exp_exc)

            return {
                "status": final_status,
                "task_id": task.id,
                "scheme_id": scheme_id,
                "result": result,
            }

        except Exception as e:
            logger.exception("Error handling task %s", task.id)
            await self.tracker.finalize_task(task.id, "escalated")
            return {"status": "escalated", "task_id": task.id, "error": str(e)}

    async def resume_task(self, task_id: str) -> dict[str, Any]:
        """Resume a task from its last Firestore checkpoint.

        Loads the persisted execution state (node_results, retry_counts) and
        re-runs the SchemeExecutor from where it left off.  Already-completed
        nodes are skipped automatically by the executor's resume logic.
        """
        # Load task state from Firestore
        task_data = await self.tracker.get_task(task_id)
        if not task_data:
            return {"status": "error", "reason": f"Task {task_id} not found in Firestore"}

        # Reconstruct the HenchmenTask from persisted payload
        task_payload = task_data.get("task_payload")
        if not task_payload:
            return {"status": "error", "reason": f"Task {task_id} has no persisted payload for resume"}

        task = HenchmenTask.model_validate(task_payload)

        # Get the scheme
        scheme_id = task_data.get("scheme_id", "")
        scheme_graph = SchemeRegistry.get(scheme_id)
        if not scheme_graph:
            return {"status": "error", "reason": f"Unknown scheme: {scheme_id}"}

        handler_problems = validate_deterministic_handlers(scheme_graph)
        if handler_problems:
            reason = "; ".join(handler_problems)
            logger.error("[MASTERMIND] %s", reason)
            await self.tracker.mark_escalated(task_id, reason=reason)
            return {"status": "escalated", "reason": reason, "task_id": task_id}

        # Build dossier (lightweight — context is mostly cached)
        dossier = await self._build_dossier(task, scheme_graph)

        # Create executor with pre-populated checkpoint state
        executor = SchemeExecutor(scheme_graph, self.lair_manager, self.settings, tracker=self.tracker)

        # Restore checkpoint state
        saved_node_results = task_data.get("node_results", {})
        saved_retry_counts = task_data.get("retry_counts", {})
        executor.node_results = saved_node_results
        executor._retry_counts = saved_retry_counts

        logger.info(
            "[MASTERMIND] Resuming task %s from checkpoint (completed nodes: %s)",
            task_id,
            list(saved_node_results.keys()),
        )

        # Execute (will skip already-completed nodes). The executor and
        # tracker persist state to Firestore as work progresses.
        self._active_tasks.add(task_id)
        result = await executor.execute(task, dossier)

        pr_url_final = result.get("pr_url", "")
        pr_number = result.get("node_results", {}).get("create_pr", {}).get("pr_number")
        final_status = result.get("final_status", "completed")
        await self.tracker.finalize_task(task_id, final_status, pr_url_final, pr_number)

        return {
            "status": final_status,
            "task_id": task_id,
            "scheme_id": scheme_id,
            "result": result,
            "resumed": True,
        }

    async def handle_ci_failure(
        self, task_id_prefix: str, repo: str, branch: str, check_suite_id: int
    ) -> dict[str, Any]:
        """Handle a CI failure by dispatching a fix operative. Max 2 retries."""
        from henchmen.models.operative import OperativeStatus
        from henchmen.models.scheme import ArsenalRequirement, NodeType, SchemeNode
        from henchmen.models.task import HenchmenTask, TaskContext, TaskSource

        # 1. Look up task
        task_data = await self.tracker.get_task_by_id_prefix(task_id_prefix)
        if not task_data:
            return {"status": "skipped", "reason": "task not found"}

        task_id = task_data["task_id"]
        ci_fix_attempts = task_data.get("ci_fix_attempts", 0)
        ci_fix_in_progress = task_data.get("ci_fix_in_progress", False)

        # 2. Dedup check
        if ci_fix_in_progress:
            return {"status": "skipped", "reason": "fix in progress"}

        # 3. Max retries
        if ci_fix_attempts >= 2:
            await self.tracker.record_ci_result(task_id, False)
            reason = "CI still failing after max retries (2 fix attempts)"
            await self.tracker.mark_escalated(task_id, reason=reason)
            return {"status": "escalated", "task_id": task_id, "reason": reason}

        # 4. Extract errors
        github_token = get_github_token()
        errors = await extract_ci_errors(repo, check_suite_id, github_token)
        if not errors:
            return {"status": "skipped", "reason": "no errors found"}

        # 5. Dispatch fix operative
        error_context = format_errors_for_operative(errors)
        await self.tracker.record_ci_fix_attempt(task_id)

        fix_node = SchemeNode(
            id="implement_fix",
            name="Fix CI Failures",
            node_type=NodeType.AGENTIC,
            arsenal_requirement=ArsenalRequirement(tool_sets=["code_edit", "git_ops"], allow_destructive=False),
            max_steps=20,
            timeout_seconds=600,
            instruction_template=(
                "You are fixing CI failures on an existing branch. "
                "Here are the exact errors to fix. Do NOT add features, refactor, or make unrelated changes. "
                "Fix the errors, commit, and push."
            ),
        )

        try:
            fix_task = HenchmenTask(
                source=TaskSource.GITHUB,
                source_id=f"ci-fix-{task_id}",
                title=f"Fix CI failures ({ci_fix_attempts + 1}/2)",
                # The operative resolves its system instruction from the
                # registered scheme node, not from the node object built here,
                # so the per-run error list has to travel in the description.
                description=(
                    "Fix the CI failures below on the existing branch. Do NOT add features, "
                    "refactor, or make unrelated changes. Fix the errors, commit, and push.\n\n"
                    "--- CI ERRORS (FIX THESE) ---\n" + error_context
                ),
                context=TaskContext(repo=repo, branch=branch),
                created_by="henchmen-ci-loop",
            )
            fix_task.id = task_id

            lair_id = await self.lair_manager.create_lair(fix_task, fix_node, scheme_id="bugfix_standard")
            report = await self.lair_manager.wait_for_completion(lair_id)
            await self.tracker.clear_ci_fix_in_progress(task_id)

            if report.status != OperativeStatus.COMPLETED:
                # TIMED_OUT/FAILED/INTERRUPTED are not a dispatched fix: report
                # them as such so the outcome is visible to the caller.
                logger.warning("[CI-LOOP] Fix operative for %s ended as %s", task_id, report.status)
                return {
                    "status": "fix_failed",
                    "task_id": task_id,
                    "attempt": ci_fix_attempts + 1,
                    "lair_id": lair_id,
                    "operative_status": report.status.value,
                    "reason": report.error or report.summary,
                }

            return {"status": "fix_dispatched", "task_id": task_id, "attempt": ci_fix_attempts + 1, "lair_id": lair_id}
        except Exception as exc:
            await self.tracker.clear_ci_fix_in_progress(task_id)
            logger.error("[CI-LOOP] Fix dispatch failed for %s: %s", task_id, exc)
            return {"status": "error", "reason": str(exc)}

    async def _select_scheme(self, task: HenchmenTask) -> str:
        """Select appropriate scheme based on task content.

        Uses keyword matching for now; can be upgraded to LLM-based selection.

        Priority order: goal_decomposition > explicit ``task_type`` > bugfix >
        feature > default. An explicit type the requester chose (e.g. in
        ``henchmen chat``) beats keywords in the text: a bugfix titled
        "Add null check" is still a bugfix. Refactors run the feature scheme.
        Goal keywords are checked against the **title only** (to avoid false
        positives from incidental words in long descriptions/specs) and first,
        because phrases like "fix all" and "update all" contain the single-word
        bugfix/feature keywords and would otherwise be unreachable. Bugfix wins
        over feature so "Fix crash when adding an item" routes to
        ``bugfix_standard``, as documented in docs/schemes.md.

        All matches are on word boundaries: "address" is not "add", "prefix" is
        not "fix", and "debug" is not "bug".
        """
        title_lower = task.title.lower()
        full_text = (task.title + " " + task.description).lower()

        if _matches_keyword(title_lower, _GOAL_KEYWORDS):
            return "goal_decomposition"

        if task.task_type == TaskType.BUGFIX:
            return "bugfix_standard"
        if task.task_type in (TaskType.FEATURE, TaskType.REFACTOR):
            return "feature_standard"

        if _matches_keyword(full_text, _BUGFIX_KEYWORDS):
            return "bugfix_standard"

        if _matches_keyword(full_text, _FEATURE_KEYWORDS):
            return "feature_standard"

        # Default to bugfix
        return "bugfix_standard"

    async def _build_dossier(self, task: HenchmenTask, scheme_graph: SchemeGraph) -> Dossier:
        """Build dossier with repo file tree, task analysis, and relevant context."""
        from henchmen.dossier.task_analyzer import TaskAnalyzer

        dossier = Dossier(task_id=task.id)

        # Analyze the task to extract context clues
        analyzer = TaskAnalyzer()
        analysis = analyzer.analyze(task.title, task.description)
        logger.info(
            "[DOSSIER] Task analysis: type=%s, ci_related=%s, files=%s, errors=%s",
            analysis.task_type,
            analysis.ci_related,
            analysis.mentioned_files,
            analysis.mentioned_errors,
        )

        # Pre-fetch file tree from GitHub so the operative knows the codebase structure
        repo = task.context.repo
        github_token = self.settings.github_token
        base_branch = task.context.branch or "main"
        if repo and github_token:
            try:
                from github import Auth, Github

                g = Github(auth=Auth.Token(github_token))
                github_repo = g.get_repo(repo)
                tree = github_repo.get_git_tree(base_branch, recursive=True)
                file_paths = [item.path for item in tree.tree if item.type == "blob"]
                # Only store file paths for scoring in bootstrap — NOT dumped into context.
                # Was 200, reduced to 0 in dossier context (operative uses tools to explore).
                # Keep a small set for the file scoring algorithm in _build_file_context.
                dossier.relevant_files = file_paths[:50]
                logger.info("[DOSSIER] Fetched %d files from %s", len(file_paths), repo)
            except Exception as exc:
                logger.warning("[DOSSIER] Failed to fetch file tree: %s", exc)

            # If CI/test related, try to fetch latest failed workflow run
            if analysis.ci_related:
                try:
                    runs = github_repo.get_workflow_runs(status="failure")
                    for run in runs[:1]:  # type: ignore[var-annotated]  # Latest failed run
                        for job in run.jobs():
                            for step in job.steps:
                                if step.conclusion == "failure":
                                    dossier.related_issues.append(
                                        RelatedIssue(
                                            number=0,
                                            title=f"CI failure: {run.name}/{job.name}/{step.name}",
                                            url=run.html_url if hasattr(run, "html_url") else "",
                                            state=step.conclusion or "failure",
                                            labels=["ci_failure"],
                                        )
                                    )
                    logger.info("[DOSSIER] Fetched %d CI failure(s)", len(dossier.related_issues))
                except Exception as exc:
                    logger.warning("[DOSSIER] Failed to fetch CI failures (non-fatal): %s", exc)

            # Fetch specifically mentioned files
            if analysis.mentioned_files:
                for file_path in analysis.mentioned_files[:5]:
                    try:
                        contents = github_repo.get_contents(file_path)
                        if contents and hasattr(contents, "decoded_content"):
                            dossier.code_search_results.append(
                                CodeSearchResult(
                                    file_path=file_path,
                                    matches=[],
                                    context=contents.decoded_content.decode("utf-8")[:5000],
                                )
                            )
                    except Exception:
                        # File path might be partial or not found -- not fatal
                        pass
                if dossier.code_search_results:
                    logger.info(
                        "[DOSSIER] Pre-fetched %d mentioned file(s)",
                        len(dossier.code_search_results),
                    )

        # Store the task analysis in the dossier as a typed field
        dossier.task_analysis = analysis

        # Fetch semantically relevant code chunks from Vector Search
        semantic_chunks = await self._fetch_semantic_chunks(task)
        if semantic_chunks:
            dossier.semantic_code_chunks = semantic_chunks
            await self.tracker.record_rag_chunks(task.id, len(semantic_chunks))

        # Also try to build via DossierBuilder for rules/PRs
        try:
            builder = DossierBuilder(self.settings)
            for node in scheme_graph.definition.nodes:
                if node.node_type == NodeType.AGENTIC and node.dossier_requirement:
                    built = await builder.build(task, node.dossier_requirement)
                    # Merge into our dossier
                    if built.rule_files:
                        dossier.rule_files = built.rule_files
                    if built.related_prs:
                        dossier.related_prs = built.related_prs
                    break
        except Exception as exc:
            logger.warning("[DOSSIER] DossierBuilder failed (non-fatal): %s", exc)

        return dossier

    async def _rerank_semantic_chunks(self, task: HenchmenTask, chunks: list[Any]) -> list[Any]:
        """Rerank vector-search chunks with the LLM when enabled.

        Reranking is a quality improvement, never a dependency: when it is
        disabled, or the LLM provider cannot be built, the vector-search order
        is kept. ``rerank_semantic_chunks`` itself never raises on LLM failure.
        """
        if not self.settings.dossier_semantic_rerank:
            return chunks
        try:
            from henchmen.dossier.reranker import rerank_semantic_chunks

            llm_provider = self._get_llm_provider()
        except Exception as exc:
            logger.warning("[DOSSIER] Semantic rerank skipped: %s", exc)
            return chunks
        try:
            reranked: list[Any] = await rerank_semantic_chunks(
                chunks,
                f"{task.title}\n{task.description}",
                llm_provider,
                top_k=_RERANK_TOP_K,
                settings=self.settings,
            )
        except Exception as exc:
            logger.warning("[DOSSIER] Semantic rerank failed, keeping vector-search order: %s", exc)
            return chunks
        logger.info("[DOSSIER] Reranked %d semantic chunks down to %d", len(chunks), len(reranked))
        return reranked

    async def _fetch_semantic_chunks(self, task: HenchmenTask) -> list[Any]:
        """Query RAG Engine for semantically relevant code chunks.

        Uses GCP-native auth (service account credentials) — no API keys needed.
        Returns empty list on any failure (graceful degradation).
        """
        repo = task.context.repo
        if not repo:
            return []

        try:
            query_text = f"{task.title} {task.description}"
            chunks = await query_similar_chunks(
                query_text=query_text,
                repo=repo,
                collection_name=self.settings.rag_corpus_display_name,
                project_id=self.settings.gcp_project_id,
                region=self.settings.rag_corpus_region,
                top_k=20,
            )
        except Exception as exc:
            logger.warning("[DOSSIER] Semantic search failed (non-fatal): %s", exc)
            return []

        if not chunks:
            return []
        logger.info("[DOSSIER] Retrieved %d semantic chunks from RAG Engine", len(chunks))
        # Outside the search try-block: a rerank problem must not discard the
        # chunks retrieval already produced.
        return await self._rerank_semantic_chunks(task, chunks)
