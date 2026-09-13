"""Code editing tools - create, write, edit, and delete files.

All file-system tools in this module route paths through
:func:`henchmen.arsenal._workspace.ensure_in_workspace` before opening, writing,
or deleting. This is the in-tool enforcement of the workspace boundary — it
closes the class of bypasses where the outer :class:`OperativeGuardrails`
check fails because a parameter is named differently than ``path``, ``file``,
or ``dir``.
"""

import os
from typing import Any

from henchmen.arsenal._workspace import ensure_in_workspace
from henchmen.arsenal.registry import tool


def _normalize(text: str) -> str:
    """Normalize whitespace and the unicode characters LLMs most often mangle."""
    # Normalize line endings and trailing whitespace
    text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    # Normalize common unicode characters that LLMs get wrong
    text = text.replace("—", "-").replace("–", "-")  # em dash, en dash -> hyphen
    text = text.replace("“", '"').replace("”", '"')  # smart double quotes
    text = text.replace("‘", "'").replace("’", "'")  # smart single quotes
    text = text.replace("…", "...")  # ellipsis
    return text.replace(" ", " ")  # non-breaking space


def _splice_normalized(original: str, old_text: str, new_text: str) -> str | None:
    """Replace the line span matching ``old_text`` under normalization.

    Returns the updated file content, or ``None`` when no span matches. Only
    the matched lines are rewritten — every other byte of ``original`` is
    preserved, so a targeted edit can never silently normalize unrelated
    strings, docs or fixtures elsewhere in the file.
    """
    orig_lines = original.splitlines(keepends=True)
    norm_lines = [_normalize(line) for line in original.splitlines()]
    old_lines = [_normalize(line) for line in old_text.replace("\r\n", "\n").strip("\n").split("\n")]
    if not old_lines:
        return None

    span = len(old_lines)
    for start in range(len(norm_lines) - span + 1):
        if norm_lines[start : start + span] != old_lines:
            continue
        newline = "\r\n" if "\r\n" in original else "\n"
        ends_with_newline = orig_lines[start + span - 1].endswith("\n")
        new_lines = new_text.replace("\r\n", "\n").split("\n")
        if new_lines and new_lines[-1] == "":
            new_lines.pop()
        block = newline.join(new_lines)
        if block and ends_with_newline:
            block += newline
        return "".join(orig_lines[:start]) + block + "".join(orig_lines[start + span :])
    return None


@tool(
    name="file_write",
    category="code_edit",
    description="Write or overwrite a file with the given content.",
)
async def file_write(path: str, content: str) -> dict[str, Any]:
    """Write content to a file, creating parent directories as needed."""
    try:
        safe_path = ensure_in_workspace(path)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}"}
    try:
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return {"path": path, "bytes_written": len(content.encode("utf-8")), "success": True}
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="file_edit",
    category="code_edit",
    description=(
        "Replace text in a file. Tries exact match first, then whitespace-normalized match. "
        "If old_text is not found, use file_write to overwrite the entire file instead."
    ),
)
async def file_edit(path: str, old_text: str, new_text: str) -> dict[str, Any]:
    """Replace text in a file with fuzzy whitespace matching.

    The normalized fallback rewrites only the matched line span: normalizing
    the whole file would silently change em dashes, smart quotes and trailing
    whitespace in unrelated code, docs and fixtures, and report success.
    """
    if not old_text or not old_text.strip():
        return {
            "error": "old_text must be a non-empty string. Use file_write to create or overwrite a file.",
            "path": path,
            "hint": "use file_write to overwrite the whole file",
        }
    try:
        safe_path = ensure_in_workspace(path)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}"}
    try:
        with open(safe_path, encoding="utf-8") as fh:
            original = fh.read()

        # Try exact match first
        if old_text in original:
            updated = original.replace(old_text, new_text, 1)
            with open(safe_path, "w", encoding="utf-8") as fh:
                fh.write(updated)
            return {"path": path, "success": True, "replacements": 1}

        # Then a whitespace/unicode-normalized match, spliced into that span only.
        spliced = _splice_normalized(original, old_text, new_text)
        if spliced is not None:
            with open(safe_path, "w", encoding="utf-8") as fh:
                fh.write(spliced)
            return {"path": path, "success": True, "replacements": 1, "note": "matched with whitespace normalization"}

        # Try matching just the first line of old_text as an anchor
        first_line = old_text.strip().split("\n")[0].strip()
        if first_line and first_line in original:
            return {
                "error": f"Exact old_text not found, but first line '{first_line[:60]}' exists in the file. "
                f"Try using file_write(path, content) to write the entire file with your changes instead.",
                "path": path,
                "hint": "use file_write to overwrite the whole file",
            }

        return {
            "error": (
                f"old_text not found in {path}. "
                "Use file_write(path, content) to write the complete file content instead."
            ),
            "path": path,
            "hint": "use file_write to overwrite the whole file",
        }
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="file_create",
    category="code_edit",
    description="Create a new file with the given content. Fails if the file already exists.",
)
async def file_create(path: str, content: str) -> dict[str, Any]:
    """Create a new file; raises an error if it already exists."""
    try:
        safe_path = ensure_in_workspace(path)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}"}
    try:
        if os.path.exists(safe_path):
            return {"error": f"File already exists: {path}"}
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "x", encoding="utf-8") as fh:
            fh.write(content)
        return {"path": path, "bytes_written": len(content.encode("utf-8")), "success": True}
    except FileExistsError:
        return {"error": f"File already exists: {path}"}
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="file_insert_at_line",
    category="code_edit",
    description=(
        "Insert text at a specific line number in a file. Line 1 inserts at the top. "
        "Use this instead of file_edit when you want to add new content at a specific location."
    ),
)
async def file_insert_at_line(path: str, line_number: int, text: str) -> dict[str, Any]:
    """Insert text at a specific line number (1-indexed). Existing content shifts down.

    Out-of-range line numbers are rejected rather than clamped: silently
    appending at EOF while reporting the requested line leaves the model with
    a wrong picture of the file for every subsequent edit.
    """
    try:
        safe_path = ensure_in_workspace(path)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}"}
    try:
        with open(safe_path, encoding="utf-8") as fh:
            lines = fh.readlines()
        if line_number < 1 or line_number > len(lines) + 1:
            return {
                "error": f"line_number {line_number} is out of range 1..{len(lines) + 1} for {path}",
                "path": path,
                "total_lines": len(lines),
            }
        idx = line_number - 1
        # Ensure text ends with newline
        if text and not text.endswith("\n"):
            text += "\n"
        lines.insert(idx, text)
        with open(safe_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        return {"path": path, "success": True, "inserted_at_line": line_number}
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    name="file_delete",
    category="code_edit",
    description="Delete a file from the filesystem. This operation is destructive and cannot be undone.",
    is_destructive=True,
)
async def file_delete(path: str) -> dict[str, Any]:
    """Delete a file. This is a destructive, irreversible operation."""
    try:
        safe_path = ensure_in_workspace(path)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}"}
    try:
        if not os.path.exists(safe_path):
            return {"error": f"File not found: {path}"}
        os.remove(safe_path)
        return {"path": path, "success": True}
    except Exception as exc:
        return {"error": str(exc)}
