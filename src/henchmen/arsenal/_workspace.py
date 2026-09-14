"""Arsenal workspace boundary enforcement.

Every Arsenal tool that touches the filesystem MUST route its paths through
``ensure_in_workspace`` before opening, writing, or deleting. This closes a
class of guardrail bypasses where the outer :class:`OperativeGuardrails` check
fails to trigger because a tool parameter is named differently than ``path``,
``file``, or ``dir``.

The allowed root is read from the ``WORKSPACE_DIR`` environment variable (part
of the Operative runtime contract; ``initialize_workspace`` sets it to the
task's clone) at first use and cached as a realpath, so symlink escapes are
blocked canonically. Tests can override the cached root by calling
``set_workspace_root`` directly.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

# The container-wide workspace root. The Operative clones each task into
# ``<DEFAULT_WORKSPACE_ROOT>/<task id>`` and then narrows ``WORKSPACE_DIR`` (and
# this module's cached root) to that clone, so the boundary is the repository
# itself. Every reader of the default imports this constant rather than
# repeating the literal.
DEFAULT_WORKSPACE_ROOT = "/workspace"

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
            raw = os.environ.get("WORKSPACE_DIR", DEFAULT_WORKSPACE_ROOT)
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
    # ``expanduser()`` resolves a leading ``~`` so a tilde path is checked
    # against the real home directory (and rejected by the boundary check
    # below) rather than being treated as a workspace-relative directory
    # literally named ``~``.
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


def current_workspace_dir() -> str:
    """Return the directory Arsenal tools should operate in by default.

    The Operative clones into ``<workspace root>/<task id>`` and chdirs there,
    so the process cwd — not the workspace root — is the repository. This
    returns that cwd when it lies inside the workspace, and falls back to the
    workspace root when the process happens to run elsewhere (for example a
    unit test whose cwd is the henchmen checkout). Callers get a directory
    that always satisfies :func:`ensure_in_workspace`.
    """
    try:
        return ensure_in_workspace(os.getcwd())
    except (PermissionError, OSError):
        return get_workspace_root()
