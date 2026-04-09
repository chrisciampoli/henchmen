"""Code chunking engine for splitting source files into embeddable chunks.

Uses AST parsing for Python, regex-based splitting for TypeScript/JavaScript,
and fixed-size line-based chunking as fallback.
"""

from __future__ import annotations

import ast
import os
import re

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE: int = 100_000  # 100 KB

ALLOWED_EXTENSIONS: set[str] = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".md",
    ".css",
    ".html",
    ".toml",
    ".go",
    ".rs",
}

SKIP_FILES: set[str] = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "Pipfile.lock",
}

SKIP_DIRS: set[str] = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    ".tox",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
}

BINARY_EXTENSIONS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".pdf",
    ".doc",
    ".docx",
    ".mp3",
    ".mp4",
    ".wav",
    ".avi",
    ".bin",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
}

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".css": "css",
    ".html": "html",
    ".toml": "toml",
    ".go": "go",
    ".rs": "rust",
}

FIXED_CHUNK_LINES: int = 40
FIXED_CHUNK_OVERLAP: int = 5

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class CodeChunk(BaseModel):
    """A single embeddable chunk of source code."""

    file_path: str
    start_line: int
    end_line: int
    symbol_name: str | None
    language: str
    content: str
    chunk_type: str  # "function", "class", "method", "fixed"


# ---------------------------------------------------------------------------
# Skip rules
# ---------------------------------------------------------------------------


def should_skip_file(rel_path: str, file_size: int = 0) -> bool:
    """Determine whether a file should be skipped during chunking."""
    # Check file size limit
    if file_size > MAX_FILE_SIZE:
        return True

    # Check skip directories
    parts = rel_path.replace("\\", "/").split("/")
    for part in parts[:-1]:  # all parts except the filename
        if part in SKIP_DIRS:
            return True

    # Check exact skip filenames
    basename = os.path.basename(rel_path)
    if basename in SKIP_FILES:
        return True

    # Check binary extensions
    _, ext = os.path.splitext(rel_path)
    ext = ext.lower()
    if ext in BINARY_EXTENSIONS:
        return True

    # Check allowed extensions (reject if not in the allow-list)
    if ext not in ALLOWED_EXTENSIONS:
        return True

    return False


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def _detect_language(file_path: str) -> str:
    """Return the language string for a file path."""
    _, ext = os.path.splitext(file_path)
    return EXTENSION_TO_LANGUAGE.get(ext.lower(), "text")


# ---------------------------------------------------------------------------
# Python AST chunking
# ---------------------------------------------------------------------------


def _get_node_end_line(node: ast.AST, source_lines: list[str]) -> int:
    """Get the end line for an AST node, with fallback."""
    if hasattr(node, "end_lineno") and node.end_lineno is not None:
        return int(node.end_lineno)
    # Fallback: use the last line of source
    return len(source_lines)


