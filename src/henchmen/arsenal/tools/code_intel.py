"""Code intelligence tools - read and search source code.

Every path, directory and ``working_dir`` argument is routed through
:func:`henchmen.arsenal._workspace.ensure_in_workspace`. The read side needs
the same in-tool boundary as the write side: the outer
:class:`OperativeGuardrails` check keys off argument *names*, so a tool whose
parameter is spelled differently silently escapes it.
"""

import ast
import fnmatch
import os
import re
from typing import Any

from henchmen.arsenal._process import SEARCH_TIMEOUT_SECONDS, run_command
from henchmen.arsenal._workspace import ensure_in_workspace
from henchmen.arsenal.registry import tool

# Directories never worth walking or grepping in a target repo. Without this a
# single file_search on a Node repo returns tens of thousands of node_modules
# paths, which the guardrails then truncate into a useless partial list.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        ".next",
        ".turbo",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "coverage",
        ".tox",
    }
)

_MAX_SEARCH_RESULTS = 200

# Source extensions searched by symbol_lookup. The operative image ships node
# and pnpm, so a Python-only lookup silently returns nothing on the Node/TS
# repositories Henchmen is expected to work on.
_SOURCE_GLOBS = ("*.py", "*.pyi", "*.js", "*.jsx", "*.ts", "*.tsx", "*.go", "*.rs", "*.java")

# Definition keywords across the supported languages.
_DEFINITION_KEYWORDS = (
    "class",
    "def",
    "function",
    "const",
    "let",
    "var",
    "interface",
    "type",
    "enum",
    "struct",
    "func",
    "fn",
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _safe_path(path: str) -> tuple[str, dict[str, Any] | None]:
    """Resolve ``path`` inside the workspace, or return an error payload."""
    try:
        return ensure_in_workspace(path), None
    except PermissionError as exc:
        return "", {"error": f"access denied: {exc}"}


@tool(
    name="file_read",
    category="code_intel",
    description="Read file contents, optionally sliced to a line range.",
)
async def file_read(path: str, start_line: int = 0, end_line: int | None = None) -> dict[str, Any]:
    """Read a file and return its contents with line numbers."""
    safe_path, denied = _safe_path(path)
    if denied:
        return denied
    try:
        if os.path.isdir(safe_path):
            # Model passed a directory — list contents instead of erroring
            entries = sorted(os.listdir(safe_path))
            return {"error": f"{path} is a directory, not a file. Contents: {entries[:50]}"}
        with open(safe_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        sliced = lines[start_line:end_line]
        content = "".join(sliced)
        return {
            "path": path,
            "content": content,
            "start_line": start_line,
            "end_line": end_line if end_line is not None else len(lines),
            "total_lines": len(lines),
        }
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="file_search",
    category="code_intel",
    description=(
        "Search for files matching a glob pattern under a directory. Skips node_modules, "
        ".git, dist and other build output, and returns at most 200 paths."
    ),
)
async def file_search(pattern: str, directory: str = ".", file_glob: str = "*") -> dict[str, Any]:
    """Return file paths matching a glob pattern within a directory."""
    safe_dir, denied = _safe_path(directory)
    if denied:
        return denied
    if not pattern:
        return {"error": "pattern must be a non-empty glob"}
    try:
        matched: list[str] = []
        truncated = False
        for root, dirnames, filenames in os.walk(safe_dir):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                if file_glob != "*" and not fnmatch.fnmatch(name, file_glob):
                    continue
                full = os.path.join(root, name)
                if not (fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(full, pattern)):
                    continue
                matched.append(full)
                if len(matched) >= _MAX_SEARCH_RESULTS:
                    truncated = True
                    break
            if truncated:
                break
        return {"matches": sorted(matched), "count": len(matched), "truncated": truncated}
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="symbol_lookup",
    category="code_intel",
    description=(
        "Find class, function, type and constant definitions matching a symbol name. "
        "Searches Python, JS/TS, Go, Rust and Java sources."
    ),
)
async def symbol_lookup(symbol: str, directory: str = ".", working_dir: str = "") -> dict[str, Any]:
    """Locate definitions of a symbol by name using grep."""
    if not symbol or not _IDENTIFIER_RE.fullmatch(symbol):
        return {
            "error": f"symbol must be a plain identifier, got {symbol!r}. Use grep_search for arbitrary patterns.",
            "symbol": symbol,
        }
    safe_dir, denied = _safe_path(directory)
    if denied:
        return denied
    safe_working_dir = ""
    if working_dir:
        safe_working_dir, denied = _safe_path(working_dir)
        if denied:
            return denied

    keywords = "|".join(_DEFINITION_KEYWORDS)
    # Word boundaries are spelled out in POSIX classes rather than ``\b``:
    # ``\b`` is a GNU extension that some grep builds ignore in -E mode, and a
    # code-search tool that silently returns zero matches is worse than one
    # that errors — the model concludes the symbol does not exist.
    regex = rf"(^|[^[:alnum:]_])({keywords})[[:space:]]+{symbol}([^[:alnum:]_]|$)"
    args = ["grep", "-rnE"]
    for glob in _SOURCE_GLOBS:
        args.append(f"--include={glob}")
    for skip in sorted(_SKIP_DIRS):
        args.append(f"--exclude-dir={skip}")
    # ``-e`` and ``--`` keep a dash-prefixed value from being parsed as an option.
    args.extend(["-e", regex, "--", safe_dir])

    proc_result = await run_command(*args, cwd=safe_working_dir, timeout_seconds=SEARCH_TIMEOUT_SECONDS)
    if proc_result.get("error"):
        return {"symbol": symbol, "error": proc_result["error"], "matches": [], "count": 0}
    if proc_result["return_code"] not in (0, 1):
        return {
            "symbol": symbol,
            "error": proc_result["stderr"].strip() or f"grep exited {proc_result['return_code']}",
            "matches": [],
            "count": 0,
        }

    results: list[dict[str, Any]] = []
    for line in proc_result["stdout"].splitlines():
        if not line.strip():
            continue
        parts = line.split(":", 2)
        if len(parts) >= 3:
            results.append({"file": parts[0], "line": parts[1], "text": parts[2].strip()})
        else:
            results.append({"raw": line})
        if len(results) >= _MAX_SEARCH_RESULTS:
            break
    return {"symbol": symbol, "matches": results, "count": len(results)}


