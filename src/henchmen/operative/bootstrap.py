"""Cloud Run Job entrypoint for the Operative runtime.

Lifecycle: SPAWN → INITIALIZE → EXECUTE → REPORT → TERMINATE
"""

import asyncio
import contextlib
import logging
import os
import re
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Register schemes so the operative can look up its node
import henchmen.schemes.bugfix_standard  # noqa: F401
import henchmen.schemes.feature_standard  # noqa: F401
import henchmen.schemes.goal_decomposition  # noqa: F401
from henchmen.config.settings import Settings, get_settings
from henchmen.models.operative import OperativeConfig, OperativeReport, OperativeStatus
from henchmen.operative.agent_builder import build_operative_agent
from henchmen.providers.interfaces import MessageBroker, ObjectStore
from henchmen.providers.interfaces.document_store import DocumentStore
from henchmen.providers.registry import ProviderRegistry
from henchmen.utils.git import clone_repo

logger = logging.getLogger(__name__)

# Firestore collection written by the heartbeat and partial report paths.
# Kept aligned with ``henchmen.observability.tracker._COLLECTION``.
_TASK_EXECUTIONS_COLLECTION = "task_executions"


class _SecretRedactionFilter(logging.Filter):
    """Logging filter that redacts known secret token patterns before they reach Cloud Logging."""

    _PATTERNS = [
        re.compile(r"(ghp_[a-zA-Z0-9]{36})"),  # GitHub personal access tokens
        re.compile(r"(ghs_[a-zA-Z0-9]{36})"),  # GitHub server-to-server tokens
        re.compile(r"(xoxb-[a-zA-Z0-9-]+)"),  # Slack bot tokens
        re.compile(r"(sk-[a-zA-Z0-9]{32,})"),  # OpenAI / generic secret keys
        re.compile(r"(x-access-token:[^@\s]+)"),  # Git clone credential URLs
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.msg)
        for pattern in self._PATTERNS:
            msg = pattern.sub("***REDACTED***", msg)
        record.msg = msg
        return True


