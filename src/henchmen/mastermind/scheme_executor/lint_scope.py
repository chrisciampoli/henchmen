"""Scope the Mastermind lint gate to the files the operative changed.

A lint gate must judge the operative's work, never violations that already
existed on the base branch: running ``ruff check .`` or a project-wide
``npm run lint`` against a repo with pre-existing warnings escalated every task
regardless of what the operative wrote.

:func:`changed_files` lists what the branch changed relative to
``origin/<base>`` (three-dot, so commits that landed on the base branch after
the operative branched are not attributed to it) and raises
:class:`LintScopeError` when that cannot be computed — the caller fails the
gate closed. :func:`plan_lint` turns the stack and the changed files into the
commands to run:

* Python — ``ruff check`` on changed ``.py`` files.
* Node (npm/pnpm) — ``eslint`` on changed JS/TS files, run from each file's
  nearest ``package.json`` directory so per-package configs apply. A package
  with no ESLint dependency or config anywhere up to the repo root has no
  linter to run.
* Go — ``go vet`` on the packages containing changed ``.go`` files.
* Rust / Java — the linters only work on a whole crate or build, so the
  project lint runs, and only when the branch touched files of that language.

No relevant changed files means there is nothing of the operative's to lint:
the plan carries a skip reason and the gate passes with that message.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from henchmen.utils.stack_detector import Stack

_PYTHON_EXTENSIONS = frozenset({".py"})
_NODE_EXTENSIONS = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"})
_GO_EXTENSIONS = frozenset({".go"})
_RUST_EXTENSIONS = frozenset({".rs"})
_JVM_EXTENSIONS = frozenset({".java", ".kt", ".kts", ".groovy"})
_RUST_MANIFESTS = frozenset({"Cargo.toml", "Cargo.lock"})
_JVM_MANIFESTS = frozenset({"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"})
_ESLINT_CONFIGS = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    "eslint.config.mts",
    "eslint.config.cts",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yaml",
    ".eslintrc.yml",
)


class LintScopeError(RuntimeError):
    """The files changed by the operative could not be determined."""


@dataclass(frozen=True)
class CheckCommand:
    """One command of a CI check, run from ``cwd`` (relative to the workspace, POSIX separators)."""

    argv: tuple[str, ...]
    cwd: str = "."


@dataclass(frozen=True)
class LintPlan:
    """Commands that lint the operative's changes; empty with ``skip_reason`` when nothing applies."""

    commands: tuple[CheckCommand, ...] = field(default_factory=tuple)
    skip_reason: str = ""


async def changed_files(workspace: str, base_branch: str) -> list[str]:
    """Return paths the branch changed relative to ``origin/<base_branch>``.

    Raises:
        LintScopeError: the base branch could not be fetched or the diff failed.
    """
    await _git(workspace, "fetch", "origin", f"{base_branch}:refs/remotes/origin/{base_branch}")
    out = await _git(workspace, "diff", "--name-only", "--no-renames", "-z", f"origin/{base_branch}...HEAD")
    return [path for path in out.split("\0") if path]


