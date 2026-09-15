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
from collections import deque
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from henchmen.config.settings import DEFAULT_LOCAL_OPERATIVE_IMAGE
from henchmen.mastermind.scheme_executor.ci_gate import (
    GATE_OUTPUT_LIMIT,
    CheckType,
    GateCommand,
    GateResult,
    env_without_github_tokens,
    parse_gate_result,
    plan_gate,
    scrub_secret,
)
from henchmen.mastermind.scheme_executor.lint_scope import (
    CheckCommand,
    LintScopeError,
    changed_files,
    plan_fix,
)
from henchmen.models.dossier import Dossier
from henchmen.models.scheme import SchemeNode
from henchmen.models.task import HenchmenTask
from henchmen.providers.local.docker import cpu_limit as docker_cpu_limit
from henchmen.providers.local.docker import memory_limit as docker_memory_limit
from henchmen.providers.registry import orchestrator_is_local
from henchmen.utils.git import clone_repo, get_github_token
from henchmen.utils.stack_detector import Stack, detect_stack

if TYPE_CHECKING:
    from henchmen.config.settings import Settings
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

    When the effective container orchestrator is local (a desktop install),
    the install, the fixers and the commit/push all run in a gate container
    (``ci_gate fix``) as an unprivileged user, never natively in this server
    process (decision C18). The cloud path below is unchanged.
    """
    repo = task.context.repo
    branch = task.branch_name
    github_token = get_github_token()

    if not repo:
        return {"condition": "fail", "message": "fix_lint failed (no repo)"}

    if orchestrator_is_local(executor.settings):
        return await _fix_lint_in_container(executor.settings, task, repo=repo, branch=branch)

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


async def _fix_lint_in_container(settings: Settings, task: HenchmenTask, *, repo: str, branch: str) -> dict[str, Any]:
    """Desktop ``fix_lint``: run ``ci_gate fix`` in a gate container and map its result to the node's edge."""
    try:
        result = await run_gate_in_container(
            settings,
            "fix",
            repo=repo,
            branch=branch,
            base_branch=task.context.branch or "main",
            extra_args=(f"--author-name={settings.git_author_name}", f"--author-email={settings.git_author_email}"),
        )
    except Exception as exc:
        detail = scrub_secret(str(exc), settings.github_token)
        logger.warning("fix_lint failed for task %s: %s", task.id, detail)
        return {"condition": "fail", "message": f"fix_lint failed (error: {detail})"}
    if result["condition"] != "pass":
        logger.warning("[SCHEME] fix_lint failed for task %s: %s", task.id, result["message"])
        return result
    logger.info("[SCHEME] %s for task %s", result["message"], task.id)
    # A finished fix is an unconditional edge to run_lint_retry, exactly like the cloud handler.
    return {**result, "condition": None}


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