def _chunk_python(file_path: str, content: str) -> list[CodeChunk]:
    """Chunk Python source using AST parsing."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return _chunk_fixed_size(file_path, content)

    chunks: list[CodeChunk] = []
    lines = content.splitlines()
    language = "python"

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            start = node.lineno
            end = _get_node_end_line(node, lines)
            chunk_content = "\n".join(lines[start - 1 : end])
            chunks.append(
                CodeChunk(
                    file_path=file_path,
                    start_line=start,
                    end_line=end,
                    symbol_name=node.name,
                    language=language,
                    content=chunk_content,
                    chunk_type="function",
                )
            )
        elif isinstance(node, ast.ClassDef):
            # Emit the whole class as one chunk
            cls_start = node.lineno
            cls_end = _get_node_end_line(node, lines)
            cls_content = "\n".join(lines[cls_start - 1 : cls_end])
            chunks.append(
                CodeChunk(
                    file_path=file_path,
                    start_line=cls_start,
                    end_line=cls_end,
                    symbol_name=node.name,
                    language=language,
                    content=cls_content,
                    chunk_type="class",
                )
            )
            # Also emit each method within the class
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    m_start = child.lineno
                    m_end = _get_node_end_line(child, lines)
                    m_content = "\n".join(lines[m_start - 1 : m_end])
                    chunks.append(
                        CodeChunk(
                            file_path=file_path,
                            start_line=m_start,
                            end_line=m_end,
                            symbol_name=f"{node.name}.{child.name}",
                            language=language,
                            content=m_content,
                            chunk_type="method",
                        )
                    )

    return chunks


# ---------------------------------------------------------------------------
# TypeScript / JavaScript regex chunking
# ---------------------------------------------------------------------------

# Patterns that identify declaration boundaries
_TS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("function", re.compile(r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+(\w+)", re.MULTILINE)),
    ("class", re.compile(r"^(?:export\s+(?:default\s+)?)?class\s+(\w+)", re.MULTILINE)),
    ("arrow", re.compile(r"^(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[^=])\s*=>", re.MULTILINE)),
    ("const_fn", re.compile(r"^(?:export\s+)?const\s+(\w+)\s*=\s*function", re.MULTILINE)),
]


def _find_matching_brace(content: str, start: int) -> int:
    """Find the position after the closing brace that matches the first opening brace at or after *start*."""
    idx = content.find("{", start)
    if idx == -1:
        return len(content)
    depth = 0
    for i in range(idx, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(content)


def _chunk_typescript(file_path: str, content: str) -> list[CodeChunk]:
    """Chunk TypeScript/JavaScript source using regex-based boundary detection."""
    language = _detect_language(file_path)
    lines = content.splitlines()
    if not lines:
        return []

    # Collect all declaration matches with their positions
    declarations: list[tuple[int, str, str]] = []  # (char_offset, name, chunk_type)
    for chunk_type, pattern in _TS_PATTERNS:
        for m in pattern.finditer(content):
            declarations.append((m.start(), m.group(1), chunk_type))

    # Sort by position in file
    declarations.sort(key=lambda d: d[0])

    if not declarations:
        # No declarations found — fall back to fixed-size
        return _chunk_fixed_size(file_path, content)

    chunks: list[CodeChunk] = []
    for char_offset, name, chunk_type in declarations:
        # Find line number from char offset
        start_line = content[:char_offset].count("\n") + 1
        # Find the end of the block by brace matching
        block_end_char = _find_matching_brace(content, char_offset)
        # Also consume a trailing semicolon if present
        if block_end_char < len(content) and content[block_end_char] == ";":
            block_end_char += 1
        end_line = content[:block_end_char].count("\n") + 1
        chunk_content = "\n".join(lines[start_line - 1 : end_line])
        chunks.append(
            CodeChunk(
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                symbol_name=name,
                language=language,
                content=chunk_content,
                chunk_type=chunk_type,
            )
        )

    return chunks


# ---------------------------------------------------------------------------
# Fixed-size fallback chunking
# ---------------------------------------------------------------------------


def _chunk_fixed_size(file_path: str, content: str) -> list[CodeChunk]:
    """Split content into fixed-size line-based chunks with overlap."""
    language = _detect_language(file_path)
    lines = content.splitlines()
    if not lines:
        return []

    chunks: list[CodeChunk] = []
    start = 0
    while start < len(lines):
        end = min(start + FIXED_CHUNK_LINES, len(lines))
        chunk_content = "\n".join(lines[start:end])
        chunks.append(
            CodeChunk(
                file_path=file_path,
                start_line=start + 1,  # 1-indexed
                end_line=end,
                symbol_name=None,
                language=language,
                content=chunk_content,
                chunk_type="fixed",
            )
        )
        # Advance by chunk size minus overlap, but ensure forward progress
        step = FIXED_CHUNK_LINES - FIXED_CHUNK_OVERLAP
        if start + step >= len(lines):
            break
        start += step

    return chunks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chunk_file(file_path: str, content: str) -> list[CodeChunk]:
    """Chunk a single file into embeddable pieces.

    Dispatches to the appropriate chunker based on file extension:
    - .py  -> AST-based Python chunker
    - .ts/.tsx/.js/.jsx -> regex-based TypeScript/JavaScript chunker
    - everything else -> fixed-size line-based chunker
    """
    if not content or not content.strip():
        return []

    _, ext = os.path.splitext(file_path)
    ext = ext.lower()

    if ext == ".py":
        return _chunk_python(file_path, content)
    elif ext in {".ts", ".tsx", ".js", ".jsx"}:
        return _chunk_typescript(file_path, content)
    else:
        return _chunk_fixed_size(file_path, content)


def chunk_files(files: dict[str, str]) -> list[CodeChunk]:
    """Chunk multiple files, skipping files that should be excluded.

    Args:
        files: mapping of relative file paths to their content.

    Returns:
        Flat list of CodeChunk instances from all non-skipped files.
    """
    all_chunks: list[CodeChunk] = []
    for path, content in files.items():
        if should_skip_file(path, file_size=len(content.encode("utf-8", errors="replace"))):
            continue
        all_chunks.extend(chunk_file(path, content))
    return all_chunks
