"""Test runner tools - supports both Python and Node.js/TypeScript projects.

In monorepos (pnpm + turbo), lint and test commands are scoped to packages
that have changes relative to ``origin/main`` so the operative is not
penalised for pre-existing issues in unrelated packages.
"""

import asyncio
import logging
import os
from typing import Any

from henchmen.arsenal.registry import tool

logger = logging.getLogger(__name__)


async def _run_subprocess(*args: str, working_dir: str = "") -> dict[str, Any]:
    """Run an arbitrary subprocess and capture output."""
    kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if working_dir:
        kwargs["cwd"] = working_dir
    proc = await asyncio.create_subprocess_exec(*args, **kwargs)
    stdout, stderr = await proc.communicate()
    return {
        "stdout": stdout.decode("utf-8"),
        "stderr": stderr.decode("utf-8"),
        "return_code": proc.returncode,
        "success": proc.returncode == 0,
    }


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
    """Detect changed packages in a monorepo via ``git diff --name-only origin/main``.

    Returns a list of package-relative paths (e.g. ``["./apps/api"]``) suitable
    for ``pnpm turbo run --filter``.  Returns an empty list when detection fails
    or changes span the root (meaning we should fall back to linting everything).
    """
    wd = working_dir or "."
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--name-only",
            "origin/main",
            cwd=wd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        diff_out, _ = await proc.communicate()
        if proc.returncode != 0:
            return []
        changed_files = [f.strip() for f in diff_out.decode().strip().split("\n") if f.strip()]
    except Exception:
        return []

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
    to ``origin/main`` so unrelated test failures don't block the operative.
    """
    project_type = _detect_project_type(working_dir or ".")

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
    to ``origin/main`` so unrelated lint failures don't block the operative.
    """
    project_type = _detect_project_type(working_dir or ".")

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
    """Run type checker. Auto-detects Python vs Node.js/TypeScript."""
    project_type = _detect_project_type(working_dir or ".")

    if project_type == "node":
        if os.path.exists(os.path.join(working_dir or ".", "pnpm-lock.yaml")):
            cmd = ["pnpm", "run", "typecheck"]
        else:
            cmd = ["npx", "tsc", "--noEmit"]
    else:
        cmd = ["python", "-m", "mypy", path]

    result = await _run_subprocess(*cmd, working_dir=working_dir)
    result["command"] = " ".join(cmd)
    result["project_type"] = project_type
    return result
