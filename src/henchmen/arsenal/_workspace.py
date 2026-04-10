"""Arsenal workspace boundary enforcement.

Every Arsenal tool that touches the filesystem MUST route its paths through
``ensure_in_workspace`` before opening, writing, or deleting. This closes a
class of guardrail bypasses where the outer :class:`OperativeGuardrails` check
fails to trigger because a tool parameter is named differently than ``path``,
``file``, or ``dir``.

The allowed root is read from the ``WORKSPACE_DIR`` environment variable at
first use and cached as a realpath, so symlink escapes are blocked
canonically. Tests can override the cached root by calling
``set_workspace_root`` directly.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

_DEFAULT_ROOT = "/workspace"

_lock = threading.Lock()
_cached_root: str | None = None


def set_workspace_root(path: str | os.PathLike[str] | None) -> None:
    """Override the workspace root.

    Pass ``None`` to clear the cache so the next call re-reads the environment
    variable. Primarily intended for tests.
    """
    global _cached_root
    with _lock:
        _cached_root = None if path is None else os.path.realpath(str(path))


def get_workspace_root() -> str:
    """Return the canonical workspace root.

    Reads ``WORKSPACE_DIR`` at first use, resolves to a real path, and caches
    the result. Subsequent calls are O(1) and do not touch the filesystem.
    """
    global _cached_root
    with _lock:
        if _cached_root is None:
            raw = os.environ.get("WORKSPACE_DIR", _DEFAULT_ROOT)
            _cached_root = os.path.realpath(raw)
        return _cached_root


def ensure_in_workspace(path: str | os.PathLike[str]) -> str:
    """Validate that ``path`` is inside the workspace root; return the real path.

    Raises :class:`PermissionError` if the supplied path escapes the workspace,
    uses ``..`` traversal, or resolves through a symlink to outside the root.
    Also rejects empty strings and ``None``.

    The returned value is a canonical absolute path that callers SHOULD use
    for the actual filesystem operation — callers must NOT re-open the raw
    ``path`` argument they were given, since that reintroduces the TOCTOU
    window the realpath resolution was meant to close.
    """
    if not path:
        raise PermissionError("workspace path must be a non-empty string")

    root = get_workspace_root()
    # ``Path(path).expanduser()`` blocks accidental tilde expansion into the
    # operator's home directory, which would escape the workspace on a
    # misconfigured container.
    candidate = Path(os.fspath(path)).expanduser()

    # Resolve relative paths against the workspace root — the agent should
    # write relative paths inside its workspace by default.
    if not candidate.is_absolute():
        candidate = Path(root) / candidate

    resolved = os.path.realpath(candidate)

    # ``commonpath`` raises ValueError on different drives (Windows). Treat
    # that as a boundary violation rather than a crash.
    try:
        common = os.path.commonpath([resolved, root])
    except ValueError as exc:
        raise PermissionError(f"Path '{path}' is on a different filesystem root than workspace '{root}'") from exc

    if common != root:
        raise PermissionError(f"Path '{path}' (resolved: '{resolved}') escapes workspace root '{root}'")
    return resolved
