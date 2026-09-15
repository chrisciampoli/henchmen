"""Cloud Run Job entrypoint for the Operative runtime.

Lifecycle: SPAWN → INITIALIZE → EXECUTE → REPORT → TERMINATE
"""

import asyncio
import contextlib
import logging
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Register schemes so the operative can look up its node
import henchmen.schemes.bugfix_standard  # noqa: F401
import henchmen.schemes.feature_standard  # noqa: F401
import henchmen.schemes.goal_decomposition  # noqa: F401
from henchmen.arsenal._repo import normalize_repo_slug
from henchmen.arsenal._workspace import DEFAULT_WORKSPACE_ROOT, set_workspace_root
from henchmen.config.posture import fail_open_allowed
from henchmen.config.settings import Settings, get_settings
from henchmen.models.llm import ModelTier
from henchmen.models.operative import OperativeConfig, OperativeReport, OperativeStatus
from henchmen.observability.tracker import TASK_EXECUTIONS_COLLECTION
from henchmen.operative.agent_builder import build_operative_agent
from henchmen.operative.git_helpers import (
    DEFAULT_BASE_BRANCH,
    detect_base_branch,
    detect_remote_default_branch,
    run_git,
)
from henchmen.providers.interfaces import MessageBroker, ObjectStore
from henchmen.providers.interfaces.document_store import DocumentStore
from henchmen.providers.registry import ProviderRegistry
from henchmen.providers.tiers import resolve_model_name
from henchmen.utils.git import build_clone_url, clone_repo
from henchmen.utils.redaction import install_secret_redaction, redact

logger = logging.getLogger(__name__)

#: Largest ``git_diff`` an operative puts in its report, in UTF-8 bytes. The
#: report travels to Mastermind in one request capped at
#: ``henchmen.dispatch.pubsub_auth.MAX_OPERATIVE_REPORT_BYTES``; nothing in
#: Mastermind reads the diff back (PRs, CI and fix nodes all work from the pushed
#: branch), so a huge change only loses the tail of an informational field.
MAX_REPORT_GIT_DIFF_BYTES = 512 * 1024
_GIT_DIFF_TRUNCATION_MARKER = "\n[henchmen: git diff truncated to {kept} of {total} bytes]\n"


def cap_report_git_diff(diff: str | None) -> str | None:
    """``diff`` unchanged when it fits :data:`MAX_REPORT_GIT_DIFF_BYTES`, else its head plus a truncation marker."""
    if diff is None:
        return None
    encoded = diff.encode("utf-8")
    if len(encoded) <= MAX_REPORT_GIT_DIFF_BYTES:
        return diff
    marker = _GIT_DIFF_TRUNCATION_MARKER.format(kept=MAX_REPORT_GIT_DIFF_BYTES, total=len(encoded))
    head = encoded[: MAX_REPORT_GIT_DIFF_BYTES - len(marker.encode("utf-8"))].decode("utf-8", errors="ignore")
    logger.warning("git diff of %d bytes truncated to %d bytes for the report", len(encoded), MAX_REPORT_GIT_DIFF_BYTES)
    return head + marker


