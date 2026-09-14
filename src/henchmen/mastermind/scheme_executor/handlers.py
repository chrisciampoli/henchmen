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
import shlex
import shutil
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from henchmen.mastermind.scheme_executor.lint_scope import (
    CheckCommand,
    LintScopeError,
    changed_files,
    plan_fix,
    plan_lint,
    to_shell_script,
)
from henchmen.models.dossier import Dossier
from henchmen.models.scheme import SchemeNode
from henchmen.models.task import HenchmenTask
from henchmen.utils.git import clone_repo, get_github_token
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
    github_token = get_github_token()

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

        # Fix only what the operative changed. Running the fixer over the whole
        # repository used to commit rewrites of files the operative never touched.
        stack = detect_stack(Path(workspace))
        if stack.name == "unknown":
            return {"condition": "fail", "message": f"fix_lint failed — could not detect the project stack for {repo}"}
        base_branch = task.context.branch or "main"
        try:
            scope = await changed_files(workspace, base_branch)
        except LintScopeError as exc:
            detail = str(exc).replace(github_token, "***") if github_token else str(exc)
            return {
                "condition": "fail",
                "message": f"fix_lint failed — could not determine the files changed against {base_branch}: {detail}",
            }
        plan = plan_fix(stack, Path(workspace), scope)
        if not plan.commands:
            logger.info("[SCHEME] fix_lint: nothing to auto-fix for task %s (%s)", task.id, plan.skip_reason)
            return {"condition": None, "message": f"fix_lint: nothing to auto-fix ({plan.skip_reason})"}

        outputs: list[str] = []
        for command in plan.commands:
            proc = await asyncio.create_subprocess_exec(
                *command.argv,
                cwd=os.path.join(workspace, command.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            outputs.append(stdout.decode(errors="replace"))
            logger.info("[SCHEME] fix_lint ran %s for task %s (rc=%s)", command.argv[:3], task.id, proc.returncode)
        fix_output = "\n".join(outputs)[:2000]

        # Stage only in-scope files; anything else the fixer touched is reverted.
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            "-z",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        status_out, _ = await proc.communicate()
        modified = [entry[3:] for entry in status_out.decode(errors="replace").split("\0") if len(entry) > 3]
        in_scope = sorted(set(modified) & set(scope))
        out_of_scope = sorted(set(modified) - set(scope))
        if out_of_scope:
            logger.warning("[SCHEME] fix_lint: reverting auto-fixes outside the operative's changes: %s", out_of_scope)
            returncode, revert_err = await _run_git(workspace, "checkout", "--", *out_of_scope)
            if returncode != 0:
                return {
                    "condition": "fail",
                    "message": f"fix_lint failed (could not revert out-of-scope fixes): {revert_err[:300]}",
                }

        if not in_scope:
            logger.info("[SCHEME] fix_lint: no files changed by auto-fix")
            return {"condition": None, "message": "fix_lint: auto-fix made no changes"}

        # Commit and push the auto-fixed files. Every git step is awaited to
        # completion and checked: an unawaited `git config`/`git add` raced the
        # commit, and an ignored commit failure pushed nothing while reporting
        # "auto-fixed and pushed".
        git_steps: list[tuple[str, ...]] = [
            ("config", "user.email", executor.settings.git_author_email),
            ("config", "user.name", executor.settings.git_author_name),
            ("add", "--", *in_scope),
            ("commit", "-m", "style: auto-fix lint issues"),
            ("push", "origin", branch),
        ]
        for step in git_steps:
            returncode, step_err = await _run_git(workspace, *step)
            if returncode != 0:
                err = step_err[:300]
                if github_token:
                    err = err.replace(github_token, "***")
                return {"condition": "fail", "message": f"fix_lint failed (git {step[0]} failed): {err}"}

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


async def _run_git(workspace: str, *args: str) -> tuple[int, str]:
    """Run a git command in *workspace* to completion; return ``(returncode, stderr)``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    returncode = proc.returncode if proc.returncode is not None else 1
    return returncode, stderr.decode(errors="replace")


@_register("run_tests")
@_register("run_tests_retry")
async def handle_run_tests(
    executor: SchemeExecutor, node: SchemeNode, task: HenchmenTask, dossier: Dossier
) -> dict[str, Any]:
    """Clone the Henchmen branch and run tests."""
    return await _run_ci_check(executor, task, "tests")


async def _run_ci_check(executor: SchemeExecutor, task: HenchmenTask, check_type: str) -> dict[str, Any]:
    """Clone the task's branch and run a specific CI check.

    Uses :func:`henchmen.utils.stack_detector.detect_stack` to pick the
    right test / lint commands for the target repo's language.

    In local mode (provider=local), commands are executed inside the
    operative Docker image via ``docker run`` with the workspace mounted
    as a volume. This ensures the correct toolchain (Node.js, npm,
    eslint, etc.) is available regardless of the host OS.

    The lint check only judges files the branch changed against
    ``origin/<base>`` (see :mod:`henchmen.mastermind.scheme_executor.lint_scope`);
    when that diff cannot be computed the gate fails.

    Fail-closed throughout: a clone failure, an undetectable stack, an
    uncomputable diff or a non-zero exit code all return ``condition: "fail"``. A project without a
    lint/test script is expressed through the package manager's
    ``--if-present`` flag (a real exit code of 0), never by masking the exit
    code in the shell.

    Args:
        executor: The scheme executor (provides settings)
        task: The task being executed (provides repo and branch info)
        check_type: "lint" or "tests"
    """
    from pathlib import Path

    from henchmen.config.settings import get_settings

    repo = task.context.repo
    branch = task.branch_name
    base_branch = task.context.branch or "main"
    settings = get_settings()
    github_token = settings.github_token
    is_local = settings.provider == "local"

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

        stack = detect_stack(Path(workspace))
        logger.info("[SCHEME] Detected stack %s for %s check on task %s", stack.name, check_type, task.id)

        if stack.name == "unknown":
            # No recognizable manifest — we cannot prove the change is safe,
            # so escalate for human review rather than waving it through.
            logger.warning("[SCHEME] %s could not detect a project stack for %s", check_type, repo)
            return {
                "condition": "fail",
                "message": (
                    f"{check_type} failed — could not detect the project stack for {repo} "
                    "(no pyproject.toml/package.json/go.mod/Cargo.toml/pom.xml found)"
                ),
            }

        if check_type == "lint":
            # Judge only what the operative changed, never pre-existing violations.
            # If the diff cannot be computed the gate cannot be scoped: fail closed.
            try:
                plan = plan_lint(stack, Path(workspace), await changed_files(workspace, base_branch))
            except LintScopeError as exc:
                detail = str(exc).replace(github_token, "***") if github_token else str(exc)
                logger.warning("[SCHEME] lint scoping failed for task %s: %s", task.id, detail)
                return {
                    "condition": "fail",
                    "message": f"lint failed — could not determine the files changed against {base_branch}: {detail}",
                }
            if not plan.commands:
                logger.info("[SCHEME] lint passed for task %s: %s", task.id, plan.skip_reason)
                return {"condition": "pass", "message": f"lint passed — {plan.skip_reason}", "output": ""}
            commands = plan.commands
        else:
            commands = (CheckCommand(argv=tuple(stack.test_command)),)

        if is_local:
            # Run inside the operative Docker image with the workspace mounted.
            result = await _run_in_docker(workspace, to_shell_script(commands, _install_script(stack)))
        else:
            # In cloud mode the host has the toolchain.
            result = await _run_on_host(workspace, stack, commands)

        passed = result["returncode"] == 0
        output = result["output"]

        logger.info("[SCHEME] %s %s for task %s", check_type, "PASSED" if passed else "FAILED", task.id)
        if not passed:
            logger.warning("[SCHEME] %s output: %s", check_type, output[:2000])

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


async def _run_in_docker(workspace: str, shell_script: str) -> dict[str, Any]:
    """Run a CI check command inside the operative Docker image.

    Mounts the cloned workspace as a volume so the container has access
    to the code and the correct toolchain (Node.js, npm, Python, etc.).
    Only reachable in local mode, where the image is always the locally
    built ``henchmen-operative:local``.
    """
    # Convert Windows paths to Docker-compatible format
    docker_workspace = workspace.replace("\\", "/")
    image = "henchmen-operative:local"

    cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{docker_workspace}:/ci-workspace",
        "-w",
        "/ci-workspace",
        "--entrypoint",
        "/bin/bash",
        image,
        "-c",
        shell_script,
    ]

    logger.info("[SCHEME] Running CI check in Docker: %s", shell_script[:200])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode(errors="replace")[:5000] if stdout else ""

    return {"returncode": proc.returncode if proc.returncode is not None else 1, "output": output}


def _install_script(stack: Stack) -> str | None:
    """The dependency install step as a shell fragment, with a lockfile-less fallback for Node."""
    if stack.install_command is None:
        return None
    install_str = shlex.join(stack.install_command)
    if stack.name == "node-pnpm":
        # --frozen-lockfile fails when the lockfile is stale/absent.
        return f"{install_str} || pnpm install --no-frozen-lockfile"
    if stack.name == "node-npm":
        # `npm ci` requires a lockfile; a plain install is the fallback.
        return f"{install_str} || npm install --no-audit"
    return install_str


async def _run_on_host(workspace: str, stack: Stack, commands: tuple[CheckCommand, ...]) -> dict[str, Any]:
    """Run CI check commands natively on the host (cloud mode); the first non-zero exit code wins."""
    if stack.install_command is not None:
        proc = await asyncio.create_subprocess_exec(
            *stack.install_command,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    returncode = 0
    outputs: list[str] = []
    for command in commands:
        proc = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=os.path.join(workspace, command.cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        stdout_text = stdout.decode(errors="replace")[:3000]
        stderr_text = stderr.decode(errors="replace")[:3000]
        output = stdout_text
        if stderr_text:
            output = f"{stdout_text}\n--- stderr ---\n{stderr_text}" if stdout_text.strip() else stderr_text
        outputs.append(output)
        command_rc = proc.returncode if proc.returncode is not None else 1
        if command_rc != 0 and returncode == 0:
            returncode = command_rc

    return {"returncode": returncode, "output": "\n".join(outputs)}


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
    base_branch = task.context.branch or "main"
    base_ref = f"origin/{base_branch}"
    github_token = get_github_token()

    if not repo:
        return {"condition": "fail", "message": "verify_changes failed (no repo)"}

    workspace = tempfile.mkdtemp(prefix="henchmen-verify-")
    try:
        try:
            await clone_repo(repo, branch, workspace, token=github_token or None)
        except RuntimeError as exc:
            return {"condition": "fail", "message": f"verify_changes failed (clone failed): {exc}"}

        logger.info("[SCHEME] verify_changes: cloned %s, fetching %s...", branch, base_ref)

        fetch_proc = await asyncio.create_subprocess_exec(
            "git",
            "fetch",
            "origin",
            f"{base_branch}:refs/remotes/origin/{base_branch}",
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
                "[SCHEME] verify_changes: fetch %s failed (rc=%s): %s",
                base_ref,
                fetch_proc.returncode,
                err,
            )

        # Check for commits beyond the base branch
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            f"{base_ref}..HEAD",
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
                "message": (f"verify_changes failed: no commits on branch beyond {base_branch} (stderr: {log_stderr})"),
            }

        # Check for source file changes
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--name-only",
            base_ref,
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
    github_token = get_github_token()

    if not repo or not github_token:
        # Fail-closed: a fabricated ".../pull/new" URL used to be reported as a
        # successful PR, which finalized the task and triggered CI on a PR that
        # does not exist.
        logger.error("Cannot create PR: repo=%s, token_present=%s", repo, bool(github_token))
        missing = "repo" if not repo else "GitHub token (HENCHMEN_GITHUB_TOKEN)"
        return {"condition": "fail", "message": f"PR creation failed: missing {missing}"}

    try:
        from github import Auth, Github

        logger.info("[CREATE_PR] Creating PR for task %s on %s (branch: %s)", task.id, repo, branch_name)

        g = Github(auth=Auth.Token(github_token))
        github_repo = g.get_repo(repo)

        # Layer 2: PR dedup — check if a PR already exists for this branch.
        # GitHub ignores a ``head`` filter that lacks the ``owner:`` prefix and
        # returns every open PR, so qualify it and re-check the ref locally.
        owner = repo.split("/")[0]
        existing_prs = [
            pr
            for pr in github_repo.get_pulls(head=f"{owner}:{branch_name}", state="open")
            if pr.head.ref == branch_name
        ]
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