async def _run_ci_check(executor: SchemeExecutor, task: HenchmenTask, check_type: CheckType) -> dict[str, Any]:
    """Run a lint or test gate against the task's branch.

    Cloud mode clones the branch on this host and runs the stack's commands
    natively. Local mode never touches a host path: a gate container from the
    operative image clones the branch itself, computes the diff against
    ``origin/<base>`` in its own workspace and runs the scoped commands
    (:mod:`henchmen.mastermind.scheme_executor.ci_gate`), because the Mastermind
    may itself run in a container whose paths a sibling container cannot mount.
    Both paths share the same clone/detect/scope logic through
    :func:`henchmen.mastermind.scheme_executor.ci_gate.plan_gate`.

    The lint check only judges files the branch changed against
    ``origin/<base>`` (see :mod:`henchmen.mastermind.scheme_executor.lint_scope`);
    when that diff cannot be computed the gate fails.

    Fail-closed throughout: a clone failure, an undetectable stack, an
    uncomputable diff, a gate container that reports no result, a timeout or a
    non-zero exit code all return ``condition: "fail"``. A project without a
    lint/test script is expressed through the package manager's
    ``--if-present`` flag (a real exit code of 0), never by masking the exit
    code in the shell.

    Args:
        executor: The scheme executor (provides settings)
        task: The task being executed (provides repo and branch info)
        check_type: "lint" or "tests"
    """
    from henchmen.config.settings import get_settings

    repo = task.context.repo
    branch = task.branch_name
    base_branch = task.context.branch or "main"
    settings = get_settings()
    github_token = settings.github_token

    if not repo:
        logger.warning("No repo for CI check, failing")
        return {"condition": "fail", "message": f"{check_type} failed (no repo)"}

    # Gated on the *effective* container orchestrator, exactly like
    # LairManager._build_env_vars — a container gate must never run where
    # lairs run in the cloud, or the other way round.
    if orchestrator_is_local(settings):
        try:
            result = await run_gate_in_container(
                settings, check_type, repo=repo, branch=branch, base_branch=base_branch
            )
        except Exception as exc:
            detail = scrub_secret(str(exc), github_token)
            logger.warning("CI check %s failed for task %s: %s", check_type, task.id, detail)
            return {"condition": "fail", "message": f"{check_type} failed (error: {detail})"}
        passed = result["condition"] == "pass"
        logger.info("[SCHEME] %s %s for task %s", check_type, "PASSED" if passed else "FAILED", task.id)
        if not passed:
            logger.warning("[SCHEME] %s output: %s", check_type, str(result.get("output", ""))[:2000])
        return result

    workspace = tempfile.mkdtemp(prefix=f"henchmen-{check_type}-")
    try:
        planned = await plan_gate(
            check_type, repo=repo, branch=branch, base_branch=base_branch, token=github_token, workspace=workspace
        )
        if isinstance(planned, GateResult):
            passed = planned.condition == "pass"
            if passed:
                logger.info("[SCHEME] %s passed for task %s: %s", check_type, task.id, planned.message)
            else:
                logger.warning("[SCHEME] %s failed for task %s: %s", check_type, task.id, planned.message)
            return planned.model_dump()

        stack = planned.stack
        logger.info("[SCHEME] Detected stack %s for %s check on task %s", stack.name, check_type, task.id)

        # In cloud mode the host has the toolchain.
        result = await _run_on_host(workspace, stack, planned.commands)

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


_GATE_MODULE = "henchmen.mastermind.scheme_executor.ci_gate"
# Caps the same runaway-container risk --pids-limit guards against on a lair
# (fork bombs, thread-exhaustion loops) for a gate container specifically.
_GATE_PIDS_LIMIT = 1024

# A gate container's stdout/stderr are captured by *this* long-lived server
# process, not by the disposable container — so, unlike ci_gate's own inner
# `_run_script` (which runs and buffers inside the ephemeral container), the
# read here must never accumulate unboundedly. Each pipe is drained to EOF in
# fixed-size chunks and only the most recent `_GATE_OUTPUT_TAIL_BYTES` are
# kept; the result marker is always the last line written, so the tail still
# contains it however much output preceded it.
_GATE_READ_CHUNK_BYTES = 65536
_GATE_OUTPUT_TAIL_BYTES = 256 * 1024


# The gate process starts as root only to drop privileges: repo-controlled code
# runs as ci_gate.UNPRIVILEGED_UID. Everything else a root process could do is
# dropped; CHOWN hands the workspace over, SETUID/SETGID drop to that user,
# DAC_OVERRIDE/FOWNER let the root-owned git steps of `fix` read and restore
# files the unprivileged user now owns.
_GATE_SECURITY_ARGS: tuple[str, ...] = (
    "--user",
    "0:0",
    "--cap-drop",
    "ALL",
    "--cap-add",
    "CHOWN",
    "--cap-add",
    "DAC_OVERRIDE",
    "--cap-add",
    "FOWNER",
    "--cap-add",
    "SETUID",
    "--cap-add",
    "SETGID",
    "--security-opt",
    "no-new-privileges",
)


