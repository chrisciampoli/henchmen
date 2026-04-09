"""Code intelligence tools - read and search source code."""

import ast
import asyncio
import os
from typing import Any

from henchmen.arsenal.registry import tool


@tool(
    name="file_read",
    category="code_intel",
    description="Read file contents, optionally sliced to a line range.",
)
async def file_read(path: str, start_line: int = 0, end_line: int | None = None) -> dict[str, Any]:
    """Read a file and return its contents with line numbers."""
    import os

    try:
        if os.path.isdir(path):
            # Model passed a directory — list contents instead of erroring
            entries = sorted(os.listdir(path))
            return {"error": f"{path} is a directory, not a file. Contents: {entries[:50]}"}
        with open(path, encoding="utf-8") as fh:
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
    description="Search for files matching a glob pattern under a directory.",
)
async def file_search(pattern: str, directory: str = ".", file_glob: str = "*") -> dict[str, Any]:
    """Return file paths matching a glob pattern within a directory."""
    import fnmatch
    import glob as glob_mod

    try:
        search_pattern = os.path.join(directory, "**", file_glob)
        all_files = glob_mod.glob(search_pattern, recursive=True)
        # Further filter by the pattern parameter against basename
        matched = [f for f in all_files if fnmatch.fnmatch(os.path.basename(f), pattern) or fnmatch.fnmatch(f, pattern)]
        if not matched:
            # Fall back: treat pattern as a glob itself
            direct = glob_mod.glob(os.path.join(directory, "**", pattern), recursive=True)
            matched = direct
        return {"matches": sorted(matched), "count": len(matched)}
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="symbol_lookup",
    category="code_intel",
    description="Find class and function definitions matching a symbol name using grep.",
)
async def symbol_lookup(symbol: str, directory: str = ".", working_dir: str = "") -> dict[str, Any]:
    """Locate class/function definitions by name using grep."""
    patterns = [
        f"class {symbol}",
        f"def {symbol}",
        f"async def {symbol}",
    ]
    results: list[dict[str, Any]] = []
    for pat in patterns:
        kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if working_dir:
            kwargs["cwd"] = working_dir
        proc = await asyncio.create_subprocess_exec(
            "grep",
            "-rn",
            "--include=*.py",
            pat,
            directory,
            **kwargs,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode("utf-8").splitlines():
            if line.strip():
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    results.append({"file": parts[0], "line": parts[1], "text": parts[2].strip()})
                else:
                    results.append({"raw": line})
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
    kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if working_dir:
        kwargs["cwd"] = working_dir
    proc = await asyncio.create_subprocess_exec(
        "grep",
        "-rn",
        f"--include={file_glob}",
        f"-C{context_lines}",
        pattern,
        directory,
        **kwargs,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode("utf-8")
    return {
        "pattern": pattern,
        "directory": directory,
        "output": output,
        "return_code": proc.returncode,
    }


@tool(
    name="ast_analysis",
    category="code_intel",
    description="List top-level classes and functions in a Python file using the ast module.",
)
async def ast_analysis(path: str) -> dict[str, Any]:
    """Parse a Python file and return its top-level symbols."""
    import os

    try:
        if os.path.isdir(path):
            return {"error": f"{path} is a directory, not a file. Use file_search to find files."}
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=path)
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
