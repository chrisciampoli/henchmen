"""Detect project conventions from workspace files.

Scans configuration files (pyproject.toml, package.json) and samples source
files to detect naming conventions, indentation style, test frameworks, lint
configurations, and other project conventions. The detected conventions are
injected into the operative's system prompt so generated code matches the
project's existing style.
"""

import json
import logging
import os
import re

from pydantic import Field

from henchmen.models._base import StrictBase

logger = logging.getLogger(__name__)

# Maximum source files to sample for convention detection
_MAX_SAMPLE_FILES = 10

# Extensions to sample for style detection
_SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java"}


class RepoConventions(StrictBase):
    """Detected conventions for a repository."""

    test_framework: str | None = Field(default=None, description="Detected test framework: pytest, jest, mocha, etc.")
    import_style: str | None = Field(default=None, description="Import style: absolute, relative")
    error_handling: str | None = Field(
        default=None, description="Error handling pattern: try/except, Result type, etc."
    )
    type_system: str | None = Field(default=None, description="Type checking system: mypy, typescript strict, etc.")
    naming_convention: str | None = Field(default=None, description="Naming convention: snake_case, camelCase")
    indentation: str | None = Field(default=None, description="Indentation style: 2-space, 4-space, tabs")
    lint_config: str | None = Field(default=None, description="Lint tool: ruff, eslint, flake8, etc.")
    package_manager: str | None = Field(default=None, description="Package manager: pip, pnpm, npm, poetry, etc.")


def detect_conventions(workspace_dir: str) -> RepoConventions:
    """Detect project conventions by scanning config files and source samples.

    This function never raises — any individual detection failure is logged
    and skipped so the dossier pipeline is never blocked.
    """
    conventions = RepoConventions()

    try:
        _detect_from_pyproject(workspace_dir, conventions)
    except Exception as exc:
        logger.debug("pyproject.toml detection failed: %s", exc)

    try:
        _detect_from_package_json(workspace_dir, conventions)
    except Exception as exc:
        logger.debug("package.json detection failed: %s", exc)

    try:
        _detect_from_source_files(workspace_dir, conventions)
    except Exception as exc:
        logger.debug("Source file detection failed: %s", exc)

    return conventions


def _detect_from_pyproject(workspace_dir: str, conventions: RepoConventions) -> None:
    """Detect conventions from pyproject.toml."""
    pyproject_path = os.path.join(workspace_dir, "pyproject.toml")
    if not os.path.exists(pyproject_path):
        return

    with open(pyproject_path, encoding="utf-8") as fh:
        content = fh.read()

    # Test framework detection
    if "[tool.pytest" in content or "pytest" in content.lower():
        conventions.test_framework = conventions.test_framework or "pytest"

    # Lint config detection
    if "[tool.ruff" in content:
        conventions.lint_config = "ruff"
    elif "[tool.flake8" in content:
        conventions.lint_config = "flake8"
    elif "[tool.pylint" in content:
        conventions.lint_config = "pylint"

    # Type system detection
    if "[tool.mypy" in content or "[mypy" in content:
        conventions.type_system = conventions.type_system or "mypy"
        if "strict" in content.lower() and "true" in content.lower():
            conventions.type_system = "mypy-strict"

    # Package manager from build system
    if "[tool.poetry" in content:
        conventions.package_manager = conventions.package_manager or "poetry"
    elif "hatchling" in content or "[tool.hatch" in content:
        conventions.package_manager = conventions.package_manager or "hatch"
    elif "setuptools" in content:
        conventions.package_manager = conventions.package_manager or "pip"


def _detect_from_package_json(workspace_dir: str, conventions: RepoConventions) -> None:
    """Detect conventions from package.json."""
    pkg_path = os.path.join(workspace_dir, "package.json")
    if not os.path.exists(pkg_path):
        return

    with open(pkg_path, encoding="utf-8") as fh:
        try:
            pkg = json.load(fh)
        except json.JSONDecodeError:
            return

    deps = {}
    deps.update(pkg.get("dependencies", {}))
    deps.update(pkg.get("devDependencies", {}))

    # Test framework
    if "jest" in deps:
        conventions.test_framework = conventions.test_framework or "jest"
    elif "mocha" in deps:
        conventions.test_framework = conventions.test_framework or "mocha"
    elif "vitest" in deps:
        conventions.test_framework = conventions.test_framework or "vitest"

    # Lint config
    if "eslint" in deps:
        conventions.lint_config = conventions.lint_config or "eslint"
    if "prettier" in deps:
        existing = conventions.lint_config or ""
        if "prettier" not in existing:
            conventions.lint_config = f"{existing}+prettier".lstrip("+") if existing else "prettier"

    # Type system
    if "typescript" in deps:
        conventions.type_system = conventions.type_system or "typescript"
        # Check for strict mode in tsconfig
        tsconfig_path = os.path.join(workspace_dir, "tsconfig.json")
        if os.path.exists(tsconfig_path):
            try:
                with open(tsconfig_path, encoding="utf-8") as fh:
                    tsconfig_text = fh.read()
                if '"strict": true' in tsconfig_text or '"strict":true' in tsconfig_text:
                    conventions.type_system = "typescript-strict"
            except Exception:
                pass

    # Package manager
    scripts = pkg.get("scripts", {})
    if any("pnpm" in str(v) for v in scripts.values()) or os.path.exists(os.path.join(workspace_dir, "pnpm-lock.yaml")):
        conventions.package_manager = conventions.package_manager or "pnpm"
    elif os.path.exists(os.path.join(workspace_dir, "yarn.lock")):
        conventions.package_manager = conventions.package_manager or "yarn"
    elif os.path.exists(os.path.join(workspace_dir, "package-lock.json")):
        conventions.package_manager = conventions.package_manager or "npm"