def _gate_timeout_seconds(settings: Settings) -> float:
    """A gate that never finishes must fail rather than hold the task forever; reuse the operative timeout."""
    return float(settings.lair_default_timeout)


# Bounds docker kill / docker rm -f / proc.wait() during cleanup: a hung or
# unresponsive docker daemon must not hang this handler forever on top of the
# gate's own timeout above.
_GATE_CLEANUP_TIMEOUT_SECONDS = 30


class _BoundedTail:
    """Keeps only the most recent ``cap`` bytes appended to it, in whole chunks.

    Trimming drops whole chunks rather than slicing inside one, so the tail
    can briefly hold up to one extra chunk beyond ``cap`` — cheap, and still
    bounded, since chunks are read in fixed (much smaller) sizes.
    """

    def __init__(self, cap: int = _GATE_OUTPUT_TAIL_BYTES) -> None:
        self._chunks: deque[bytes] = deque()
        self._total = 0
        self._cap = cap

    def add(self, chunk: bytes) -> None:
        self._chunks.append(chunk)
        self._total += len(chunk)
        while self._total > self._cap and len(self._chunks) > 1:
            self._total -= len(self._chunks.popleft())

    def getvalue(self) -> bytes:
        return b"".join(self._chunks)


async def _drain_stream(stream: asyncio.StreamReader | None, tail: _BoundedTail) -> None:
    """Read *stream* to EOF in bounded chunks, keeping only *tail*'s most recent bytes.

    The stream is always drained fully, cap or no cap: an unread pipe fills
    its OS buffer and blocks the child's next write to it, which can wedge a
    *sibling* pipe too (e.g. line-buffered stdout interleaved with a bulk
    stderr write) — so stdout and stderr must both be drained concurrently,
    never one after the other.
    """
    if stream is None:
        return
    while True:
        chunk = await stream.read(_GATE_READ_CHUNK_BYTES)
        if not chunk:
            break
        tail.add(chunk)


