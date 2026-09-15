"""Run one local CI gate (lint or tests) inside the operative image.

In local mode the Mastermind may itself run in a container, so a host path it
creates cannot be bind-mounted into a sibling container. Instead the gate
container runs this module: it clones the task branch itself, computes the
diff against ``origin/<base>`` in its own workspace, runs the scoped commands
(the same :mod:`~henchmen.mastermind.scheme_executor.lint_scope` plan the
cloud path uses) and prints one result line the Mastermind parses.

:func:`plan_gate` is the single place that clones and scopes a gate — it is
used both by this module's :func:`run_gate` (inside the gate container) and
by the Mastermind's cloud path (:func:`henchmen.mastermind.scheme_executor.
handlers._run_ci_check`), so the clone/detect/scope logic exists exactly
once. The two paths differ only in how they *execute* the scoped commands:
the cloud path runs them natively on the host via ``_run_on_host``, the gate
container renders them to a shell script and runs it via ``_run_script``.

Fail-closed: a clone failure, an undetectable stack, an uncomputable diff, a
non-zero exit code or any unexpected error is a ``fail`` result, and the
process exits 0 only for a ``pass``.

Usage inside the operative image::

    python -m henchmen.mastermind.scheme_executor.ci_gate lint --repo=owner/name --branch=henchmen/x --base=main

The GitHub token arrives as ``HENCHMEN_GITHUB_TOKEN`` and is read through
``Settings``; it is never passed on the command line.
"""

from __future__ import annotations

import argparse
import asyncio
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
    plan_lint,
    to_shell_script,
)
from henchmen.utils.git import clone_repo
from henchmen.utils.redaction import redact
from henchmen.utils.stack_detector import Stack, detect_stack

GATE_RESULT_MARKER = "HENCHMEN_GATE_RESULT "
_OUTPUT_LIMIT = 5000

# Every environment variable a GitHub token could plausibly travel under.
# Repo-controlled code (an install script, the linter, the test suite, a
# `gh` CLI invocation) runs as a subprocess of this process and inherits its
# whole environment unless these are stripped from a copy of it first.
_TOKEN_ENV_VARS = ("HENCHMEN_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")

CheckType = Literal["lint", "tests"]


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


async def _strip_remote_token(workspace: str, repo: str, check_type: CheckType) -> GateResult | None:
    """Reset ``origin``'s URL to a token-less form before any repo-controlled code runs.

    ``clone_repo`` embeds the token in ``origin``'s URL so `git fetch`/`git
    diff` can authenticate; once the last authenticated git call
    (``changed_files``, for lint) has run, an install script, the linter or
    the test suite could otherwise read that URL straight out of
    ``.git/config``. Returns a fail :class:`GateResult` when the reset itself
    fails (fail-closed); ``None`` on success, including when there is no git
    repository to scrub (cloning never actually happened, e.g. a clone
    failure already returned earlier, or a caller that skipped it in a test).
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
        return GateResult(
            condition="fail",
            message=f"{check_type} failed (could not remove the token from the git remote): {detail}",
        )
    return None


async def _run_script(workspace: str, script: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-c",
        script,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env_without_github_tokens(),
    )
    stdout, _ = await proc.communicate()
    returncode = proc.returncode if proc.returncode is not None else 1
    return returncode, stdout.decode(errors="replace") if stdout else ""


async def plan_gate(
    check_type: CheckType, *, repo: str, branch: str, base_branch: str, token: str, workspace: str
) -> GatePlan | GateResult:
    """Clone ``branch`` into ``workspace`` and scope the commands for ``check_type``.

    Returns a :class:`GateResult` directly when the gate is already decided —
    a clone failure, an undetectable stack, an uncomputable lint diff, or (for
    lint) no relevant changed files — otherwise the :class:`GatePlan` to run.

    This is the single place that clones and scopes a gate: both the cloud
    path (which then runs the plan natively via ``_run_on_host``) and the
    gate container's :func:`run_gate` (which renders it to a shell script)
    call this function so the clone/detect/scope logic exists exactly once.
    """
    try:
        await clone_repo(repo, branch, workspace, token=token or None)
    except RuntimeError as exc:
        return GateResult(condition="fail", message=scrub_secret(f"{check_type} failed (clone failed): {exc}", token))

    stack = detect_stack(Path(workspace))
    if stack.name == "unknown":
        return GateResult(
            condition="fail",
            message=(
                f"{check_type} failed — could not detect the project stack for {repo} "
                "(no pyproject.toml/package.json/go.mod/Cargo.toml/pom.xml found)"
            ),
        )

    if check_type == "lint":
        try:
            plan = plan_lint(stack, Path(workspace), await changed_files(workspace, base_branch))
        except LintScopeError as exc:
            message = f"lint failed — could not determine the files changed against {base_branch}: {exc}"
            return GateResult(condition="fail", message=scrub_secret(message, token))
        if not plan.commands:
            return GateResult(condition="pass", message=f"lint passed — {plan.skip_reason}")
        commands = plan.commands
    else:
        commands = (CheckCommand(argv=tuple(stack.test_command)),)

    # From here on, repo-controlled code runs (an install script, the linter,
    # the test suite) — make sure none of it can read the token back out of
    # `.git/config` first. This is the last authenticated git call either
    # branch above made (`changed_files`, for lint; `clone_repo` alone, for
    # tests), so nothing after this point still needs the token in the URL.
    scrub_result = await _strip_remote_token(workspace, repo, check_type)
    if scrub_result is not None:
        return scrub_result

    return GatePlan(stack=stack, commands=commands)


async def run_gate(
    check_type: CheckType, *, repo: str, branch: str, base_branch: str, token: str, workspace: str
) -> GateResult:
    """Clone ``branch`` into ``workspace`` and run the ``check_type`` gate there."""
    planned = await plan_gate(
        check_type, repo=repo, branch=branch, base_branch=base_branch, token=token, workspace=workspace
    )
    if isinstance(planned, GateResult):
        return planned

    script = to_shell_script(planned.commands, install_script(planned.stack))
    returncode, output = await _run_script(workspace, script)
    passed = returncode == 0
    return GateResult(
        condition="pass" if passed else "fail",
        message=f"{check_type} {'passed' if passed else 'failed'}",
        output=scrub_secret(output, token)[:_OUTPUT_LIMIT],
    )


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
    parser.add_argument("check", choices=("lint", "tests"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--base", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point inside the gate container; exit code 0 only for a pass."""
    from henchmen.config.settings import Settings

    args = _parse_args(argv)
    token = Settings(_env_file=None, provider="local").github_token  # type: ignore[call-arg]
    workspace = tempfile.mkdtemp(prefix=f"henchmen-gate-{args.check}-")
    try:
        result = asyncio.run(
            run_gate(
                args.check, repo=args.repo, branch=args.branch, base_branch=args.base, token=token, workspace=workspace
            )
        )
    except Exception as exc:
        result = GateResult(condition="fail", message=scrub_secret(f"{args.check} failed (error: {exc})", token))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    sys.stdout.write(f"{GATE_RESULT_MARKER}{result.model_dump_json()}\n")
    sys.stdout.flush()
    return 0 if result.condition == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GATE_RESULT_MARKER",
    "GatePlan",
    "GateResult",
    "env_without_github_tokens",
    "install_script",
    "main",
    "parse_gate_result",
    "plan_gate",
    "run_gate",
    "scrub_secret",
]
