"""Deterministic node handlers for scheme execution.

Each handler is an async function with signature:
    async def handler(executor, node, task, dossier) -> dict[str, Any]

The ``executor`` parameter provides access to settings, node_results, etc.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from henchmen.models.dossier import Dossier
from henchmen.models.scheme import SchemeNode
from henchmen.models.task import HenchmenTask
from henchmen.utils.git import clone_repo
from henchmen.utils.stack_detector import Stack, detect_stack

if TYPE_CHECKING:
    from henchmen.mastermind.scheme_executor.executor import SchemeExecutor

logger = logging.getLogger(__name__)

# Type alias for handler functions
HandlerFn = Callable[["SchemeExecutor", SchemeNode, HenchmenTask, Dossier], Coroutine[Any, Any, dict[str, Any]]]

# Handler registry — maps node IDs/names to handler functions
_HANDLERS: dict[str, HandlerFn] = {}


def _register(name: str) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator to register a handler function by node ID."""

    def decorator(fn: HandlerFn) -> HandlerFn:
        _HANDLERS[name] = fn
        return fn

    return decorator


def get_handler(node_id_or_name: str) -> HandlerFn | None:
    """Look up a handler by node ID or name."""
    return _HANDLERS.get(node_id_or_name)


# ---------------------------------------------------------------------------
# Branch / context handlers
# ---------------------------------------------------------------------------


@_register("create_branch")
async def handle_create_branch(
    executor: SchemeExecutor, node: SchemeNode, task: HenchmenTask, dossier: Dossier
) -> dict[str, Any]:
    """Stub handler — the operative bootstrap is what actually creates the branch.

    Kept so existing schemes that reference the ``create_branch`` node do not
    break.  Returns the canonical branch name (from
    :attr:`HenchmenTask.branch_name`) and a pass-through status.
    """
    logger.debug("create_branch stub for task %s -> %s", task.id, task.branch_name)
    return {
        "condition": None,  # unconditional next
        "branch_name": task.branch_name,
        "status": "ok",
        "message": f"Branch {task.branch_name} (no-op — created by operative bootstrap)",
    }


@_register("prefetch_context")
async def handle_prefetch_context(
    executor: SchemeExecutor, node: SchemeNode, task: HenchmenTask, dossier: Dossier
) -> dict[str, Any]:
    """Prefetch context data for the operative."""
    logger.info("Prefetching context for task %s", task.id)
    return {
        "condition": None,
        "message": "Context prefetched",
        "dossier_artifact_uri": dossier.artifact_uri,
    }


# ---------------------------------------------------------------------------
# CI check handlers
# ---------------------------------------------------------------------------


@_register("run_lint")
@_register("run_lint_retry")
async def handle_run_lint(
    executor: SchemeExecutor, node: SchemeNode, task: HenchmenTask, dossier: Dossier
) -> dict[str, Any]:
    """Clone the Henchmen branch and run lint checks."""
    return await _run_ci_check(executor, task, "lint")