async def _remove_gate_container(name: str) -> None:
    """Fallback when `docker kill` itself fails: force-remove the container. Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", name, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "docker rm -f %s exited %s: %s",
                name,
                proc.returncode,
                (stdout or b"").decode(errors="replace").strip(),
            )
    except Exception as exc:
        logger.warning("Could not remove gate container %s via docker rm -f: %s", name, exc)


async def _kill_gate_container(name: str) -> None:
    """`docker kill`, falling back to `docker rm -f` if the kill itself fails. Never raises."""
    killed = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", name, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        killed = proc.returncode == 0
        if not killed:
            logger.warning(
                "docker kill %s exited %s: %s", name, proc.returncode, (stdout or b"").decode(errors="replace").strip()
            )
    except Exception as exc:
        logger.warning("Could not kill gate container %s: %s", name, exc)

    if not killed:
        await _remove_gate_container(name)


async def _cleanup_gate_container(container: str, proc: asyncio.subprocess.Process) -> None:
    """Kill (or force-remove) the container and reap the process. Never raises except cancellation."""
    await _kill_gate_container(container)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


def _gate_resource_limit_args(settings: Settings) -> list[str]:
    """The same `--memory`/`--cpus` a lair gets (`Settings.lair_default_memory`/`_cpu`), plus a pids cap.

    A gate container is otherwise unbounded: nothing else limits how much
    memory or CPU repo-controlled code (an install script, the linter, the
    test suite) can consume, or how many processes it can fork.
    """
    args = ["--memory", docker_memory_limit(settings.lair_default_memory)]
    cpus = docker_cpu_limit(settings.lair_default_cpu)
    if cpus:
        args.extend(["--cpus", cpus])
    args.extend(["--pids-limit", str(_GATE_PIDS_LIMIT)])
    return args


async def _deliver_token(proc: asyncio.subprocess.Process, token: str) -> None:
    """Write the token line to the gate's stdin and close it; the gate reads it before anything else.

    A gate that already exited (docker could not start it) closes the pipe
    first; that is not an error here — the run then fails on its missing result.
    """
    stdin = proc.stdin
    if stdin is None:
        return
    try:
        stdin.write(f"{token}\n".encode())
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        logger.warning("The gate container closed its stdin before the token was delivered")
    finally:
        stdin.close()


async def run_gate_in_container(
    settings: Settings,
    command: GateCommand,
    *,
    repo: str,
    branch: str,
    base_branch: str,
    extra_args: tuple[str, ...] = (),
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Run ``ci_gate <command>`` in a fresh operative-image container that clones and diffs by itself.

    The single docker runner for every local gate: the Mastermind's lint and
    test gates, desktop ``fix_lint`` and desktop Forge CI all come through
    here. The GitHub token reaches the container only over stdin — never argv,
    never the docker CLI's or the container's environment (decision C18).
    """
    token = settings.github_token
    image = settings.operative_image or DEFAULT_LOCAL_OPERATIVE_IMAGE
    container = f"henchmen-gate-{uuid4().hex[:12]}"
    # --init: a tiny init reaps the orphans repo code leaves behind and forwards signals to the gate.
    cmd = ["docker", "run", "--rm", "--init", "-i", "--name", container]
    if settings.local_docker_network:
        cmd.extend(["--network", settings.local_docker_network])
    cmd.extend(_gate_resource_limit_args(settings))
    cmd.extend(_GATE_SECURITY_ARGS)
    cmd.extend(["--entrypoint", "python", image, "-m", _GATE_MODULE, command])
    cmd.extend([f"--repo={repo}", f"--branch={branch}", f"--base={base_branch}", *extra_args])

    logger.info("[SCHEME] Running %s gate for %s@%s in %s", command, repo, branch, image)
    # stdout and stderr are separate pipes (not merged), each drained by its
    # own task below, so a flood on either one can never block the other. The
    # docker CLI needs this process's PATH/DOCKER_HOST, but no token variable.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env_without_github_tokens(),
    )
    stdout_tail = _BoundedTail()
    stderr_tail = _BoundedTail()
    stdout_task = asyncio.ensure_future(_drain_stream(proc.stdout, stdout_tail))
    stderr_task = asyncio.ensure_future(_drain_stream(proc.stderr, stderr_tail))

    timeout = timeout_seconds if timeout_seconds is not None else _gate_timeout_seconds(settings)
    exited_normally = False
    timeout_result: dict[str, Any] | None = None
    try:
        await _deliver_token(proc, token)
        await asyncio.wait_for(asyncio.gather(proc.wait(), stdout_task, stderr_task), timeout=timeout)
        exited_normally = True
    except TimeoutError:
        timeout_result = {
            "condition": "fail",
            "message": f"{command} failed (the gate did not finish within {timeout:g}s)",
            "output": "",
        }
    finally:
        # Anything other than the gather above completing outright — a
        # timeout, this coroutine itself being cancelled, or any other
        # exception out of the token delivery, proc.wait() or the drain tasks —
        # leaves the container and its reader tasks running unless cleaned up
        # here. A CancelledError already propagating still propagates once
        # this block finishes, per asyncio's cancellation contract.
        if not exited_normally:
            await _cleanup_after_abnormal_exit(container, proc, (stdout_task, stderr_task))

    if timeout_result is not None:
        return timeout_result

    returncode = proc.returncode if proc.returncode is not None else 1
    # The result marker is always written to stdout (ci_gate.main), never stderr.
    stdout_text = stdout_tail.getvalue().decode(errors="replace")
    result = parse_gate_result(stdout_text)
    if result is None:
        stderr_text = stderr_tail.getvalue().decode(errors="replace")
        # stderr first (a crash traceback lives there), then stdout, each cut
        # to its own last ~GATE_OUTPUT_LIMIT characters. A tail sliced by raw
        # character count can start mid-line; drop that partial line (before
        # scrubbing) so the kept text always begins cleanly.
        combined = _tail_chars(stderr_text) + _tail_chars(stdout_text)
        return {
            "condition": "fail",
            "message": f"{command} failed (the gate container exited {returncode} without a result)",
            "output": scrub_secret(combined, token),
        }
    message = scrub_secret(result.message, token)
    output = scrub_secret(result.output, token)
    extra: dict[str, Any] = {}
    if result.checks:
        # Multi-check gates (forge) report each check; scrubbed like the rest of the result.
        extra["checks"] = [
            {
                "name": check.name,
                "condition": check.condition,
                "message": scrub_secret(check.message, token),
                "output": scrub_secret(check.output, token),
            }
            for check in result.checks
        ]
    if result.condition == "pass" and returncode == 0:
        return {"condition": "pass", "message": message, "output": output, **extra}
    if result.condition == "pass":
        message = f"{command} failed (the gate reported a pass but exited {returncode})"
    return {"condition": "fail", "message": message, "output": output, **extra}