async def _heartbeat_loop(
    document_store: DocumentStore,
    task_id: str,
    interval_seconds: int,
) -> None:
    """Write ``last_heartbeat`` to the task document on a fixed cadence.

    Runs as a background task alongside the agent loop so the Mastermind
    watchdog can distinguish a live-but-slow operative from a dead one.
    Any Firestore write failures are swallowed — observability must never
    crash the operative.
    """
    while True:
        try:
            await document_store.update(
                _TASK_EXECUTIONS_COLLECTION,
                task_id,
                {"last_heartbeat": datetime.now(UTC)},
            )
        except Exception as exc:
            logger.debug("Heartbeat write failed (non-fatal): %s", exc)
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
            _TASK_EXECUTIONS_COLLECTION,
            report.task_id,
            {
                "interrupted_node_id": report.node_id,
                "interrupted_at": datetime.now(UTC),
                "interrupted_report": report.model_dump(mode="json"),
                "execution_state": "interrupted",
            },
        )
        logger.info("Persisted interrupted report for task %s node %s", report.task_id, report.node_id)
    except Exception as exc:
        logger.warning("Failed to persist interrupted report: %s", exc)


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
    document_store: DocumentStore | None
    try:
        document_store = registry.get_document_store()
    except Exception as exc:
        logger.warning("Document store unavailable (heartbeat/accumulator disabled): %s", exc)
        document_store = None

    # 1. Read config from environment
    config = OperativeConfig(
        task_id=os.environ["TASK_ID"],
        node_id=os.environ["NODE_ID"],
        scheme_id=os.environ["SCHEME_ID"],
        model_name=os.environ.get("MODEL_NAME", settings.vertex_ai_model_complex),
    )
    operative_id = os.environ.get("OPERATIVE_ID", f"op-{config.task_id}-{config.node_id}")

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

    # 2. INITIALIZE: Clone repo, download dossier, set up workspace
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
                logger.debug("Heartbeat task exited with exception: %s", exc)

        heartbeat_task.add_done_callback(_log_heartbeat_exit)

    # 3. EXECUTE: Build and run the agent
    interrupted = False
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
        # (Gemini may have edited files without calling git_commit)
        branch_name = config.branch_name
        has_changes = await _check_for_changes(workspace_dir)
        if has_changes:
            await _create_branch_and_push(workspace_dir, branch_name)
            result["branch_pushed"] = branch_name
            result["files_changed"] = result.get("files_changed") or [branch_name]
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
        result = {"summary": "Operative timed out but may have made changes", "error": "Timeout"}
        status = OperativeStatus.TIMED_OUT
        # Still try to push any changes made before timeout
        try:
            branch_name = config.branch_name
            has_changes = await _check_for_changes(workspace_dir)
            if has_changes:
                await _create_branch_and_push(workspace_dir, branch_name)
                result["branch_pushed"] = branch_name
                logger.info("Pushed changes despite timeout (status remains TIMED_OUT)")
        except Exception:
            pass
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
        # Still try to push any changes made before failure
        try:
            branch_name = config.branch_name
            has_changes = await _check_for_changes(workspace_dir)
            if has_changes:
                await _create_branch_and_push(workspace_dir, branch_name)
                result["branch_pushed"] = branch_name
                logger.info("Pushed changes despite error")
        except Exception:
            pass
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
        git_diff=result.get("git_diff"),
        summary=result.get("summary", ""),
        confidence_score=result.get("confidence", 0.5),
        files_changed=result.get("files_changed", []),
        error=result.get("error"),
        block_reason=result.get("block_reason"),
        started_at=started_at,
        completed_at=completed_at,
        model_name=config.model_name,
        total_input_tokens=telemetry.get("total_input_tokens", 0),
        total_output_tokens=telemetry.get("total_output_tokens", 0),
        cached_input_tokens=telemetry.get("cached_input_tokens", 0),
        model_calls=telemetry.get("model_calls", 0),
        tool_calls_count=telemetry.get("tool_calls_count", 0),
        tool_calls_detail=telemetry.get("tool_calls_detail", {}),
        wall_clock_seconds=wall_clock_seconds,
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
    for mentioned files and keyword-matching files.
    """
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

    # 2. Score files for relevance
    combined_text = f"{task_title} {task_description}".lower()
    # Extract potential file names / stems from task text
    mentioned_patterns: list[str] = re.findall(r"[\w\-]+\.[\w]+", combined_text)
    keywords = set(re.findall(r"[a-z]{3,}", combined_text))

    scored: list[tuple[float, str]] = []
    for rel in all_files:
        score = 0.0
        basename = os.path.basename(rel).lower()
        rel_lower = rel.lower()

        # HIGH PRIORITY: Exact match with files mentioned in task analysis
        if basename in analysis_mentioned_lower or rel_lower in analysis_mentioned_lower:
            score += 50
        # Partial match with analysis mentioned files
        for mentioned in analysis_mentioned_lower:
            if mentioned in rel_lower:
                score += 25
                break

        # Exact file name match from task text
        for pat in mentioned_patterns:
            if pat.lower() == basename:
                score += 10
            elif pat.lower() in rel_lower:
                score += 5

        # Top-level config / readme
        if os.path.basename(rel) in _TOP_LEVEL_FILES and "/" not in rel:
            score += 3

        # README.md anywhere
        if basename == "readme.md":
            score += 4

        # Keyword overlap with path components (both original and analysis keywords)
        path_parts = set(re.findall(r"[a-z]{3,}", rel_lower))
        overlap = keywords & path_parts
        score += len(overlap) * 0.5
        # Analysis keywords get additional weight
        analysis_overlap = analysis_keywords & path_parts
        score += len(analysis_overlap) * 1.0

        # Boost files that appear in RAG semantic search results
        if rel_lower in rag_file_paths or basename in rag_file_paths:
            score += 30

        # Boost files in the same directory as mentioned files
        rel_dir = os.path.dirname(rel_lower)
        if rel_dir:
            for mentioned in analysis_mentioned_lower:
                mentioned_dir = os.path.dirname(mentioned.lower())
                if mentioned_dir and rel_dir == mentioned_dir:
                    score += 15
                    break

        # For test_fix tasks, boost test files
        if analysis.task_type == "test_fix" and ("test" in basename or "spec" in basename or "test" in rel_lower):
            score += 8

        # Prefer source files
        if rel.endswith((".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java")):
            score += 0.5

        scored.append((score, rel))

    # Sort descending by score, take top N
    scored.sort(key=lambda t: (-t[0], t[1]))
    selected = [rel for _score, rel in scored[:_MAX_FILES]]

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
    """Clone repo (or restore from GCS cache), checkout branch, download dossier."""
    workspace = f"/workspace/{config.task_id}"
    os.makedirs(workspace, exist_ok=True)

    repo_url = os.environ.get("REPO_URL", "")
    branch = os.environ.get("BRANCH", "main")

    # Try snapshot cache first
    from henchmen.dossier.cache import SnapshotCache

    cache = SnapshotCache(settings)
    snapshot_uri = await cache.get_snapshot(repo_url, branch)
    if snapshot_uri:
        logger.info("Restoring workspace from snapshot cache: %s", snapshot_uri)
        await cache.restore_snapshot(snapshot_uri, workspace)
    elif repo_url:
        # Normalize repo_url to "owner/repo" form expected by clone_repo.
        github_token = os.environ.get("GITHUB_TOKEN", "")
        if repo_url.startswith("https://github.com/"):
            repo_slug = repo_url[len("https://github.com/") :]
            if repo_slug.endswith(".git"):
                repo_slug = repo_slug[: -len(".git")]
        else:
            repo_slug = repo_url

        # Use deeper clone for feature branches so we have origin/main for diffing
        depth = 50 if branch.startswith("henchmen/") else 1
        logger.info("Cloning repo %s (branch: %s, depth: %s)", repo_url, branch, depth)
        await clone_repo(
            repo_slug,
            branch,
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

    # Fetch origin/main ref so scoped lint/tests can diff against it
    proc = await asyncio.create_subprocess_exec(
        "git",
        "fetch",
        "origin",
        "main:refs/remotes/origin/main",
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    # Install project dependencies if package.json exists (Node.js projects need this for type checking)
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
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.info("Node.js dependencies installed successfully")
        else:
            logger.warning("Node.js dependency install failed (non-fatal): %s", stderr.decode()[:500])

    # Download dossier artifact if available
    dossier_uri = os.environ.get("DOSSIER_URI")
    if dossier_uri:
        await download_dossier(dossier_uri, workspace, object_store=object_store)

    return workspace


async def _check_for_changes(workspace_dir: str) -> bool:
    """Check if the workspace has any uncommitted or committed-but-not-pushed changes."""
    try:
        # Check for uncommitted changes (modified, new, deleted files)
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            cwd=workspace_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if stdout.strip():
            logger.info("[OPERATIVE] Uncommitted changes detected")
            return True

        # Check if current branch has commits ahead of origin/main (agent committed on the branch)
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-list",
            "--count",
            "origin/main..HEAD",
            cwd=workspace_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        count = stdout.decode().strip()
        if count.isdigit() and int(count) > 0:
            logger.info("[OPERATIVE] Branch has %s commit(s) ahead of origin/main", count)
            return True

        return False
    except Exception as exc:
        logger.warning("Could not check for changes: %s", exc)
        return False


async def _create_branch_and_push(workspace_dir: str, branch_name: str) -> None:
    """Create a git branch, commit any uncommitted changes, and push to origin."""

    async def _git(*args: str) -> tuple[str, str, int]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=workspace_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return stdout.decode().strip(), stderr.decode().strip(), proc.returncode or 0

    # Configure git user for commits (use env vars set during initialize_workspace)
    git_email = os.environ.get("HENCHMEN_GIT_AUTHOR_EMAIL", "henchmen-operative@noreply.local")
    git_name = os.environ.get("HENCHMEN_GIT_AUTHOR_NAME", "Henchmen Operative")
    await _git("config", "user.email", git_email)
    await _git("config", "user.name", git_name)

    # Exclude henchmen temp files from the commit (only add if not already present)
    gitignore_path = os.path.join(workspace_dir, ".gitignore")
    existing = ""
    if os.path.exists(gitignore_path):
        with open(gitignore_path, encoding="utf-8") as fh:
            existing = fh.read()
    if ".henchmen_file_context.txt" not in existing:
        with open(gitignore_path, "a", encoding="utf-8") as fh:
            fh.write("\n.henchmen_file_context.txt\n")

    # Create and checkout branch
    out, err, rc = await _git("checkout", "-b", branch_name)
    if rc != 0:
        # Branch might already exist
        await _git("checkout", branch_name)

    # Stage all changes
    await _git("add", "-A")

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

    Supports gs:// (GCS) and s3:// URIs. The object_store provider handles the
    actual download; if no provider is supplied a direct GCS fallback is used so
    callers that don't yet pass a provider continue to work.
    """
    dest_dir = os.path.join(workspace, ".henchmen", "dossier")
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, "dossier.json")

    # Parse the URI into bucket + key regardless of scheme
    if uri.startswith("gs://"):
        scheme_len = len("gs://")
    elif uri.startswith("s3://"):
        scheme_len = len("s3://")
    else:
        logger.warning("Invalid dossier URI (expected gs:// or s3://): %s", uri)
        return

    without_prefix = uri[scheme_len:]
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
    """
    data = report.model_dump_json().encode("utf-8")
    topic = settings.pubsub_topic_operative_complete

    if broker is not None:
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
    """Entrypoint for the container."""
    logging.basicConfig(level=logging.INFO)
    logging.getLogger().addFilter(_SecretRedactionFilter())
    asyncio.run(run_operative())


if __name__ == "__main__":
    main()