@_register("fix_lint")
async def handle_fix_lint(
    executor: SchemeExecutor, node: SchemeNode, task: HenchmenTask, dossier: Dossier
) -> dict[str, Any]:
    """Run eslint --fix / ruff --fix and commit the auto-fixed files.

    This is deterministic — no LLM needed. Auto-fixers handle most lint issues.
    """
    repo = task.context.repo
    branch = task.branch_name
    github_token = os.environ.get("GITHUB_TOKEN", "")

    if not repo:
        return {"condition": "fail", "message": "fix_lint failed (no repo)"}

    workspace = tempfile.mkdtemp(prefix="henchmen-fix-lint-")
    try:
        # Clone the branch
        try:
            await clone_repo(repo, branch, workspace, token=github_token or None)
        except RuntimeError as exc:
            return {"condition": "fail", "message": f"fix_lint failed (clone failed): {exc}"}

        # Install dependencies
        if os.path.exists(os.path.join(workspace, "package.json")):
            pnpm_lock = os.path.join(workspace, "pnpm-lock.yaml")
            cmd = ["pnpm", "install", "--frozen-lockfile"] if os.path.exists(pnpm_lock) else ["npm", "ci"]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        # Run lint with --fix
        is_pnpm = os.path.exists(os.path.join(workspace, "pnpm-lock.yaml"))
        is_turbo = os.path.exists(os.path.join(workspace, "turbo.json"))
        if is_pnpm and is_turbo:
            fix_cmd = ["pnpm", "run", "lint:fix"]
        elif os.path.exists(os.path.join(workspace, "package.json")):
            fix_cmd = ["npx", "eslint", ".", "--fix"]
        else:
            fix_cmd = ["python", "-m", "ruff", "check", ".", "--fix"]

        proc = await asyncio.create_subprocess_exec(
            *fix_cmd,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        fix_output = stdout.decode(errors="replace")[:2000]

        logger.info("[SCHEME] fix_lint auto-fix ran for task %s (rc=%s)", task.id, proc.returncode)

        # Check if any files were changed by the auto-fix
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        status_out, _ = await proc.communicate()
        changed_files = status_out.decode().strip()

        if not changed_files:
            logger.info("[SCHEME] fix_lint: no files changed by auto-fix")
            return {"condition": None, "message": "fix_lint: auto-fix made no changes"}

        # Commit and push the auto-fixed files
        git_email = executor.settings.git_author_email
        git_name = executor.settings.git_author_name
        await asyncio.create_subprocess_exec(
            "git",
            "config",
            "user.email",
            git_email,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.create_subprocess_exec(
            "git",
            "config",
            "user.name",
            git_name,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.create_subprocess_exec(
            "git",
            "add",
            "-A",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc = await asyncio.create_subprocess_exec(
            "git",
            "commit",
            "-m",
            "style: auto-fix lint issues",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        proc = await asyncio.create_subprocess_exec(
            "git",
            "push",
            "origin",
            branch,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, push_err = await proc.communicate()
        if proc.returncode != 0:
            err = push_err.decode()[:300]
            if github_token:
                err = err.replace(github_token, "***")
            return {"condition": "fail", "message": f"fix_lint failed (push failed): {err}"}

        logger.info("[SCHEME] fix_lint: auto-fixed and pushed for task %s", task.id)
        return {
            "condition": None,  # unconditional to run_lint_retry
            "message": "fix_lint: auto-fixed lint issues and pushed",
            "output": fix_output,
        }

    except Exception as exc:
        logger.warning("fix_lint failed for task %s: %s", task.id, exc)
        return {"condition": "fail", "message": f"fix_lint failed (error: {exc})"}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


@_register("run_tests")
@_register("run_tests_retry")
async def handle_run_tests(
    executor: SchemeExecutor, node: SchemeNode, task: HenchmenTask, dossier: Dossier
) -> dict[str, Any]:
    """Clone the Henchmen branch and run tests."""
    return await _run_ci_check(executor, task, "tests")


async def _get_affected_packages(workspace: str) -> list[str]:
    """Detect changed packages in a monorepo via git diff.

    Returns a list of package-relative paths (e.g. ``["./apps/api", "./packages/shared"]``)
    suitable for ``pnpm turbo run --filter``.  Returns empty list if detection fails
    or if changes span the root (run everything).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--name-only",
            "origin/main",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        diff_out, _ = await proc.communicate()
        changed_files = [f.strip() for f in diff_out.decode().strip().split("\n") if f.strip()]
    except Exception:
        return []

    if not changed_files:
        return []

    # Extract unique top-two-level directories (e.g. "apps/api", "packages/shared")
    packages: set[str] = set()
    for f in changed_files:
        parts = f.split("/")
        if len(parts) >= 2 and parts[0] in ("apps", "packages"):
            packages.add(f"./{parts[0]}/{parts[1]}")
        else:
            # File at root or unknown directory — can't scope, run everything
            return []

    return sorted(packages)


async def _run_ci_check(executor: SchemeExecutor, task: HenchmenTask, check_type: str) -> dict[str, Any]:
    """Clone the task's branch and run a specific CI check.

    Uses :func:`henchmen.utils.stack_detector.detect_stack` to pick the
    right test / lint commands for the target repo's language. For
    JS/TS monorepos (pnpm + turbo), scopes lint/test to affected
    packages via ``--filter``. Other stacks run the detected commands
    across the full workspace.

    Args:
        executor: The scheme executor (provides settings)
        task: The task being executed (provides repo and branch info)
        check_type: "lint" or "tests"
    """
    from pathlib import Path

    repo = task.context.repo
    branch = task.branch_name
    github_token = os.environ.get("GITHUB_TOKEN", "")

    if not repo:
        logger.warning("No repo for CI check, failing")
        return {"condition": "fail", "message": f"{check_type} failed (no repo)"}

    workspace = tempfile.mkdtemp(prefix=f"henchmen-{check_type}-")
    try:
        # Full clone — monorepo builds need all packages, not just the branch tip.
        try:
            await clone_repo(repo, branch, workspace, token=github_token or None)
        except RuntimeError as exc:
            logger.warning("Clone failed for %s check: %s", check_type, exc)
            return {"condition": "fail", "message": f"{check_type} failed (clone failed): {exc}"}

        # Fetch origin/main for diff — must map ref explicitly
        fetch_proc = await asyncio.create_subprocess_exec(
            "git",
            "fetch",
            "origin",
            "main:refs/remotes/origin/main",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await fetch_proc.communicate()

        stack = detect_stack(Path(workspace))
        logger.info("[SCHEME] Detected stack %s for %s check on task %s", stack.name, check_type, task.id)

        if stack.name == "unknown":
            # No recognizable manifest — fail open with a clear message.
            return {
                "condition": "pass",
                "message": f"{check_type} skipped — could not detect project stack",
            }

        # Install dependencies if the stack has an install step
        if stack.install_command is not None:
            proc = await asyncio.create_subprocess_exec(
                *stack.install_command,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        # For JS/TS monorepos, scope to affected packages
        affected_packages: list[str] = []
        if stack.is_monorepo:
            affected_packages = await _get_affected_packages(workspace)
            if affected_packages:
                logger.info(
                    "[SCHEME] Scoping %s to affected packages: %s",
                    check_type,
                    affected_packages,
                )

        # Run the check
        if check_type == "lint":
            lint_proc = await _run_lint_check(workspace, stack, affected_packages)
            if lint_proc is None:
                logger.info("[SCHEME] No lintable files changed for task %s, skipping lint", task.id)
                return {"condition": "pass", "message": "lint passed (no changed files to lint)"}
            proc = lint_proc
        else:  # tests
            test_cmd = list(stack.test_command)
            # Legacy monorepo path: pnpm run test still works, no extra args.
            proc = await asyncio.create_subprocess_exec(
                *test_cmd,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        stdout, stderr = await proc.communicate()
        # Capture both stdout and stderr — turbo writes to both streams
        stdout_text = stdout.decode(errors="replace")[:3000]
        stderr_text = stderr.decode(errors="replace")[:3000]
        output = stdout_text
        if stderr_text:
            output = f"{stdout_text}\n--- stderr ---\n{stderr_text}" if stdout_text.strip() else stderr_text
        passed = proc.returncode == 0

        logger.info("[SCHEME] %s %s for task %s", check_type, "PASSED" if passed else "FAILED", task.id)
        if not passed:
            logger.warning("[SCHEME] %s stdout: %s", check_type, stdout_text[:1000])
            logger.warning("[SCHEME] %s stderr: %s", check_type, stderr_text[:1000])

        return {
            "condition": "pass" if passed else "fail",
            "message": f"{check_type} {'passed' if passed else 'failed'}",
            "output": output,
        }

    except Exception as exc:
        logger.warning("CI check %s failed for task %s: %s", check_type, task.id, exc)
        return {"condition": "fail", "message": f"{check_type} failed (error: {exc})"}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


async def _run_lint_check(
    workspace: str, stack: Stack, affected_packages: list[str]
) -> asyncio.subprocess.Process | None:
    """Run lint check, returning the subprocess or None if no files to lint.

    For JS/TS monorepos the historical behaviour is preserved: scope to
    affected packages via ``pnpm turbo run lint --filter`` when possible.
    For all other stacks, delegate to ``stack.lint_command`` applied
    across the workspace. Single-package JS/TS also short-circuits to
    the ``eslint`` changed-file mode for speed.
    """
    if stack.is_monorepo:
        if affected_packages:
            filter_args: list[str] = []
            for pkg in affected_packages:
                filter_args.extend(["--filter", pkg])
            return await asyncio.create_subprocess_exec(
                "pnpm",
                "turbo",
                "run",
                "lint",
                *filter_args,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        return await asyncio.create_subprocess_exec(
            *stack.lint_command,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    # Single-package JS/TS: lint only changed files via eslint for speed.
    if stack.name == "node-npm":
        changed_proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--name-only",
            "origin/main",
            "--diff-filter=ACMR",
            "--",
            "*.ts",
            "*.tsx",
            "*.js",
            "*.jsx",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        changed_out, _ = await changed_proc.communicate()
        changed_files = [f.strip() for f in changed_out.decode().strip().split("\n") if f.strip()]

        if not changed_files:
            return None

        return await asyncio.create_subprocess_exec(
            "npx",
            "eslint",
            *changed_files,
            "--max-warnings=0",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    # All other stacks: run the stack's configured lint command
    return await asyncio.create_subprocess_exec(
        *stack.lint_command,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


# ---------------------------------------------------------------------------
# Verification handler
# ---------------------------------------------------------------------------


@_register("verify_changes")
async def handle_verify_changes(
    executor: SchemeExecutor, node: SchemeNode, task: HenchmenTask, dossier: Dossier
) -> dict[str, Any]:
    """Deterministic verification: check branch has commits and source file changes."""
    repo = task.context.repo
    branch = task.branch_name
    github_token = os.environ.get("GITHUB_TOKEN", "")

    if not repo:
        return {"condition": "fail", "message": "verify_changes failed (no repo)"}

    workspace = tempfile.mkdtemp(prefix="henchmen-verify-")
    try:
        try:
            await clone_repo(repo, branch, workspace, token=github_token or None)
        except RuntimeError as exc:
            return {"condition": "fail", "message": f"verify_changes failed (clone failed): {exc}"}

        logger.info("[SCHEME] verify_changes: cloned %s, fetching origin/main...", branch)

        fetch_proc = await asyncio.create_subprocess_exec(
            "git",
            "fetch",
            "origin",
            "main:refs/remotes/origin/main",
            "--depth=1",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        fetch_out, fetch_err = await fetch_proc.communicate()
        if fetch_proc.returncode != 0:
            err = fetch_err.decode()[:300]
            if github_token:
                err = err.replace(github_token, "***")
            logger.warning(
                "[SCHEME] verify_changes: fetch origin/main failed (rc=%s): %s",
                fetch_proc.returncode,
                err,
            )

        # Check for commits beyond origin/main
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            "origin/main..HEAD",
            "--oneline",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log_out, log_err = await proc.communicate()
        commits = [line for line in log_out.decode().strip().split("\n") if line.strip()]
        logger.info("[SCHEME] verify_changes: git log found %d commit(s)", len(commits))

        if not commits:
            log_stderr = log_err.decode()[:200]
            return {
                "condition": "fail",
                "message": f"verify_changes failed: no commits on branch beyond main (stderr: {log_stderr})",
            }

        # Check for source file changes
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--name-only",
            "origin/main",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        diff_out, _ = await proc.communicate()
        changed_files = [f.strip() for f in diff_out.decode().strip().split("\n") if f.strip()]

        if not changed_files:
            return {"condition": "fail", "message": "verify_changes failed: no file changes on branch"}

        logger.info(
            "[SCHEME] verify_changes PASSED for task %s: %d commit(s), %d file(s) changed",
            task.id,
            len(commits),
            len(changed_files),
        )
        return {
            "condition": "pass",
            "message": f"Verified: {len(commits)} commit(s), {len(changed_files)} file(s) changed",
            "commits": len(commits),
            "files_changed": changed_files,
        }

    except Exception as exc:
        logger.warning("verify_changes failed for task %s: %s", task.id, exc)
        return {"condition": "fail", "message": f"verify_changes failed (error: {exc})"}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# PR / lifecycle handlers
# ---------------------------------------------------------------------------


@_register("create_pr")
async def handle_create_pr(
    executor: SchemeExecutor, node: SchemeNode, task: HenchmenTask, dossier: Dossier
) -> dict[str, Any]:
    """Create a real GitHub pull request."""
    repo = task.context.repo
    branch_name = task.branch_name
    github_token = os.environ.get("GITHUB_TOKEN", "")

    if not repo or not github_token:
        pr_url = f"https://github.com/{repo}/pull/new"
        logger.warning("Cannot create real PR: repo=%s, token_present=%s", repo, bool(github_token))
        return {"condition": "pass", "pr_url": pr_url, "message": "PR creation skipped (missing repo or token)"}

    try:
        from github import Github

        logger.info("[CREATE_PR] Creating PR for task %s on %s (branch: %s)", task.id, repo, branch_name)

        g = Github(github_token)
        github_repo = g.get_repo(repo)

        # Layer 2: PR dedup — check if PR already exists for this branch
        existing_prs = list(github_repo.get_pulls(head=branch_name, state="open"))
        if existing_prs:
            pr_url = existing_prs[0].html_url
            logger.info("[CREATE_PR] PR already exists: %s", pr_url)
            return {
                "condition": "pass",
                "pr_url": pr_url,
                "pr_number": existing_prs[0].number,
                "message": "PR already exists",
            }

        # Build PR body
        summary = ""
        impl_result = executor.node_results.get("implement_fix", executor.node_results.get("implement_feature", {}))
        if impl_result and impl_result.get("report"):
            summary = impl_result["report"].get("summary", "")

        pr_body = (
            f"## Summary\n\n"
            f"{summary or task.description}\n\n"
            f"## Task Details\n\n"
            f"- **Task ID**: `{task.id}`\n"
            f"- **Source**: {task.source.value}\n"
            f"- **Title**: {task.title}\n\n"
            f"---\n"
            f"\U0001f916 Generated by Henchmen Agent Factory"
        )

        pr = github_repo.create_pull(
            title=f"[Henchmen] {task.title}",
            body=pr_body,
            head=branch_name,
            base=task.context.branch or "main",
        )

        # Add label (may not exist yet)
        with contextlib.suppress(Exception):
            pr.add_to_labels("henchmen-operative")

        pr_url = pr.html_url
        logger.info("[CREATE_PR] PR created: %s", pr_url)

        return {
            "condition": "pass",
            "pr_url": pr_url,
            "pr_number": pr.number,
            "message": f"PR #{pr.number} created: {pr_url}",
        }

    except Exception as exc:
        logger.error("[CREATE_PR] Failed to create PR: %s", exc)
        logger.error("Failed to create PR for task %s: %s", task.id, exc)
        return {
            "condition": "fail",
            "message": f"PR creation failed: {exc}",
        }


@_register("escalate")
async def handle_escalate(
    executor: SchemeExecutor, node: SchemeNode, task: HenchmenTask, dossier: Dossier
) -> dict[str, Any]:
    """Escalate the task to a human."""
    logger.warning("Escalating task %s", task.id)
    return {
        "condition": None,
        "message": f"Task {task.id} escalated to human review",
        "escalated": True,
    }


@_register("report_plan")
async def handle_report_plan(
    executor: SchemeExecutor, node: SchemeNode, task: HenchmenTask, dossier: Dossier
) -> dict[str, Any]:
    """Report the decomposition plan back to the user."""
    analyze_result = executor.node_results.get("analyze_goal", {})
    report = analyze_result.get("report", {})
    summary = report.get("summary", "") if isinstance(report, dict) else str(analyze_result.get("message", ""))

    return {
        "condition": "pass",
        "plan": summary,
        "message": f"Goal decomposed into sub-tasks. Plan:\n{summary}",
    }
