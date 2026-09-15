"""Run one local CI gate (lint, tests or the lint auto-fix) inside the operative image.

In local mode the Mastermind may itself run in a container, so a host path it
creates cannot be bind-mounted into a sibling container. Instead the gate
container runs this module: it clones the task branch itself, computes the
diff against ``origin/<base>`` in its own workspace, runs the scoped commands
(the same :mod:`~henchmen.mastermind.scheme_executor.lint_scope` plan the
cloud path uses) and prints one result line the Mastermind parses.

:func:`plan_gate` is the single place that clones and scopes a gate — it is
used both by this module's :func:`run_gate` / :func:`run_fix` (inside the gate
container) and by the Mastermind's cloud path (:func:`henchmen.mastermind.
scheme_executor.handlers._run_ci_check`), so the clone/detect/scope logic
exists exactly once. The paths differ only in how they *execute* the scoped
commands: the cloud path runs them natively on the host via ``_run_on_host``,
the gate container runs them as an unprivileged user.

Token isolation inside the gate container (decision C18):

* The GitHub token arrives on **stdin** (``docker run -i``; the Mastermind
  writes it and closes the pipe). It is never in this process's environment
  or argv, so ``docker inspect`` and ``/proc/<pid>/environ`` never show it.
* This process runs as root only so it can drop privileges: every
  repo-controlled command (dependency installs, linters, test suites, the
  auto-fixers) runs as :data:`UNPRIVILEGED_UID` with the workspace chowned to
  it, so it cannot read this process's memory or ``/proc/1/environ``.
* The token is used only by root-owned git calls: the clone and base fetch
  (both before any repo-controlled code runs) and, for ``fix``, the push.
  ``origin``'s URL is reset to a token-less form before repo code runs.
* ``fix`` pushes from a git directory moved out of the workspace into a
  root-only directory, with hooks disabled, after every unprivileged process
  has been killed, and passes the token to ``git push`` only through that
  one process's environment — so nothing repo code wrote (hooks, config,
  a lingering background process) can observe or redirect it.

Fail-closed: a clone failure, an undetectable stack, an uncomputable diff, a
privilege-drop failure, a non-zero exit code or any unexpected error is a
``fail`` result, and the process exits 0 only for a ``pass``.

Usage inside the operative image (the token is read from stdin)::

    python -m henchmen.mastermind.scheme_executor.ci_gate lint --repo=owner/name --branch=henchmen/x --base=main
    python -m henchmen.mastermind.scheme_executor.ci_gate fix --repo=owner/name --branch=henchmen/x --base=main
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from henchmen.mastermind.scheme_executor.lint_scope import (
    CheckCommand,
    LintScopeError,
    changed_files,
    plan_fix,
    plan_lint,
    to_shell_script,
)
from henchmen.utils.git import clone_repo
from henchmen.utils.redaction import redact
from henchmen.utils.stack_detector import Stack, detect_stack

GATE_RESULT_MARKER = "HENCHMEN_GATE_RESULT "
#: Output characters kept in a gate result — shared with the Mastermind's container runner (handlers).
GATE_OUTPUT_LIMIT = 5000
_FIX_OUTPUT_LIMIT = 2000

#: The uid/gid every repo-controlled command in the gate container runs as (``nobody``).
UNPRIVILEGED_UID = 65534
UNPRIVILEGED_GID = 65534

# The token line on stdin is bounded: a GitHub token is well under this.
_MAX_STDIN_TOKEN_CHARS = 4096

# Every environment variable a GitHub token could plausibly travel under.
# Repo-controlled code runs as a subprocess of this process and inherits its
# whole environment unless these are stripped from a copy of it first.
_TOKEN_ENV_VARS = ("HENCHMEN_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")

CheckType = Literal["lint", "tests"]
GateCommand = Literal["lint", "tests", "fix"]


class GateResult(BaseModel):
    """Outcome of one gate run, printed as the last stdout line after :data:`GATE_RESULT_MARKER`."""

    condition: Literal["pass", "fail"] = Field(..., description="Scheme edge condition")
    message: str = Field(..., description="Human-readable summary (secrets scrubbed)")
    output: str = Field(default="", description="Command output, truncated (secrets scrubbed)")


@dataclass(frozen=True)
class GatePlan:
    """The detected stack and scoped commands to run for one gate, already cloned into the workspace."""

    stack: Stack
    commands: tuple[CheckCommand, ...]
    #: The files the branch changed against ``origin/<base>`` (lint and fix only).
    changed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Sandbox:
    """Who repo-controlled commands run as, and the private HOME they get."""

    uid: int
    gid: int
    home: str


def _label(command: GateCommand) -> str:
    return "fix_lint" if command == "fix" else command


def install_script(stack: Stack) -> str | None:
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


def scrub_secret(text: str, token: str) -> str:
    """Remove ``token`` and every known secret pattern from ``text``."""
    if token:
        text = text.replace(token, "***")
    return redact(text)


def env_without_github_tokens() -> dict[str, str]:
    """A copy of the current environment with every GitHub token variable removed.

    Repo-controlled code runs as a subprocess of this process and otherwise
    inherits its whole environment, including whatever token cloned it.
    Never mutates ``os.environ`` itself — always builds a fresh copy.
    """
    return {key: value for key, value in os.environ.items() if key not in _TOKEN_ENV_VARS}


def read_token_from_stdin() -> str:
    """The GitHub token the Mastermind wrote to this container's stdin (``""`` when none)."""
    stream = sys.stdin
    if stream is None or stream.closed:
        return ""
    return stream.readline(_MAX_STDIN_TOKEN_CHARS).strip()