def _detect_from_source_files(workspace_dir: str, conventions: RepoConventions) -> None:
    """Sample source files to detect naming and indentation conventions."""
    source_files = _find_source_files(workspace_dir, max_files=_MAX_SAMPLE_FILES)
    if not source_files:
        return

    indent_counts: dict[str, int] = {"2-space": 0, "4-space": 0, "tabs": 0}
    naming_counts: dict[str, int] = {"snake_case": 0, "camelCase": 0}
    import_counts: dict[str, int] = {"absolute": 0, "relative": 0}

    for file_path in source_files:
        try:
            with open(file_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read(8000)  # Sample first 8KB
        except Exception:
            continue

        # Indentation detection
        for line in content.split("\n")[:100]:
            if line.startswith("\t"):
                indent_counts["tabs"] += 1
            elif line.startswith("    "):
                indent_counts["4-space"] += 1
            elif line.startswith("  ") and not line.startswith("   "):
                indent_counts["2-space"] += 1

        # Naming convention detection (function/variable definitions)
        ext = os.path.splitext(file_path)[1]
        if ext == ".py":
            # Python: check for snake_case function defs
            snake_matches = re.findall(r"def [a-z][a-z0-9_]+\(", content)
            camel_matches = re.findall(r"def [a-z][a-zA-Z0-9]+\(", content)
            naming_counts["snake_case"] += len(snake_matches)
            naming_counts["camelCase"] += max(0, len(camel_matches) - len(snake_matches))

            # Import style
            abs_imports = re.findall(r"^from [a-zA-Z]", content, re.MULTILINE)
            rel_imports = re.findall(r"^from \.", content, re.MULTILINE)
            import_counts["absolute"] += len(abs_imports)
            import_counts["relative"] += len(rel_imports)
        elif ext in (".js", ".ts", ".jsx", ".tsx"):
            # JS/TS: check for camelCase function defs
            camel_matches = re.findall(r"(?:function|const|let|var)\s+[a-z][a-zA-Z0-9]+", content)
            snake_matches = re.findall(r"(?:function|const|let|var)\s+[a-z][a-z0-9_]+_[a-z]", content)
            naming_counts["camelCase"] += max(0, len(camel_matches) - len(snake_matches))
            naming_counts["snake_case"] += len(snake_matches)

    # Set conventions from counts
    if indent_counts:
        winner = max(indent_counts, key=lambda k: indent_counts[k])
        if indent_counts[winner] > 0:
            conventions.indentation = conventions.indentation or winner

    if naming_counts:
        winner = max(naming_counts, key=lambda k: naming_counts[k])
        if naming_counts[winner] > 0:
            conventions.naming_convention = conventions.naming_convention or winner

    if import_counts:
        winner = max(import_counts, key=lambda k: import_counts[k])
        if import_counts[winner] > 0:
            conventions.import_style = conventions.import_style or winner


def _find_source_files(workspace_dir: str, max_files: int = 10) -> list[str]:
    """Find source files in the workspace, skipping noisy directories."""
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", ".tox", ".mypy_cache", "dist", "build"}
    found: list[str] = []

    for root, dirs, files in os.walk(workspace_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            ext = os.path.splitext(fname)[1]
            if ext in _SOURCE_EXTENSIONS:
                found.append(os.path.join(root, fname))
                if len(found) >= max_files:
                    return found

    return found


def conventions_to_prompt(conventions: RepoConventions) -> str:
    """Format detected conventions as a system prompt section.

    Returns an empty string if no conventions were detected so the caller
    can skip injection entirely.
    """
    lines: list[str] = []

    if conventions.test_framework:
        lines.append(f"- Test framework: {conventions.test_framework}")
    if conventions.lint_config:
        lines.append(f"- Linter: {conventions.lint_config}")
    if conventions.type_system:
        lines.append(f"- Type checking: {conventions.type_system}")
    if conventions.naming_convention:
        lines.append(f"- Naming convention: {conventions.naming_convention}")
    if conventions.indentation:
        lines.append(f"- Indentation: {conventions.indentation}")
    if conventions.import_style:
        lines.append(f"- Import style: {conventions.import_style}")
    if conventions.package_manager:
        lines.append(f"- Package manager: {conventions.package_manager}")
    if conventions.error_handling:
        lines.append(f"- Error handling: {conventions.error_handling}")

    if not lines:
        return ""

    return "## Detected Project Conventions\n\nFollow these conventions in all code you write:\n" + "\n".join(lines)