@tool(
    name="grep_search",
    category="code_intel",
    description="Search file contents with a regex pattern, returning matches with context.",
)
async def grep_search(
    pattern: str,
    directory: str = ".",
    file_glob: str = "*",
    context_lines: int = 3,
    working_dir: str = "",
) -> dict[str, Any]:
    """Grep for a regex pattern across files, returning lines with surrounding context."""
    if not pattern:
        return {"error": "pattern must be a non-empty string"}
    safe_dir, denied = _safe_path(directory)
    if denied:
        return denied
    safe_working_dir = ""
    if working_dir:
        safe_working_dir, denied = _safe_path(working_dir)
        if denied:
            return denied

    context = max(0, min(int(context_lines), 20))
    args = ["grep", "-rn", f"--include={file_glob}", f"-C{context}"]
    for skip in sorted(_SKIP_DIRS):
        args.append(f"--exclude-dir={skip}")
    # Pass the pattern via ``-e`` and terminate options with ``--`` so a
    # dash-prefixed pattern ("--dry-run", "-v") searches instead of silently
    # being parsed as a grep option.
    args.extend(["-e", pattern, "--", safe_dir])

    proc_result = await run_command(*args, cwd=safe_working_dir, timeout_seconds=SEARCH_TIMEOUT_SECONDS)
    if proc_result.get("error"):
        return {"pattern": pattern, "directory": directory, "error": proc_result["error"], "output": ""}
    result: dict[str, Any] = {
        "pattern": pattern,
        "directory": directory,
        "output": proc_result["stdout"],
        "return_code": proc_result["return_code"],
    }
    # grep exits 0 on matches, 1 on no matches, >=2 on a real error.
    if proc_result["return_code"] not in (0, 1):
        result["error"] = proc_result["stderr"].strip() or f"grep exited {proc_result['return_code']}"
    return result


@tool(
    name="ast_analysis",
    category="code_intel",
    description="List top-level classes and functions in a Python file using the ast module.",
)
async def ast_analysis(path: str) -> dict[str, Any]:
    """Parse a Python file and return its top-level symbols."""
    safe_path, denied = _safe_path(path)
    if denied:
        return denied
    try:
        if os.path.isdir(safe_path):
            return {"error": f"{path} is a directory, not a file. Use file_search to find files."}
        with open(safe_path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=safe_path)
        classes = []
        functions = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                methods = [
                    n.name for n in ast.iter_child_nodes(node) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                classes.append({"name": node.name, "line": node.lineno, "methods": methods})
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append({"name": node.name, "line": node.lineno})
        return {"path": path, "classes": classes, "functions": functions}
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except SyntaxError as exc:
        return {"error": f"Syntax error: {exc}"}
    except Exception as exc:
        return {"error": str(exc)}