# ---------------------------------------------------------------------------
# Privilege separation
# ---------------------------------------------------------------------------

if sys.platform == "win32":  # pragma: no cover - the gate container is always Linux

    def _running_as_root() -> bool:
        return False

    def _chown_tree(root: str, uid: int, gid: int) -> None:
        raise OSError("changing file ownership is not supported on Windows")

else:

    def _running_as_root() -> bool:
        return os.geteuid() == 0

    def _chown_tree(root: str, uid: int, gid: int) -> None:
        """``chown -R`` without following symlinks."""
        os.lchown(root, uid, gid)
        for dirpath, dirnames, filenames in os.walk(root):
            for name in (*dirnames, *filenames):
                os.lchown(os.path.join(dirpath, name), uid, gid)


def _prepare_sandbox(workspace: str, label: str) -> Sandbox | GateResult:
    """Hand ``workspace`` (and a fresh HOME) to the unprivileged user; fail closed when that is impossible."""
    if not _running_as_root():
        return GateResult(
            condition="fail",
            message=(
                f"{label} failed (the gate container must start as root to run repository code as an unprivileged user)"
            ),
        )
    try:
        home = tempfile.mkdtemp(prefix="henchmen-gate-home-")
        _chown_tree(home, UNPRIVILEGED_UID, UNPRIVILEGED_GID)
        _chown_tree(workspace, UNPRIVILEGED_UID, UNPRIVILEGED_GID)
    except OSError as exc:
        return GateResult(condition="fail", message=f"{label} failed (could not hand the workspace over: {exc})")
    return Sandbox(uid=UNPRIVILEGED_UID, gid=UNPRIVILEGED_GID, home=home)


def _sandbox_env(sandbox: Sandbox) -> dict[str, str]:
    env = env_without_github_tokens()
    env["HOME"] = sandbox.home
    env["USER"] = "nobody"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


