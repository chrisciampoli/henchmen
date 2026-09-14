"""Test runner tools - supports both Python and Node.js/TypeScript projects.

In monorepos (pnpm + turbo), lint and test commands are scoped to packages
that have changes relative to the repository's default branch so the
operative is not penalised for pre-existing issues in unrelated packages.

Fail-closed: a project whose stack cannot be detected gets an explicit
``unsupported project type`` failure, never a Python toolchain run that
reports on a repository it does not understand.
"""

import logging
import os
from typing import Any

from henchmen.arsenal._process import DEFAULT_TIMEOUT_SECONDS, TEST_TIMEOUT_SECONDS, run_command, subprocess_env
from henchmen.arsenal._workspace import ensure_in_workspace
from henchmen.arsenal.registry import tool

logger = logging.getLogger(__name__)


async def _run_subprocess(
    *args: str,
    working_dir: str = "",
    timeout_seconds: float = TEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run an arbitrary subprocess and capture output.

    Delegates to :func:`henchmen.arsenal._process.run_command` so a test or
    lint command that never exits is killed and reported as a failure instead
    of holding the operative until its wall clock expires. ``CI=1`` is pinned
    because many Node test scripts drop into watch mode when it is unset.
    """
    return await run_command(
        *args,
        cwd=working_dir,
        timeout_seconds=timeout_seconds,
        env=subprocess_env(CI="1"),
    )


def _unsupported(project_type: str, working_dir: str) -> dict[str, Any]:
    """Fail-closed payload for a repository whose stack is not recognised."""
    return {
        "error": (
            f"unsupported project type in {working_dir or os.getcwd()}: no package.json, "
            "pyproject.toml or setup.py found"
        ),
        "success": False,
        "return_code": -1,
        "project_type": project_type,
    }


def _resolve_working_dir(working_dir: str) -> tuple[str, dict[str, Any] | None]:
    """Workspace-check ``working_dir``; an empty value means the current directory."""
    if not working_dir:
        return "", None
    try:
        return ensure_in_workspace(working_dir), None
    except PermissionError as exc:
        return "", {"error": f"access denied: {exc}", "success": False}


def _detect_project_type(working_dir: str) -> str:
    """Detect whether the project is Python or Node.js based on files present."""
    if not working_dir:
        working_dir = os.getcwd()
    if os.path.exists(os.path.join(working_dir, "package.json")):
        return "node"
    if os.path.exists(os.path.join(working_dir, "pyproject.toml")) or os.path.exists(
        os.path.join(working_dir, "setup.py")
    ):
        return "python"
    return "unknown"


def _is_monorepo(working_dir: str) -> bool:
    """Check if the workspace is a pnpm + turbo monorepo."""
    wd = working_dir or "."
    return os.path.exists(os.path.join(wd, "pnpm-lock.yaml")) and os.path.exists(os.path.join(wd, "turbo.json"))


async def _get_affected_packages(working_dir: str) -> list[str]:
    """Detect changed packages in a monorepo via ``git diff --name-only <base ref>``.

    The base ref is the repository's own default branch (``origin/master`` on
    a master-based repo); hard-coding ``origin/main`` made the diff fail and
    silently fall back to linting everything.

    Returns a list of package-relative paths (e.g. ``["./apps/api"]``) suitable
    for ``pnpm turbo run --filter``.  Returns an empty list when detection fails
    or changes span the root (meaning we should fall back to linting everything).
    """
    wd = working_dir or "."
    # Imported lazily: ``henchmen.operative`` pulls in the whole agent runtime,
    # which Arsenal (a dependency of that runtime) must not load at import time.
    from henchmen.operative.git_helpers import detect_base_ref

    base_ref = await detect_base_ref(wd)
    result = await run_command("git", "diff", "--name-only", base_ref, cwd=wd, timeout_seconds=DEFAULT_TIMEOUT_SECONDS)
    if not result["success"]:
        return []
    changed_files = [f.strip() for f in str(result["stdout"]).strip().split("\n") if f.strip()]

    if not changed_files:
        return []

    packages: set[str] = set()
    for f in changed_files:
        parts = f.split("/")
        if len(parts) >= 2 and parts[0] in ("apps", "packages"):
            packages.add(f"./{parts[0]}/{parts[1]}")
        else:
            # File at root or unknown directory — can't scope, run everything
            return []

    return sorted(packages)


@tool(
    name="run_tests",
    category="test_runner",
    description=(
        "Run tests. Auto-detects project type: uses pytest for Python, pnpm/npm test for Node.js. "
        "In monorepos, scopes to affected packages so pre-existing failures don't block."
    ),
)
async def run_tests(test_path: str = ".", args: list[str] | None = None, working_dir: str = "") -> dict[str, Any]:
    """Execute tests and return the results. Auto-detects Python vs Node.js.

    For pnpm+turbo monorepos, scopes tests to packages with changes relative
    to the default branch so unrelated test failures don't block the operative.
    """
    working_dir, denied = _resolve_working_dir(working_dir)
    if denied:
        return denied
    project_type = _detect_project_type(working_dir or ".")
    if project_type == "unknown":
        return _unsupported(project_type, working_dir)

    if project_type == "node":
        wd = working_dir or "."
        if _is_monorepo(wd):
            affected = await _get_affected_packages(wd)
            if affected:
                filter_args: list[str] = []
                for pkg in affected:
                    filter_args.extend(["--filter", pkg])
                cmd = ["pnpm", "turbo", "run", "test", *filter_args]
                logger.info("Scoping tests to affected packages: %s", affected)
            else:
                cmd = ["pnpm", "run", "test"]
        elif os.path.exists(os.path.join(wd, "pnpm-lock.yaml")):
            cmd = ["pnpm", "run", "test"]
        else:
            cmd = ["npm", "test"]
        if test_path != ".":
            cmd.extend(["--", test_path])
    else:
        cmd = ["python", "-m", "pytest", test_path, "-v"]

    if args:
        cmd.extend(args)
    result = await _run_subprocess(*cmd, working_dir=working_dir)
    result["command"] = " ".join(cmd)
    result["project_type"] = project_type
    return result


@tool(
    name="run_lint",
    category="test_runner",
    description=(
        "Run linter. Auto-detects project type: uses ruff for Python, eslint for Node.js. "
        "In monorepos, scopes to affected packages so pre-existing lint failures don't block. "
        "Set fix=True to auto-fix."
    ),
)
async def run_lint(path: str = ".", fix: bool = False, working_dir: str = "") -> dict[str, Any]:
    """Run linter; optionally auto-fix issues. Auto-detects Python vs Node.js.

    For pnpm+turbo monorepos, scopes lint to packages with changes relative
    to the default branch so unrelated lint failures don't block the operative.
    """
    working_dir, denied = _resolve_working_dir(working_dir)
    if denied:
        return denied
    project_type = _detect_project_type(working_dir or ".")
    if project_type == "unknown":
        return _unsupported(project_type, working_dir)

    if project_type == "node":
        wd = working_dir or "."
        if _is_monorepo(wd):
            affected = await _get_affected_packages(wd)
            if affected:
                filter_args: list[str] = []
                for pkg in affected:
                    filter_args.extend(["--filter", pkg])
                cmd = ["pnpm", "turbo", "run", "lint", *filter_args]
                logger.info("Scoping lint to affected packages: %s", affected)
            else:
                # Can't determine affected packages — fall back to full lint
                cmd = ["pnpm", "run", "lint"]
        elif os.path.exists(os.path.join(wd, "pnpm-lock.yaml")):
            cmd = ["pnpm", "run", "lint"]
        else:
            cmd = ["npx", "eslint", path]
        if fix:
            cmd.append("--fix")
    else:
        cmd = ["python", "-m", "ruff", "check", path]
        if fix:
            cmd.append("--fix")

    result = await _run_subprocess(*cmd, working_dir=working_dir)
    result["command"] = " ".join(cmd)
    result["project_type"] = project_type
    return result


@tool(
    name="type_check",
    category="test_runner",
    description="Run type checker. Auto-detects project type: uses mypy for Python, tsc for TypeScript.",
)
async def type_check(path: str = ".", working_dir: str = "") -> dict[str, Any]:
    """Run type checker. Auto-detects Python vs Node.js/TypeScript.

    For TypeScript a non-default ``path`` is handed to ``tsc -p`` (a directory
    containing a tsconfig, or the tsconfig file itself) instead of being
    silently ignored.
    """
    working_dir, denied = _resolve_working_dir(working_dir)
    if denied:
        return denied
    project_type = _detect_project_type(working_dir or ".")
    if project_type == "unknown":
        return _unsupported(project_type, working_dir)

    if project_type == "node":
        if path not in ("", "."):
            cmd = ["npx", "tsc", "--noEmit", "-p", path]
        elif os.path.exists(os.path.join(working_dir or ".", "pnpm-lock.yaml")):
            cmd = ["pnpm", "run", "typecheck"]
        else:
            cmd = ["npx", "tsc", "--noEmit"]
    else:
        cmd = ["python", "-m", "mypy", path]

    result = await _run_subprocess(*cmd, working_dir=working_dir)
    result["command"] = " ".join(cmd)
    result["project_type"] = project_type
    return result