async def _cleanup_after_abnormal_exit(
    container: str, proc: asyncio.subprocess.Process, readers: tuple[asyncio.Future[None], ...]
) -> None:
    """Cancel the reader tasks, then kill (or force-remove) the container, bounded in time.

    Awaiting a reader task this function just cancelled raises that task's own
    CancelledError, which is swallowed. A *new* cancellation aimed at the
    calling task while it waits shows up as a higher
    ``asyncio.current_task().cancelling()`` count: it is remembered, the
    container is still cleaned up, and then it is re-raised.
    """
    current = asyncio.current_task()
    baseline = current.cancelling() if current is not None else 0
    cancelled_meanwhile = False
    for reader in readers:
        reader.cancel()
    for reader in readers:
        try:
            await reader
        except asyncio.CancelledError:
            if current is not None and current.cancelling() > baseline:
                cancelled_meanwhile = True
        except Exception:
            pass
    # Bounded: a hung or unresponsive docker daemon must not hang this handler
    # forever on top of the gate's own timeout. A genuine cancellation of the
    # calling task while inside this call still propagates as CancelledError
    # (asyncio.wait_for only converts its *own* internal timeout to
    # TimeoutError); only that internal timeout is caught here and logged.
    try:
        await asyncio.wait_for(_cleanup_gate_container(container, proc), timeout=_GATE_CLEANUP_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "Gate container cleanup for %s did not finish within %ss", container, _GATE_CLEANUP_TIMEOUT_SECONDS
        )
    if cancelled_meanwhile:
        raise asyncio.CancelledError


def _tail_chars(text: str, limit: int = GATE_OUTPUT_LIMIT) -> str:
    """The last `limit` characters of `text`, with a trimmed tail's partial leading fragment dropped.

    A tail sliced by raw character count can start mid-line, or (with no
    newline anywhere in the kept window) mid-word — either way a fragment of
    a secret token could otherwise survive at the very start of the kept
    text. When there's a newline, drop up to and including it. Otherwise: a
    window that starts on whitespace starts on a word boundary, so nothing is
    partial (only the leading whitespace goes); a window that starts mid-word
    drops up to the next run of whitespace; and a window with no whitespace at
    all could be one long secret fragment, so none of it is kept
    (fail-closed). Text that didn't need trimming is returned unchanged.
    """
    if len(text) <= limit:
        return text
    trimmed = text[-limit:]
    if "\n" in trimmed:
        return trimmed.split("\n", 1)[1]
    if trimmed[0].isspace():
        return trimmed.lstrip()
    parts = trimmed.split(None, 1)
    return parts[1] if len(parts) > 1 else ""


async def _run_on_host(workspace: str, stack: Stack, commands: tuple[CheckCommand, ...]) -> dict[str, Any]:
    """Run CI check commands natively on the host (cloud mode); the first non-zero exit code wins.

    Deliberately unchanged from before Task 8's token-scrubbing work: the
    global constraint that non-desktop behaviour is unchanged overrides
    scrubbing the token out of this path too, so these subprocesses inherit
    the ambient environment exactly as they always have. Only the gate
    container path (``ci_gate.run_gate``) scrubs the token, per that
    ruling.
    """
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