async def _run_unprivileged(argv: tuple[str, ...], cwd: str, sandbox: Sandbox) -> tuple[int, str]:
    """Run one repo-controlled command as the sandbox user; ``(returncode, combined output)``."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_sandbox_env(sandbox),
        user=sandbox.uid,
        group=sandbox.gid,
        extra_groups=[],
    )
    stdout, _ = await proc.communicate()
    returncode = proc.returncode if proc.returncode is not None else 1
    return returncode, stdout.decode(errors="replace") if stdout else ""


async def _run_script(workspace: str, script: str, *, sandbox: Sandbox) -> tuple[int, str]:
    return await _run_unprivileged(("/bin/bash", "-c", script), workspace, sandbox)


async def _kill_unprivileged(sandbox: Sandbox) -> None:
    """Kill every process the sandbox user still has (a daemonised install script, a stray watcher).

    ``kill -KILL -1`` from a process running as that user signals every process
    the user owns except itself. Runs before the root-owned git steps of
    ``fix`` so nothing repo-controlled is alive while the token is in use.
    """
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-c",
        "kill -KILL -1 2>/dev/null; exit 0",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=_sandbox_env(sandbox),
        user=sandbox.uid,
        group=sandbox.gid,
        extra_groups=[],
    )
    await proc.wait()


async def _strip_remote_token(workspace: str, repo: str, check_type: str, token: str) -> GateResult | None:
    """Reset ``origin``'s URL to a token-less form before any repo-controlled code runs.

    ``clone_repo`` embeds the token in ``origin``'s URL so `git fetch`/`git
    diff` can authenticate; once the last authenticated git call
    (``changed_files``, for lint) has run, an install script, the linter or
    the test suite could otherwise read that URL straight out of
    ``.git/config``. Returns a fail :class:`GateResult` when the reset itself
    fails (fail-closed); ``None`` on success, including when there is no git
    repository to scrub (cloning never actually happened, e.g. a clone
    failure already returned earlier, or a caller that skipped it in a test).

    Only called inside the gate container: the cloud host path
    (``handlers._run_on_host``) must behave exactly as it did before this
    existed (global constraint: non-desktop behaviour is unchanged), so it is
    never called from :func:`plan_gate`, which the cloud path shares.
    """
    if not (Path(workspace) / ".git").exists():
        return None
    proc = await asyncio.create_subprocess_exec(
        "git",
        "remote",
        "set-url",
        "origin",
        f"https://github.com/{repo}.git",
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()[:300]
        message = f"{check_type} failed (could not remove the token from the git remote): {detail}"
        return GateResult(condition="fail", message=scrub_secret(message, token))
    return None


async def plan_gate(
    check_type: GateCommand, *, repo: str, branch: str, base_branch: str, token: str, workspace: str
) -> GatePlan | GateResult:
    """Clone ``branch`` into ``workspace`` and scope the commands for ``check_type``.

    Returns a :class:`GateResult` directly when the gate is already decided —
    a clone failure, an undetectable stack, an uncomputable diff, or (for
    lint and fix) nothing relevant to run — otherwise the :class:`GatePlan`.

    This is the single place that clones and scopes a gate: both the cloud
    path (which then runs the plan natively via ``_run_on_host``) and the
    gate container's :func:`run_gate` / :func:`run_fix` call this function
    so the clone/detect/scope logic exists exactly once. ``fix`` is only
    ever planned inside the gate container.
    """
    label = _label(check_type)
    try:
        await clone_repo(repo, branch, workspace, token=token or None)
    except RuntimeError as exc:
        return GateResult(condition="fail", message=scrub_secret(f"{label} failed (clone failed): {exc}", token))

    stack = detect_stack(Path(workspace))
    if stack.name == "unknown":
        return GateResult(
            condition="fail",
            message=(
                f"{label} failed — could not detect the project stack for {repo} "
                "(no pyproject.toml/package.json/go.mod/Cargo.toml/pom.xml found)"
            ),
        )

    if check_type == "tests":
        return GatePlan(stack=stack, commands=(CheckCommand(argv=tuple(stack.test_command)),))

    try:
        changed = await changed_files(workspace, base_branch)
        plan = (plan_fix if check_type == "fix" else plan_lint)(stack, Path(workspace), changed)
    except LintScopeError as exc:
        message = f"{label} failed — could not determine the files changed against {base_branch}: {exc}"
        return GateResult(condition="fail", message=scrub_secret(message, token))
    if not plan.commands:
        if check_type == "fix":
            return GateResult(condition="pass", message=f"fix_lint: nothing to auto-fix ({plan.skip_reason})")
        return GateResult(condition="pass", message=f"lint passed — {plan.skip_reason}")
    return GatePlan(stack=stack, commands=plan.commands, changed=tuple(changed))


async def run_gate(
    check_type: CheckType, *, repo: str, branch: str, base_branch: str, token: str, workspace: str
) -> GateResult:
    """Clone ``branch`` into ``workspace`` and run the ``check_type`` gate there as the unprivileged user."""
    planned = await plan_gate(
        check_type, repo=repo, branch=branch, base_branch=base_branch, token=token, workspace=workspace
    )
    if isinstance(planned, GateResult):
        return planned

    # From here on repo-controlled code runs *inside this container*: make sure
    # none of it can read the token back out of `.git/config`, and hand it to
    # the unprivileged user so it cannot read this process's memory either.
    # One consequence: a repo whose install step genuinely needs GITHUB_TOKEN
    # (e.g. to authenticate to GitHub Packages) fails its *local* gate — a
    # deliberate fail-closed trade-off, not a bug.
    scrub_result = await _strip_remote_token(workspace, repo, check_type, token)
    if scrub_result is not None:
        return scrub_result
    sandbox = _prepare_sandbox(workspace, check_type)
    if isinstance(sandbox, GateResult):
        return sandbox

    script = to_shell_script(planned.commands, install_script(planned.stack))
    returncode, output = await _run_script(workspace, script, sandbox=sandbox)
    passed = returncode == 0
    return GateResult(
        condition="pass" if passed else "fail",
        message=f"{check_type} {'passed' if passed else 'failed'}",
        output=scrub_secret(output, token)[:GATE_OUTPUT_LIMIT],
    )


# ---------------------------------------------------------------------------
# fix: deterministic auto-fix, committed and pushed from inside the gate
# ---------------------------------------------------------------------------


def _isolated_git_env(private_home: str, token: str | None = None) -> dict[str, str]:
    """Environment for a root-owned git call: no system/global config, and the token only when pushing."""
    env = env_without_github_tokens()
    env["HOME"] = private_home
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credentials}"
    return env


async def _isolated_git(
    git_dir: str, workspace: str, private_home: str, *args: str, token: str | None = None
) -> tuple[int, str, str]:
    """Run git as root against the private git directory; hooks and fsmonitor are always off."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"safe.directory={workspace}",
        f"--git-dir={git_dir}",
        f"--work-tree={workspace}",
        *args,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_isolated_git_env(private_home, token),
    )
    stdout, stderr = await proc.communicate()
    returncode = proc.returncode if proc.returncode is not None else 1
    return returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _isolate_git_dir(workspace: str, private_dir: str) -> str | GateResult:
    """Move ``workspace/.git`` into ``private_dir`` (root-only) so repo-controlled code cannot touch it."""
    source = Path(workspace) / ".git"
    target = Path(private_dir) / "git"
    try:
        os.chmod(private_dir, 0o700)
        shutil.move(str(source), str(target))
    except OSError as exc:
        return GateResult(condition="fail", message=f"fix_lint failed (could not isolate the git directory: {exc})")
    return str(target)


async def run_fix(
    *,
    repo: str,
    branch: str,
    base_branch: str,
    token: str,
    workspace: str,
    private_dir: str,
    author_name: str,
    author_email: str,
) -> GateResult:
    """Auto-fix only the files the branch changed, then commit and push them from inside the gate.

    Mirrors the cloud ``handle_fix_lint``: fixers run on the operative's
    changed files only, any fix outside them is reverted, and nothing is
    committed when the fixers changed nothing. A ``pass`` here means "the
    node finished" (including "nothing to fix"); every error is a ``fail``.
    """
    planned = await plan_gate(
        "fix", repo=repo, branch=branch, base_branch=base_branch, token=token, workspace=workspace
    )
    if isinstance(planned, GateResult):
        return planned

    scrub_result = await _strip_remote_token(workspace, repo, "fix_lint", token)
    if scrub_result is not None:
        return scrub_result
    git_dir = _isolate_git_dir(workspace, private_dir)
    if isinstance(git_dir, GateResult):
        return git_dir
    sandbox = _prepare_sandbox(workspace, "fix_lint")
    if isinstance(sandbox, GateResult):
        return sandbox

    outputs: list[str] = []
    install = install_script(planned.stack) if planned.stack.name in ("node-npm", "node-pnpm") else None
    try:
        if install is not None:
            # Best effort, exactly like the cloud handler: a failed install
            # leaves the fixer to report what it cannot resolve.
            await _run_script(workspace, install, sandbox=sandbox)
        for command in planned.commands:
            _, output = await _run_unprivileged(command.argv, os.path.join(workspace, command.cwd), sandbox)
            outputs.append(output)
    finally:
        # Nothing repo-controlled may still be running once root git holds the token.
        await _kill_unprivileged(sandbox)
    fix_output = scrub_secret("\n".join(outputs), token)[:_FIX_OUTPUT_LIMIT]

    def fail(step: str, detail: str) -> GateResult:
        return GateResult(
            condition="fail",
            message=scrub_secret(f"fix_lint failed ({step}): {detail[:300]}", token),
            output=fix_output,
        )

    rc, status_out, status_err = await _isolated_git(git_dir, workspace, private_dir, "status", "--porcelain", "-z")
    if rc != 0:
        return fail("git status failed", status_err)
    scope = set(planned.changed)
    modified = [entry[3:] for entry in status_out.split("\0") if len(entry) > 3]
    in_scope = sorted(set(modified) & scope)
    out_of_scope = sorted(set(modified) - scope)
    if out_of_scope:
        rc, _, err = await _isolated_git(git_dir, workspace, private_dir, "checkout", "--", *out_of_scope)
        if rc != 0:
            return fail("could not revert out-of-scope fixes", err)
    if not in_scope:
        return GateResult(condition="pass", message="fix_lint: auto-fix made no changes", output=fix_output)

    steps: list[tuple[str, tuple[str, ...], str | None]] = [
        ("add", ("add", "--", *in_scope), None),
        (
            "commit",
            ("-c", f"user.name={author_name}", "-c", f"user.email={author_email}")
            + ("commit", "-m", "style: auto-fix lint issues"),
            None,
        ),
        ("push", ("push", "origin", f"HEAD:refs/heads/{branch}"), token or None),
    ]
    for name, args, step_token in steps:
        rc, _, err = await _isolated_git(git_dir, workspace, private_dir, *args, token=step_token)
        if rc != 0:
            return fail(f"git {name} failed", err)
    return GateResult(condition="pass", message="fix_lint: auto-fixed lint issues and pushed", output=fix_output)


def parse_gate_result(stdout: str) -> GateResult | None:
    """The result from the last marker line, or ``None`` when absent or unparsable."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(GATE_RESULT_MARKER):
            try:
                return GateResult.model_validate_json(line[len(GATE_RESULT_MARKER) :])
            except ValidationError:
                return None
    return None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ci_gate", description="Run a Henchmen CI gate inside the operative image")
    parser.add_argument("check", choices=("lint", "tests", "fix"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--author-name", default="Henchmen")
    parser.add_argument("--author-email", default="henchmen@users.noreply.github.com")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, token: str, workspace: str, private_dir: str) -> GateResult:
    if args.check == "fix":
        return await run_fix(
            repo=args.repo,
            branch=args.branch,
            base_branch=args.base,
            token=token,
            workspace=workspace,
            private_dir=private_dir,
            author_name=args.author_name,
            author_email=args.author_email,
        )
    return await run_gate(
        args.check, repo=args.repo, branch=args.branch, base_branch=args.base, token=token, workspace=workspace
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point inside the gate container; exit code 0 only for a pass."""
    args = _parse_args(argv)
    token = read_token_from_stdin()
    label = _label(args.check)
    workspace = tempfile.mkdtemp(prefix=f"henchmen-gate-{args.check}-")
    private_dir = tempfile.mkdtemp(prefix="henchmen-gate-private-")
    try:
        result = asyncio.run(_run(args, token, workspace, private_dir))
    except Exception as exc:
        result = GateResult(condition="fail", message=scrub_secret(f"{label} failed (error: {exc})", token))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(private_dir, ignore_errors=True)
    sys.stdout.write(f"{GATE_RESULT_MARKER}{result.model_dump_json()}\n")
    sys.stdout.flush()
    return 0 if result.condition == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GATE_OUTPUT_LIMIT",
    "GATE_RESULT_MARKER",
    "UNPRIVILEGED_GID",
    "UNPRIVILEGED_UID",
    "GatePlan",
    "GateResult",
    "Sandbox",
    "env_without_github_tokens",
    "install_script",
    "main",
    "parse_gate_result",
    "plan_gate",
    "read_token_from_stdin",
    "run_fix",
    "run_gate",
    "scrub_secret",
]