async def _git(workspace: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()[:300]
        raise LintScopeError(f"git {args[0]} failed (rc={proc.returncode}): {detail}")
    return stdout.decode(errors="replace")


def plan_lint(stack: Stack, workspace: Path, changed: list[str]) -> LintPlan:
    """Build the lint commands for ``changed`` (paths relative to ``workspace``)."""
    existing = [path for path in changed if (workspace / path).is_file()]
    if stack.name == "python":
        # The stack lints "."; keep its command and pass the changed files instead.
        # --force-exclude keeps the project's ruff excludes for explicitly named files.
        base = [arg for arg in stack.lint_command if arg != "."] + ["--force-exclude"]
        return _files_plan(base, _with_suffix(existing, _PYTHON_EXTENSIONS), "Python")
    if stack.name in ("node-npm", "node-pnpm"):
        return _node_plan(stack, workspace, _with_suffix(existing, _NODE_EXTENSIONS))
    if stack.name == "go":
        packages = sorted({_as_arg(str(PurePosixPath(p).parent)) for p in _with_suffix(existing, _GO_EXTENSIONS)})
        if not packages:
            return LintPlan(skip_reason="no changed Go files to lint")
        return LintPlan(commands=(CheckCommand(argv=("go", "vet", *packages)),))
    if stack.name == "rust":
        return _whole_project_plan(stack, changed, _RUST_EXTENSIONS, _RUST_MANIFESTS, "Rust")
    if stack.name in ("java-maven", "java-gradle"):
        return _whole_project_plan(stack, changed, _JVM_EXTENSIONS, _JVM_MANIFESTS, "JVM")
    raise LintScopeError(f"no lint scoping rule for stack {stack.name!r}")


def plan_fix(stack: Stack, workspace: Path, changed: list[str]) -> LintPlan:
    """Auto-fix commands limited to ``changed``; empty with ``skip_reason`` when no fixer applies.

    Only Python (ruff) and Node (eslint) have deterministic auto-fixers. Other
    stacks get no command rather than the old fallback of running ruff over a
    Go, Rust or Java repository.
    """
    if stack.name not in ("python", "node-npm", "node-pnpm"):
        return LintPlan(skip_reason=f"no auto-fixer for the {stack.name} stack")
    lint = plan_lint(stack, workspace, changed)
    return LintPlan(
        commands=tuple(CheckCommand(argv=(*command.argv, "--fix"), cwd=command.cwd) for command in lint.commands),
        skip_reason=lint.skip_reason,
    )


def _with_suffix(paths: list[str], extensions: frozenset[str]) -> list[str]:
    return [path for path in paths if PurePosixPath(path).suffix in extensions]


def _as_arg(path: str) -> str:
    """Make a relative path safe to pass as an argument (never parsed as a flag)."""
    if path == "." or path.startswith(("./", "/")):
        return path
    return f"./{path}"


def _files_plan(base_argv: list[str], files: list[str], language: str) -> LintPlan:
    if not files:
        return LintPlan(skip_reason=f"no changed {language} files to lint")
    return LintPlan(commands=(CheckCommand(argv=(*base_argv, *(_as_arg(f) for f in files))),))


def _whole_project_plan(
    stack: Stack, changed: list[str], extensions: frozenset[str], manifests: frozenset[str], language: str
) -> LintPlan:
    touched = any(PurePosixPath(p).suffix in extensions or PurePosixPath(p).name in manifests for p in changed)
    if not touched:
        return LintPlan(skip_reason=f"no changed {language} files to lint")
    return LintPlan(commands=(CheckCommand(argv=tuple(stack.lint_command)),))


def _node_plan(stack: Stack, workspace: Path, files: list[str]) -> LintPlan:
    if not files:
        return LintPlan(skip_reason="no changed JavaScript/TypeScript files to lint")
    runner = ("pnpm", "exec", "eslint") if stack.name == "node-pnpm" else ("npx", "--no-install", "eslint")
    groups: dict[str, list[str]] = {}
    for path in files:
        groups.setdefault(_nearest_package_dir(workspace, path), []).append(path)
    commands: list[CheckCommand] = []
    for package_dir, group in sorted(groups.items()):
        if not _eslint_configured(workspace, package_dir):
            continue
        relative = [str(PurePosixPath(p).relative_to(package_dir)) if package_dir != "." else p for p in group]
        commands.append(CheckCommand(argv=(*runner, *(_as_arg(p) for p in relative)), cwd=package_dir))
    if not commands:
        return LintPlan(skip_reason="changed JavaScript/TypeScript files have no ESLint configured")
    return LintPlan(commands=tuple(commands))


def _ancestors(package_dir: str) -> list[str]:
    """``package_dir`` and every parent up to the workspace root (``"."``)."""
    chain = [package_dir]
    current = PurePosixPath(package_dir)
    while str(current) != ".":
        current = current.parent
        chain.append(str(current))
    return chain


def _nearest_package_dir(workspace: Path, path: str) -> str:
    current = PurePosixPath(path).parent
    while True:
        if (workspace / current / "package.json").is_file() or str(current) == ".":
            return str(current)
        current = current.parent


def _eslint_configured(workspace: Path, package_dir: str) -> bool:
    for directory in _ancestors(package_dir):
        base = workspace / directory
        if any((base / name).is_file() for name in _ESLINT_CONFIGS):
            return True
        manifest = base / "package.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise LintScopeError(f"{directory}/package.json is not readable JSON: {exc}") from exc
            if "eslintConfig" in data:
                return True
            for section in ("dependencies", "devDependencies"):
                if "eslint" in (data.get(section) or {}):
                    return True
    return False


def to_shell_script(commands: tuple[CheckCommand, ...], install: str | None) -> str:
    """Render commands for ``bash -c``: install, then every command, exiting non-zero if any failed."""
    runs = " ".join(
        f"( cd {shlex.quote(command.cwd)} && {shlex.join(command.argv)} ) || rc=$?;" for command in commands
    )
    body = f"rc=0; {runs} exit $rc"
    return f"{{ {install}; }} && {{ {body}; }}" if install else body


__all__ = ["CheckCommand", "LintPlan", "LintScopeError", "changed_files", "plan_lint", "to_shell_script"]
