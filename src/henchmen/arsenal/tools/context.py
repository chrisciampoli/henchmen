"""Live context tools: semantic_search and find_related for mid-task RAG.

These tools allow operatives to query the vector index and discover related
files during task execution, not just at dossier build time. This is useful
when the initial context is insufficient and the operative needs to explore
the codebase more deeply.

Use ``semantic_search`` when: you need to find code related to a concept,
error message, or feature description. Do NOT use it for exact string matches
— use ``file_search`` (code_intel) instead.

Use ``find_related`` when: you have a file and want to discover what it
imports and what imports it. Do NOT use it as a substitute for reading the
file — use ``file_read`` (code_intel) instead.
"""

import ast
import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

from henchmen.arsenal.registry import tool

logger = logging.getLogger(__name__)


@tool(
    name="semantic_search",
    category="context",
    description=(
        "Query the code vector index for chunks semantically related to a natural-language query. "
        "Use when you need to find code related to a concept, error, or feature. "
        "Do NOT use for exact string matching — use file_search instead."
    ),
)
async def semantic_search(query: str, top_k: int = 5) -> dict[str, Any]:
    """Query the vector index for relevant code chunks mid-task.

    Tries Vertex AI RAG Engine first, falls back to grep-based search
    if the RAG backend is unavailable.
    """
    if not query.strip():
        return {"error": "Query cannot be empty"}

    top_k = max(1, min(top_k, 20))  # Clamp to reasonable range

    # Try RAG-based semantic search
    try:
        from henchmen.dossier.embedder import query_similar_chunks

        repo = os.environ.get("REPO_SLUG", "")
        project_id = os.environ.get("HENCHMEN_GCP_PROJECT_ID", "")
        region = os.environ.get("HENCHMEN_GCP_REGION", "us-central1")

        chunks = await query_similar_chunks(
            query_text=query,
            repo=repo,
            project_id=project_id,
            region=region,
            top_k=top_k,
        )

        if chunks:
            results = []
            for chunk in chunks:
                results.append(
                    {
                        "file_path": chunk.file_path,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "symbol_name": chunk.symbol_name,
                        "language": chunk.language,
                        "relevance_score": chunk.relevance_score,
                        "content_preview": chunk.content[:300],
                    }
                )
            return {"results": results, "source": "rag"}

    except Exception as exc:
        logger.debug("RAG semantic search unavailable, falling back to grep: %s", exc)

    # Fallback: grep-based search
    return await _grep_fallback(query, top_k)


async def _grep_fallback(query: str, top_k: int) -> dict[str, Any]:
    """Fallback search using grep when RAG is unavailable."""

    # Extract meaningful search terms from the query
    terms = [w for w in query.lower().split() if len(w) > 3]
    if not terms:
        terms = query.split()[:3]

    search_term = terms[0] if terms else query[:20]
    workspace = os.getcwd()

    try:
        proc = await asyncio.create_subprocess_exec(
            "grep",
            "-rl",
            "--include=*.py",
            "--include=*.ts",
            "--include=*.js",
            "--include=*.tsx",
            "--include=*.jsx",
            "--include=*.go",
            "--include=*.rs",
            "--include=*.java",
            "-i",
            search_term,
            workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        matches = stdout.decode().strip().split("\n")
        matches = [m for m in matches if m][:top_k]

        results = []
        for match_path in matches:
            rel = str(Path(match_path).relative_to(workspace))
            results.append({"file_path": rel.replace("\\", "/"), "source": "grep"})

        return {"results": results, "source": "grep_fallback", "search_term": search_term}
    except Exception as exc:
        return {"error": f"Grep fallback also failed: {exc}", "source": "none"}


@tool(
    name="find_related",
    category="context",
    description=(
        "Parse imports in a source file and return related files up to a given depth. "
        "Use when you need to discover the dependency graph around a file. "
        "Do NOT use as a substitute for reading the file — use file_read instead."
    ),
)
def find_related(file_path: str, depth: int = 1) -> dict[str, Any]:
    """Parse imports and find related files.

    For Python files, uses the ``ast`` module to parse import statements.
    For JS/TS files, uses regex to match ``import`` and ``require`` statements.
    Resolves import paths to actual file paths relative to the workspace.
    """
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}

    depth = max(1, min(depth, 3))  # Clamp depth to prevent explosion

    workspace = os.getcwd()
    visited: set[str] = set()
    related: dict[str, list[str]] = {}

    _discover_imports(file_path, workspace, depth, visited, related)

    return {"file": file_path, "related": related, "depth": depth}