class _RedactingFormatter(logging.Formatter):
    """Formatter that redacts secrets from the fully rendered line, traceback included.

    The shared record factory (:func:`install_secret_redaction`) covers the
    message and its ``%``-args; only the traceback appended by
    ``logger.exception`` is rendered later, so it is redacted here.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def install_log_redaction() -> None:
    """Install secret redaction for everything this process logs.

    A ``logging.Filter`` on the root *logger* never sees records emitted by
    ``henchmen.*`` module loggers (they propagate straight to the root
    *handlers*), which is why redaction is installed at the record factory and
    on each handler's formatter instead.
    """
    install_secret_redaction()
    for handler in logging.getLogger().handlers:
        existing = handler.formatter
        if not isinstance(existing, _RedactingFormatter):
            handler.setFormatter(
                _RedactingFormatter(
                    getattr(existing, "_fmt", None),
                    getattr(existing, "datefmt", None),
                )
            )


_HEARTBEAT_FAILURE_LOG_EVERY = 5


async def _heartbeat_loop(
    document_store: DocumentStore,
    task_id: str,
    interval_seconds: int,
) -> None:
    """Write ``last_heartbeat`` to the task document on a fixed cadence.

    Runs as a background task alongside the agent loop so the Mastermind
    watchdog can distinguish a live-but-slow operative from a dead one.
    Write failures are swallowed — observability must never crash the
    operative — but are not silent: the first failure and every
    ``_HEARTBEAT_FAILURE_LOG_EVERY``-th consecutive one after it are logged
    at WARNING (a single flaky write is expected noise; a heartbeat that
    cannot land at all is worth knowing about without flooding the log every
    ``interval_seconds``).

    The timestamp is an ISO-8601 UTC string, matching every other timestamp
    ``TaskTracker`` stores. ``get_stalled_tasks`` range-filters
    ``last_heartbeat < cutoff.isoformat()``; a native datetime is never matched
    by a string range on Firestore (so the task could never be found stalled)
    and raises on the SQLite store.
    """
    consecutive_failures = 0
    while True:
        try:
            await document_store.update(
                TASK_EXECUTIONS_COLLECTION,
                task_id,
                {"last_heartbeat": datetime.now(UTC).isoformat()},
            )
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            if consecutive_failures == 1 or consecutive_failures % _HEARTBEAT_FAILURE_LOG_EVERY == 0:
                logger.warning(
                    "Heartbeat write failed for task %s (non-fatal, %d consecutive): %s",
                    task_id,
                    consecutive_failures,
                    exc,
                )
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


async def _persist_interrupted_report(
    document_store: DocumentStore,
    report: OperativeReport,
) -> None:
    """Write a partial INTERRUPTED report to the cross-instance document store.

    Used when SIGTERM fires mid-node so the Mastermind pickup path has
    an authoritative record of the partial work (tokens, files touched)
    rather than fabricating a FAILED report with zero telemetry.
    """
    try:
        await document_store.update(
            TASK_EXECUTIONS_COLLECTION,
            report.task_id,
            {
                "interrupted_node_id": report.node_id,
                # ISO string, like every TaskTracker timestamp (see _heartbeat_loop).
                "interrupted_at": datetime.now(UTC).isoformat(),
                "interrupted_report": report.model_dump(mode="json"),
                "execution_state": "interrupted",
            },
        )
        logger.info("Persisted interrupted report for task %s node %s", report.task_id, report.node_id)
    except Exception as exc:
        logger.warning("Failed to persist interrupted report: %s", exc)


def _get_document_store(registry: ProviderRegistry, settings: Settings) -> DocumentStore | None:
    """Build the document store, failing closed outside dev.

    The store carries the heartbeat the Mastermind watchdog relies on and the
    task-level cost accumulator. Running without it in staging/prod would let
    a node spend past the task ceiling and look dead to the watchdog, so there
    the Job fails instead. Dev on a repository checkout keeps the
    warn-and-continue path; an operative launched by a desktop install never does.
    """
    try:
        return registry.get_document_store()
    except Exception as exc:
        if fail_open_allowed(settings):
            logger.warning("Document store unavailable (heartbeat/accumulator disabled in dev): %s", exc)
            return None
        logger.error(
            "Document store unavailable in %s — refusing to run without heartbeats and the task cost ceiling: %s",
            settings.environment.value,
            exc,
        )
        raise RuntimeError(f"Document store unavailable in {settings.environment.value}: {exc}") from exc


async def run_operative() -> None:
    """Main operative lifecycle: SPAWN → INITIALIZE → EXECUTE → REPORT → TERMINATE"""
    settings = get_settings()

    # Initialize distributed tracing
    from henchmen.observability.tracing import init_tracing

    init_tracing("operative", project_id=settings.gcp_project_id)

    # Create providers via registry — no direct GCP SDK calls below this point
    registry = ProviderRegistry(settings)
    broker = registry.get_message_broker()
    object_store = registry.get_object_store()
    llm_provider = registry.get_llm_provider()
    document_store = _get_document_store(registry, settings)

    # 1. Read config from environment. MODEL_NAME may be a tier ("default/complex");
    # resolve it once here so telemetry, the report and the cost gate all carry the
    # concrete model the provider will actually bill.
    raw_model_name = os.environ.get("MODEL_NAME") or ModelTier.COMPLEX.value
    config = OperativeConfig(
        task_id=os.environ["TASK_ID"],
        node_id=os.environ["NODE_ID"],
        scheme_id=os.environ["SCHEME_ID"],
        model_name=resolve_model_name(settings, raw_model_name),
    )
    # LAIR_ID is what the Mastermind uses for the fallback report, so prefer it
    # over a synthetic id — one execution must not have two identities.
    operative_id = (
        os.environ.get("OPERATIVE_ID") or os.environ.get("LAIR_ID") or f"op-{config.task_id}-{config.node_id}"
    )

    started_at = datetime.now(UTC)

    # ------------------------------------------------------------------
    # K5 fix: install SIGTERM handler for graceful shutdown on Cloud Run
    # eviction. Cloud Run Jobs send SIGTERM followed by SIGKILL after 10s,
    # so we need to exit the agent loop cleanly before the kill fires.
    # ------------------------------------------------------------------
    shutdown_event = asyncio.Event()

    def _sigterm_handler() -> None:
        logger.warning("[operative] SIGTERM received, initiating graceful shutdown")
        shutdown_event.set()

    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler — skip gracefully
        # (matches the pattern used in mastermind/server.py lifespan).
        logger.debug("add_signal_handler not supported on this platform; SIGTERM handler disabled")

    # 2. INITIALIZE: Clone repo, download dossier, set up workspace.
    # A clone/checkout failure must still produce a report — otherwise the
    # Mastermind only learns "the job failed" with no root cause.
    try:
        workspace_dir = await initialize_workspace(config, settings, object_store=object_store)

        # 2b. Pre-read files into context so the operative can skip searching
        file_context = await _build_file_context(
            workspace_dir,
            os.environ.get("TASK_TITLE", ""),
            os.environ.get("TASK_DESCRIPTION", ""),
        )
        # Write to a file instead of env var to avoid "Argument list too long" errors
        file_context_path = os.path.join(workspace_dir, ".henchmen_file_context.txt")
        with open(file_context_path, "w", encoding="utf-8") as fh:
            fh.write(file_context)
        os.environ["FILE_CONTEXT_PATH"] = file_context_path
    except Exception as exc:
        logger.exception("Workspace initialisation failed")
        failure_report = OperativeReport(
            task_id=config.task_id,
            scheme_id=config.scheme_id,
            node_id=config.node_id,
            operative_id=operative_id,
            status=OperativeStatus.FAILED,
            summary=f"Workspace initialisation failed: {exc}",
            confidence_score=0.0,
            error=str(exc),
            started_at=started_at,
            completed_at=datetime.now(UTC),
            model_name=config.model_name,
            wall_clock_seconds=(datetime.now(UTC) - started_at).total_seconds(),
        )
        await publish_report(failure_report, settings, broker=broker)
        raise

    # ------------------------------------------------------------------
    # Start the heartbeat task. We hold a strong reference so the event
    # loop can't GC it mid-flight, and cancel it explicitly when the
    # agent loop exits.
    # ------------------------------------------------------------------
    heartbeat_task: asyncio.Task[None] | None = None
    if document_store is not None:
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(
                document_store,
                config.task_id,
                settings.operative_heartbeat_interval_seconds,
            ),
            name=f"heartbeat-{config.task_id[:8]}",
        )

        def _log_heartbeat_exit(t: asyncio.Task[None]) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.warning("Heartbeat task exited with exception: %s", exc)

        heartbeat_task.add_done_callback(_log_heartbeat_exit)

    # 3. EXECUTE: Build and run the agent
    interrupted = False
    agent = None
    try:
        agent = await build_operative_agent(
            config,
            workspace_dir,
            settings,
            llm_provider=llm_provider,
            document_store=document_store,
            shutdown_event=shutdown_event,
        )
        result = await agent.run()  # Returns dict with git_diff, summary, files_changed, confidence

        # Always check for changes — even if agent didn't report them
        # (the model may have edited files without calling git_commit)
        branch_name = config.branch_name
        has_changes = await _check_for_changes(workspace_dir)
        if has_changes:
            await _create_branch_and_push(workspace_dir, branch_name, settings)
            result["branch_pushed"] = branch_name
            logger.info("Pushed branch %s", branch_name)
        else:
            logger.info("No changes detected in workspace — skipping branch push")

        # Interrupt takes precedence over blocked/completed: the agent exited
        # early on SIGTERM, so we cannot claim the work is finished.
        if result.get("interrupted"):
            interrupted = True
            status = OperativeStatus.INTERRUPTED
            result["error"] = result.get("error") or "SIGTERM received during node execution"
            result["summary"] = result.get("summary") or "Operative interrupted by SIGTERM — partial work preserved"
        elif result.get("blocked"):
            status = OperativeStatus.BLOCKED
        else:
            status = OperativeStatus.COMPLETED
    except TimeoutError:
        # Timed-out nodes are usually the expensive ones — keep their telemetry
        # so the task-level cost gate is not under-counted.
        result = {
            "summary": "Operative timed out but may have made changes",
            "error": "Timeout",
            "telemetry": agent.get_telemetry() if agent is not None else {},
        }
        status = OperativeStatus.TIMED_OUT
        # Still try to push any changes made before timeout
        try:
            branch_name = config.branch_name
            has_changes = await _check_for_changes(workspace_dir)
            if has_changes:
                await _create_branch_and_push(workspace_dir, branch_name, settings)
                result["branch_pushed"] = branch_name
                logger.info("Pushed changes despite timeout (status remains TIMED_OUT)")
        except Exception:
            logger.warning("Could not push changes after timeout", exc_info=True)
    except Exception as e:
        logger.exception("Operative execution failed")
        # If SIGTERM had already fired, classify this as INTERRUPTED rather
        # than FAILED — a raised exception during graceful shutdown is
        # expected (e.g. an in-flight HTTP call cancelled by the shutdown).
        if shutdown_event.is_set():
            interrupted = True
            status = OperativeStatus.INTERRUPTED
            result = {
                "summary": "Operative interrupted by SIGTERM — partial work preserved",
                "error": f"SIGTERM received during node execution: {e}",
            }
        else:
            result = {"summary": f"Operative failed: {str(e)}", "error": str(e)}
            status = OperativeStatus.FAILED
        result["telemetry"] = agent.get_telemetry() if agent is not None else {}
        # Still try to push any changes made before failure
        try:
            branch_name = config.branch_name
            has_changes = await _check_for_changes(workspace_dir)
            if has_changes:
                await _create_branch_and_push(workspace_dir, branch_name, settings)
                result["branch_pushed"] = branch_name
                logger.info("Pushed changes despite error")
        except Exception:
            logger.warning("Could not push changes after failure", exc_info=True)
    finally:
        # Cancel the heartbeat before we publish — we don't want a post-report
        # heartbeat write racing with TaskTracker.finalize_task.
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task

    # 4. REPORT: Publish result
    completed_at = datetime.now(UTC)
    telemetry = result.get("telemetry", {})
    wall_clock_seconds = (completed_at - started_at).total_seconds()
    report = OperativeReport(
        task_id=config.task_id,
        scheme_id=config.scheme_id,
        node_id=config.node_id,
        operative_id=operative_id,
        status=status,
        git_diff=cap_report_git_diff(result.get("git_diff")),
        summary=result.get("summary", ""),
        confidence_score=result.get("confidence", 0.5),
        files_changed=result.get("files_changed", []),
        error=result.get("error"),
        block_reason=result.get("block_reason"),
        started_at=started_at,
        completed_at=completed_at,
        # Telemetry carries the concrete model the provider reported; fall back
        # to the resolved config value when the agent never got to run.
        model_name=telemetry.get("model_name") or config.model_name,
        total_input_tokens=telemetry.get("total_input_tokens", 0),
        total_output_tokens=telemetry.get("total_output_tokens", 0),
        cached_input_tokens=telemetry.get("cached_input_tokens", 0),
        # The guardrails sum the provider-billed cost per call; the tracker persists this figure.
        estimated_cost_usd=telemetry.get("estimated_cost_usd", 0.0),
        model_calls=telemetry.get("model_calls", 0),
        tool_calls_count=telemetry.get("tool_calls_count", 0),
        tool_calls_detail=telemetry.get("tool_calls_detail", {}),
        wall_clock_seconds=wall_clock_seconds,
        steps_used=telemetry.get("steps_used", 0),
        context_tokens_at_start=telemetry.get("context_tokens_at_start", 0),
        context_tokens_at_end=telemetry.get("context_tokens_at_end", 0),
    )

    # Persist a partial INTERRUPTED record to the cross-instance document
    # store BEFORE publishing so the Mastermind pickup path has authoritative
    # state even if the broker publish is killed by SIGKILL.
    if interrupted and document_store is not None:
        await _persist_interrupted_report(document_store, report)

    await publish_report(report, settings, broker=broker)


_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".tox", ".mypy_cache", ".pytest_cache"}
_MAX_FILES = 5
_MAX_FILE_CHARS = 4000
_MAX_CONTEXT_TOKENS = 20_000  # Hard cap on total context tokens sent to model
_MAX_CONTEXT_CHARS = _MAX_CONTEXT_TOKENS * 4  # Char equivalent for fast truncation
_TOP_LEVEL_FILES = {"README.md", "package.json", "pyproject.toml", "setup.py", "Makefile", "Cargo.toml", "go.mod"}


def _load_task_analysis_from_dossier(workspace_dir: str) -> Any:
    """Read TaskAnalysis from the dossier JSON if available."""
    import json

    from henchmen.dossier.task_analyzer import TaskAnalysis

    dossier_path = os.path.join(workspace_dir, ".henchmen", "dossier", "dossier.json")
    if not os.path.exists(dossier_path):
        return None
    try:
        with open(dossier_path, encoding="utf-8") as fh:
            data = json.load(fh)
        ta = data.get("task_analysis")
        if isinstance(ta, dict):
            return TaskAnalysis(**ta)
    except Exception:
        pass
    return None


def _load_semantic_file_paths_from_dossier(workspace_dir: str) -> set[str]:
    """Read file paths from semantic_code_chunks in the dossier JSON if available."""
    import json

    dossier_path = os.path.join(workspace_dir, ".henchmen", "dossier", "dossier.json")
    if not os.path.exists(dossier_path):
        return set()
    try:
        with open(dossier_path, encoding="utf-8") as fh:
            data = json.load(fh)
        chunks = data.get("semantic_code_chunks", [])
        return {c["file_path"].lower() for c in chunks if isinstance(c, dict) and "file_path" in c}
    except Exception:
        return set()


async def _build_file_context(workspace_dir: str, task_title: str, task_description: str) -> str:
    """Walk the workspace, pick the most relevant files, and return their contents as context.

    Uses TaskAnalyzer results (from the dossier) when available to boost scores
    for mentioned files and keyword-matching files. File selection is delegated
    to ``FileScorer`` which uses a context-window budget instead of a hard file
    count limit.
    """
    from henchmen.dossier.file_scorer import FileScorer
    from henchmen.dossier.task_analyzer import TaskAnalyzer

    # 0. Try to read task analysis from dossier (avoids re-running analyzer)
    analysis = _load_task_analysis_from_dossier(workspace_dir)
    if analysis is None:
        analyzer = TaskAnalyzer()
        analysis = analyzer.analyze(task_title, task_description)

    # Build a set of mentioned file basenames for fast lookup
    analysis_mentioned_lower = {f.lower() for f in analysis.mentioned_files}
    analysis_keywords = set(analysis.keywords)

    # Load file paths from semantic search results for score boosting
    rag_file_paths = _load_semantic_file_paths_from_dossier(workspace_dir)

    # 1. Collect all files, skipping noisy directories
    all_files: list[str] = []
    workspace = Path(workspace_dir)
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            rel = os.path.relpath(os.path.join(root, fname), workspace_dir)
            rel = rel.replace("\\", "/")
            all_files.append(rel)

    all_files.sort()

    # 2. Score files using FileScorer (replaces inline scoring logic)
    scorer = FileScorer()
    scored = scorer.score_files(
        all_files=all_files,
        task_title=task_title,
        task_description=task_description,
        mentioned_files=analysis_mentioned_lower,
        rag_file_paths=rag_file_paths,
        analysis_keywords=analysis_keywords,
        max_context_chars=_MAX_CONTEXT_CHARS,
    )

    # For test_fix tasks, boost test files (task-type-specific logic stays here)
    if analysis.task_type == "test_fix":
        boosted: list[tuple[float, str]] = []
        for score, rel in scored:
            basename = os.path.basename(rel).lower()
            if "test" in basename or "spec" in basename or "test" in rel.lower():
                score += 8
            boosted.append((score, rel))
        boosted.sort(key=lambda t: (-t[0], t[1]))
        scored = boosted

    selected = [rel for _score, rel in scored]

    # 3. Read file contents
    file_sections: list[str] = []
    for rel in selected:
        full_path = os.path.join(workspace_dir, rel)
        try:
            with open(full_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read(_MAX_FILE_CHARS)
            if len(content) == _MAX_FILE_CHARS:
                content += "\n... [truncated]"
            file_sections.append(f"### {rel}\n```\n{content}\n```")
        except Exception:
            file_sections.append(f"### {rel}\n(could not read)")

    # 4. Build output — NO full file tree dump (was 2,461 paths = 30K tokens of waste).
    # Only include pre-read file contents. The operative has grep_search/file_read tools
    # to discover files it needs — dumping the entire tree just bloats context.
    parts: list[str] = []
    if file_sections:
        parts.append("## Pre-Read Files (most relevant to your task)\n")
        parts.append("\n\n".join(file_sections))

    context = "\n\n".join(parts)

    # Hard cap to prevent context explosion
    if len(context) > _MAX_CONTEXT_CHARS:
        context = context[:_MAX_CONTEXT_CHARS] + "\n\n... [context truncated to save tokens]"

    logger.info(
        "Built file context: %d total files, %d pre-read, %d chars (task_type=%s)",
        len(all_files),
        len(selected),
        len(context),
        analysis.task_type,
    )
    return context


async def initialize_workspace(
    config: OperativeConfig, settings: Settings, object_store: ObjectStore | None = None
) -> str:
    """Clone the repo, create the task branch, install dependencies, download the dossier."""
    workspace = f"{DEFAULT_WORKSPACE_ROOT}/{config.task_id}"
    os.makedirs(workspace, exist_ok=True)

    # Arsenal resolves every tool path against WORKSPACE_DIR. Without this the
    # root stays /workspace (the parent), so git_commit without an explicit
    # working_dir runs outside the repository and can never succeed.
    os.environ["WORKSPACE_DIR"] = workspace
    set_workspace_root(workspace)

    repo_url = os.environ.get("REPO_URL", "")
    branch = os.environ.get("BRANCH", "") or DEFAULT_BASE_BRANCH

    # Always clone. The snapshot cache used to be consulted here, but it was
    # keyed without a commit SHA, nothing ever saved a snapshot, and a restore
    # was never followed by a fetch — so it could never hit, and a hit would
    # have handed the agent a stale tree.
    if repo_url:
        # Normalize repo_url to "owner/repo" form expected by clone_repo. The
        # token comes from Settings, which accepts both HENCHMEN_GITHUB_TOKEN and
        # the bare GITHUB_TOKEN a secret mount injects (and reads .env files).
        github_token = settings.github_token
        repo_slug = normalize_repo_slug(repo_url) or repo_url

        # Use deeper clone for feature branches so we have origin/main for diffing
        depth = 50 if branch.startswith("henchmen/") else 1
        logger.info("Cloning repo %s (branch: %s, depth: %s)", repo_url, branch, depth)
        try:
            await clone_repo(
                repo_slug,
                branch,
                workspace,
                token=github_token or None,
                depth=depth,
                single_branch=False,
            )
        except RuntimeError:
            # Branch doesn't exist on remote — fall back to the repository's
            # own default branch (which is not necessarily "main"). The
            # operative creates its henchmen/<task_id> branch from there anyway.
            default_branch = await detect_remote_default_branch(
                build_clone_url(repo_slug, github_token or None),
                token=github_token or None,
            )
            if default_branch == branch:
                raise
            logger.warning(
                "Branch %s not found on remote, falling back to default branch %s",
                branch,
                default_branch,
            )
            await clone_repo(
                repo_slug,
                default_branch,
                workspace,
                token=github_token or None,
                depth=depth,
                single_branch=False,
            )
    else:
        logger.warning("No REPO_URL set; workspace will be empty")

    # Configure git identity so the agent's git_commit tool works.
    # Use --global so it works regardless of cwd, and await communicate() to ensure completion.
    git_email = settings.git_author_email
    git_name = settings.git_author_name
    for config_args in [
        ["git", "config", "--global", "user.email", git_email],
        ["git", "config", "--global", "user.name", git_name],
    ]:
        proc = await asyncio.create_subprocess_exec(
            *config_args,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    # Keep henchmen scratch files out of the target repo's tree WITHOUT editing
    # its tracked .gitignore (that diff would land in every PR).
    _write_git_exclusions(workspace)

    # Create the henchmen feature branch so agent commits land on a branch, not main
    branch_name = config.branch_name
    proc = await asyncio.create_subprocess_exec(
        "git",
        "checkout",
        "-b",
        branch_name,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    if proc.returncode == 0:
        logger.info("Created branch %s", branch_name)
    else:
        logger.warning("Failed to create branch %s, continuing on current branch", branch_name)

    # Fetch the repository's default branch so scoped lint/tests can diff against it
    base_branch = await detect_base_branch(workspace)
    _, stderr_text, rc = await run_git(workspace, "fetch", "origin", f"{base_branch}:refs/remotes/origin/{base_branch}")
    if rc != 0:
        logger.error("Could not fetch base branch %s: %s", base_branch, stderr_text[:300])

    # Install project dependencies so run_tests / run_lint / type_check work.
    await _install_project_dependencies(workspace)

    # Download dossier artifact if available
    dossier_uri = os.environ.get("DOSSIER_URI")
    if dossier_uri:
        await download_dossier(dossier_uri, workspace, object_store=object_store)

    return workspace


def _write_git_exclusions(workspace: str) -> None:
    """Add henchmen scratch paths to ``.git/info/exclude`` (never the tracked .gitignore)."""
    exclude_path = os.path.join(workspace, ".git", "info", "exclude")
    entries = [".henchmen_file_context.txt", ".henchmen/", "node_modules/"]
    try:
        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        existing = ""
        if os.path.exists(exclude_path):
            with open(exclude_path, encoding="utf-8") as fh:
                existing = fh.read()
        missing = [entry for entry in entries if entry not in existing]
        if missing:
            with open(exclude_path, "a", encoding="utf-8") as fh:
                fh.write("\n# Added by Henchmen operative (local only, never committed)\n")
                fh.write("\n".join(missing) + "\n")
    except OSError as exc:
        logger.warning("Could not write git exclusions: %s", exc)


async def _install_project_dependencies(workspace: str) -> None:
    """Install target-repo dependencies (Node and Python). Failures are non-fatal."""
    package_json = os.path.join(workspace, "package.json")
    if os.path.exists(package_json):
        pnpm_lock = os.path.join(workspace, "pnpm-lock.yaml")
        install_cmd = ["pnpm", "install", "--frozen-lockfile"] if os.path.exists(pnpm_lock) else ["npm", "ci"]
        logger.info("Installing Node.js dependencies with %s", install_cmd[0])
        proc = await asyncio.create_subprocess_exec(
            *install_cmd,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.info("Node.js dependencies installed successfully")
        else:
            logger.warning("Node.js dependency install failed (non-fatal): %s", stderr.decode()[:500])

    # Python projects: the image ships pytest/ruff/mypy, but the target repo's
    # own dependencies still have to be installed for its tests to import.
    requirements = [
        name for name in ("requirements.txt", "requirements-dev.txt") if os.path.exists(os.path.join(workspace, name))
    ]
    pip_commands: list[list[str]] = [["pip", "install", "--user", "-r", name] for name in requirements]
    if os.path.exists(os.path.join(workspace, "pyproject.toml")) or os.path.exists(os.path.join(workspace, "setup.py")):
        pip_commands.append(["pip", "install", "--user", "-e", "."])

    for cmd in pip_commands:
        logger.info("Installing Python dependencies: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("Python dependency install failed (non-fatal): %s", stderr.decode()[:500])


async def _check_for_changes(workspace_dir: str) -> bool:
    """Check if the workspace has any uncommitted or committed-but-not-pushed changes."""
    try:
        # Check for uncommitted changes (modified, new, deleted files)
        porcelain, _, _ = await run_git(workspace_dir, "status", "--porcelain")
        if porcelain.strip():
            logger.info("[OPERATIVE] Uncommitted changes detected")
            return True

        # Check if the current branch has commits ahead of the base ref
        # (the agent committed on the branch). The base branch is NOT always
        # "main" — a wrong ref made rev-list fail and silently discarded work.
        base_branch = await detect_base_branch(workspace_dir)
        count, stderr_text, rc = await run_git(workspace_dir, "rev-list", "--count", f"origin/{base_branch}..HEAD")
        if rc != 0:
            logger.error("Could not compare against origin/%s: %s", base_branch, stderr_text[:300])
            return False
        if count.isdigit() and int(count) > 0:
            logger.info("[OPERATIVE] Branch has %s commit(s) ahead of origin/%s", count, base_branch)
            return True

        return False
    except Exception as exc:
        logger.warning("Could not check for changes: %s", exc)
        return False


async def _create_branch_and_push(workspace_dir: str, branch_name: str, settings: Settings) -> None:
    """Create a git branch, commit any uncommitted changes, and push to origin."""

    async def _git(*args: str) -> tuple[str, str, int]:
        return await run_git(workspace_dir, *args)

    # Configure git user for commits from Settings (the same source
    # ``initialize_workspace`` used for the --global config).
    await _git("config", "user.email", settings.git_author_email)
    await _git("config", "user.name", settings.git_author_name)

    # Henchmen scratch files are excluded via .git/info/exclude in
    # initialize_workspace — never by editing the repo's tracked .gitignore.

    # Create and checkout branch
    _out, _err, rc = await _git("checkout", "-b", branch_name)
    if rc != 0:
        # Branch might already exist
        await _git("checkout", branch_name)

    # Stage any uncommitted changes and commit if needed
    await _git("add", "-A")
    out, _, _ = await _git("diff", "--cached", "--name-only")
    if out.strip():
        commit_msg = f"fix: automated changes by Henchmen operative\n\nBranch: {branch_name}"
        _, err, rc = await _git("commit", "-m", commit_msg)
        if rc != 0:
            logger.error("[OPERATIVE] git commit failed: %s", err)
    else:
        logger.info("[OPERATIVE] No staged changes to commit (agent already committed)")

    # Always push — even if we didn't commit, the agent's earlier commits need to be pushed
    out, err, rc = await _git("push", "-u", "origin", branch_name)
    if rc != 0:
        logger.error("[OPERATIVE] git push failed: %s", err)
        raise RuntimeError(f"git push failed: {err}")

    logger.info("[OPERATIVE] Pushed branch %s to origin", branch_name)


async def download_dossier(uri: str, workspace: str, object_store: ObjectStore | None = None) -> None:
    """Download dossier artifact from object storage to workspace/.henchmen/dossier/

    Accepts ``gs://bucket/key``, ``s3://bucket/key`` and provider-relative
    ``bucket/key`` (what the filesystem object store used in local mode emits).
    The object_store provider performs the download; if no provider is supplied
    a direct GCS fallback is used so callers that don't yet pass one still work.
    """
    dest_dir = os.path.join(workspace, ".henchmen", "dossier")
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, "dossier.json")

    # Parse the URI into bucket + key regardless of scheme
    without_prefix = uri
    for scheme in ("gs://", "s3://", "file://"):
        if uri.startswith(scheme):
            without_prefix = uri[len(scheme) :]
            break
    else:
        if "://" in uri:
            logger.warning("Unsupported dossier URI scheme: %s", uri)
            return

    parts = without_prefix.split("/", 1)
    if len(parts) != 2:
        logger.warning("Could not parse object storage URI: %s", uri)
        return

    bucket_name, blob_name = parts

    if object_store is not None:
        await object_store.get_file(bucket_name, blob_name, dest_path)
    else:
        # Fallback: direct GCS download (legacy path, avoid in new code)
        from google.cloud import storage

        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.download_to_filename(dest_path)

    logger.info("Downloaded dossier from %s → %s", uri, dest_path)


async def publish_report(report: OperativeReport, settings: Settings, broker: MessageBroker | None = None) -> None:
    """Publish operative report to the message broker.

    Uses the provided MessageBroker provider when available. Falls back to a
    direct Pub/Sub call if no broker is supplied so legacy callers continue to work.

    On a desktop install the broker is a separate-process ``InMemoryMessageBroker``
    forwarding to the host over HTTP: its ``publish`` schedules that forward as a
    background task and returns immediately, which is correct for the shared
    server-side broker but fatal here — this process exits right after this
    call returns, cancelling the in-flight forward before it ever reaches
    Mastermind (ruling P8). So when the broker is that kind and has a forward
    URL configured for this topic, delivery is awaited directly via
    ``publish_and_confirm`` and a failed delivery raises, taking this operative
    down non-zero rather than silently losing the report. Every other broker
    (GCP, AWS, or an in-memory broker with nothing to forward to) keeps its
    existing non-blocking-from-the-caller's-perspective path.
    """
    data = report.model_dump_json().encode("utf-8")
    topic = settings.pubsub_topic_operative_complete

    if broker is not None:
        from henchmen.providers.local.memory import InMemoryMessageBroker

        if isinstance(broker, InMemoryMessageBroker) and broker.has_forward_target(topic):
            delivered = await broker.publish_and_confirm(topic, data, task_id=report.task_id)
            if not delivered:
                # No token or secret is interpolated here — only the task id, which is not sensitive.
                logger.error(
                    "Failed to deliver operative report for task %s (status=%s) — exiting non-zero",
                    report.task_id,
                    report.status,
                )
                raise RuntimeError(f"Failed to deliver operative completion report for task {report.task_id}")
        else:
            await broker.publish(topic, data, task_id=report.task_id)
    else:
        # Fallback: direct Pub/Sub publish (legacy path, avoid in new code)
        from google.cloud import pubsub_v1  # type: ignore[attr-defined]

        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(settings.gcp_project_id, topic)
        future = publisher.publish(topic_path, data=data, task_id=report.task_id)
        future.result()  # Block until published

    logger.info("Published operative report for task %s (status=%s)", report.task_id, report.status)


def main() -> None:
    """Entrypoint for the container.

    Fail-closed (ruling P8): any exception escaping ``run_operative`` --
    including an undeliverable completion report -- is logged at ERROR (never
    with a token; ``install_log_redaction`` also covers this line) and turned
    into a non-zero exit, rather than letting the process exit 0 having lost
    its report.
    """
    logging.basicConfig(level=logging.INFO)
    install_log_redaction()
    try:
        asyncio.run(run_operative())
    except Exception as exc:
        logger.error("[operative] Fatal error, exiting non-zero: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