def _discover_imports(
    file_path: str,
    workspace: str,
    depth: int,
    visited: set[str],
    related: dict[str, list[str]],
) -> None:
    """Recursively discover imports from a file."""
    abs_path = os.path.abspath(file_path)
    if abs_path in visited or depth < 0:
        return

    visited.add(abs_path)
    rel_path = os.path.relpath(abs_path, workspace).replace("\\", "/")

    ext = os.path.splitext(file_path)[1].lower()
    imports: list[str] = []

    try:
        if ext == ".py":
            imports = _parse_python_imports(abs_path, workspace)
        elif ext in (".js", ".ts", ".jsx", ".tsx"):
            imports = _parse_js_imports(abs_path, workspace)
    except Exception as exc:
        logger.debug("Failed to parse imports from %s: %s", file_path, exc)

    # Filter to only files that actually exist
    existing_imports = [imp for imp in imports if os.path.exists(os.path.join(workspace, imp))]
    related[rel_path] = existing_imports

    # Recurse into discovered files
    if depth > 1:
        for imp_path in existing_imports:
            full_imp = os.path.join(workspace, imp_path)
            _discover_imports(full_imp, workspace, depth - 1, visited, related)


def _parse_python_imports(file_path: str, workspace: str) -> list[str]:
    """Parse Python import statements using AST and resolve to file paths."""
    with open(file_path, encoding="utf-8", errors="replace") as fh:
        source = fh.read()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    imports: list[str] = []
    file_dir = os.path.dirname(file_path)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = _resolve_python_module(alias.name, workspace)
                if resolved:
                    imports.append(resolved)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level > 0:
                # Relative import
                resolved = _resolve_relative_import(node.module, node.level, file_dir, workspace)
            else:
                resolved = _resolve_python_module(node.module, workspace)
            if resolved:
                imports.append(resolved)

    return imports


def _resolve_python_module(module_name: str, workspace: str) -> str:
    """Resolve a Python module dotted path to a file path relative to workspace."""
    parts = module_name.split(".")
    # Try as a direct module file
    path = os.path.join(*parts) + ".py"
    if os.path.exists(os.path.join(workspace, path)):
        return path.replace("\\", "/")

    # Try as a package __init__
    path = os.path.join(*parts, "__init__.py")
    if os.path.exists(os.path.join(workspace, path)):
        return path.replace("\\", "/")

    # Try under src/
    path = os.path.join("src", *parts) + ".py"
    if os.path.exists(os.path.join(workspace, path)):
        return path.replace("\\", "/")

    path = os.path.join("src", *parts, "__init__.py")
    if os.path.exists(os.path.join(workspace, path)):
        return path.replace("\\", "/")

    return ""


def _resolve_relative_import(module: str, level: int, file_dir: str, workspace: str) -> str:
    """Resolve a relative import to a file path."""
    base_dir = file_dir
    for _ in range(level - 1):
        base_dir = os.path.dirname(base_dir)

    parts = module.split(".")
    path = os.path.join(base_dir, *parts) + ".py"
    if os.path.exists(path):
        return os.path.relpath(path, workspace).replace("\\", "/")

    path = os.path.join(base_dir, *parts, "__init__.py")
    if os.path.exists(path):
        return os.path.relpath(path, workspace).replace("\\", "/")

    return ""


def _parse_js_imports(file_path: str, workspace: str) -> list[str]:
    """Parse JS/TS import and require statements using regex."""
    with open(file_path, encoding="utf-8", errors="replace") as fh:
        content = fh.read(16000)  # Limit to first 16KB

    imports: list[str] = []
    file_dir = os.path.dirname(file_path)

    # Match: import ... from '...' or import '...'
    for match in re.finditer(r"""(?:import|export)\s+.*?from\s+['"](\.{1,2}/[^'"]+)['"]""", content):
        resolved = _resolve_js_path(match.group(1), file_dir, workspace)
        if resolved:
            imports.append(resolved)

    # Match: require('...')
    for match in re.finditer(r"""require\(['"](\.{1,2}/[^'"]+)['"]\)""", content):
        resolved = _resolve_js_path(match.group(1), file_dir, workspace)
        if resolved:
            imports.append(resolved)

    return imports


def _resolve_js_path(import_path: str, file_dir: str, workspace: str) -> str:
    """Resolve a relative JS/TS import path to a file path."""
    base = os.path.join(file_dir, import_path)
    extensions = ["", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js"]

    for ext in extensions:
        candidate = base + ext
        if os.path.exists(candidate) and os.path.isfile(candidate):
            return os.path.relpath(candidate, workspace).replace("\\", "/")

    return ""
