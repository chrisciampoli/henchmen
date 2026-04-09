"""Git operations tools - branch, commit, push, diff, log, status.

Security notes
--------------
- Every tool that accepts a ``working_dir`` routes it through
  :func:`henchmen.arsenal._workspace.ensure_in_workspace` so the operative
  cannot point git at an out-of-workspace repository.
- :func:`git_commit` validates every staged file against the workspace root
  after normalizing absolute paths — this prevents an LLM from staging files
  outside the operative's sandbox.
- :func:`git_force_push` is gated by the ``HENCHMEN_ALLOW_FORCE_PUSH``
  environment variable (default OFF) and additionally refuses to target any
  protected branch (``main``, ``master``, ``develop``, ``trunk``, ``release*``).
  The intent is that force-push is never needed for a healthy Henchmen
  workflow; leaving it off by default protects the target repo's history from
  hallucinated agent actions.
"""

import asyncio
import os
from typing import Any

from henchmen.arsenal._workspace import ensure_in_workspace
from henchmen.arsenal.registry import tool

# Branches that MUST NEVER be force-pushed, even when HENCHMEN_ALLOW_FORCE_PUSH
# is set. Match is case-insensitive and applied after stripping ``origin/`` and
# any leading ``refs/heads/``.
_PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "trunk"})
_PROTECTED_PREFIXES = ("release", "rel/", "stable")


def _resolve_working_dir(working_dir: str) -> str:
    """Return a workspace-checked absolute path, or an empty string if unset.

    Raises :class:`PermissionError` if the supplied ``working_dir`` escapes
    the workspace root.
    """
    if not working_dir:
        return ""
    return ensure_in_workspace(working_dir)


def _branch_is_protected(branch: str | None) -> bool:
    """Return True if ``branch`` is a protected name that must never be force-pushed."""
    if not branch:
        # HEAD / current branch — we can't tell without consulting git, so
        # conservatively refuse. Callers that legitimately want to force-push
        # MUST pass the explicit ``henchmen/*`` branch name.
        return True
    name = branch.strip().lower()
    if name.startswith("origin/"):
        name = name[len("origin/") :]
    if name.startswith("refs/heads/"):
        name = name[len("refs/heads/") :]
    if name in _PROTECTED_BRANCHES:
        return True
    return any(name.startswith(prefix) for prefix in _PROTECTED_PREFIXES)


async def _run_git(*args: str, working_dir: str = "") -> dict[str, Any]:
    """Run a git command and return stdout/stderr/returncode."""
    kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if working_dir:
        kwargs["cwd"] = working_dir
    proc = await asyncio.create_subprocess_exec("git", *args, **kwargs)
    stdout, stderr = await proc.communicate()
    return {
        "stdout": stdout.decode("utf-8"),
        "stderr": stderr.decode("utf-8"),
        "return_code": proc.returncode,
        "success": proc.returncode == 0,
    }


@tool(
    name="git_branch_create",
    category="git_ops",
    description="Create and checkout a new git branch from a base branch.",
)
async def git_branch_create(branch_name: str, base_branch: str = "main", working_dir: str = "") -> dict[str, Any]:
    """Create a new branch based on base_branch and check it out."""
    try:
        safe_working_dir = _resolve_working_dir(working_dir)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}", "success": False}
    fetch_result = await _run_git("fetch", "origin", base_branch, working_dir=safe_working_dir)
    if not fetch_result["success"]:
        # Continue even if fetch fails (local-only repo)
        pass
    result = await _run_git("checkout", "-b", branch_name, f"origin/{base_branch}", working_dir=safe_working_dir)
    if not result["success"]:
        # Try without origin/ prefix
        result = await _run_git("checkout", "-b", branch_name, base_branch, working_dir=safe_working_dir)
    result["branch_name"] = branch_name
    return result


@tool(
    name="git_commit",
    category="git_ops",
    description="Stage specified files (or all changes) and create a commit with the given message.",
)
async def git_commit(message: str, files: list[str] | str | None = None, working_dir: str = "") -> dict[str, Any]:
    """Stage files and commit. If files is None, stages all changes.

    Every supplied file path is validated against the workspace root. A file
    that escapes the workspace causes the entire stage to abort — we do not
    silently skip paths, because a half-staged commit is a worse outcome than
    a clear access-denied error.
    """
    import json as _json

    try:
        safe_working_dir = _resolve_working_dir(working_dir)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}", "success": False}

    # Normalize files: models sometimes pass a JSON string instead of a list
    file_list: list[str] | None = None
    if isinstance(files, str):
        try:
            parsed = _json.loads(files)
            if isinstance(parsed, list):
                file_list = [str(f) for f in parsed]
            else:
                file_list = [files]  # Single filename as string
        except _json.JSONDecodeError:
            # Space-separated or single file
            file_list = files.split() if " " in files else [files]
    elif isinstance(files, list):
        file_list = files

    if file_list:
        # Resolve each path against the workspace and bail out if any escape.
        cleaned: list[str] = []
        base_dir = safe_working_dir or os.getcwd()
        for f in file_list:
            # Resolve absolute against workspace, relative against working_dir.
            if os.path.isabs(f):
                try:
                    resolved = ensure_in_workspace(f)
                except PermissionError as exc:
                    return {"error": f"staged file '{f}' is outside workspace: {exc}", "success": False}
                cleaned.append(os.path.relpath(resolved, base_dir))
            else:
                try:
                    resolved = ensure_in_workspace(os.path.join(base_dir, f))
                except PermissionError as exc:
                    return {"error": f"staged file '{f}' is outside workspace: {exc}", "success": False}
                cleaned.append(os.path.relpath(resolved, base_dir))
        add_result = await _run_git("add", "--", *cleaned, working_dir=safe_working_dir)
        # If specific file staging fails, fall back to staging all changes
        if not add_result["success"]:
            add_result = await _run_git("add", "-A", working_dir=safe_working_dir)
    else:
        add_result = await _run_git("add", "-A", working_dir=safe_working_dir)
    if not add_result["success"]:
        return add_result

    result = await _run_git("commit", "-m", message, working_dir=safe_working_dir)
    result["message"] = message
    return result


@tool(
    name="git_push",
    category="git_ops",
    description="Push the current branch to the remote.",
)
async def git_push(branch: str | None = None, working_dir: str = "") -> dict[str, Any]:
    """Push to the remote. Force-push is intentionally NOT exposed by default."""
    try:
        safe_working_dir = _resolve_working_dir(working_dir)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}", "success": False}
    args = ["push", "origin"]
    if branch:
        args.append(branch)
    else:
        args.extend(["--set-upstream", "origin", "HEAD"])
    return await _run_git(*args, working_dir=safe_working_dir)


@tool(
    name="git_force_push",
    category="git_ops",
    description=(
        "Force-push a Henchmen branch to the remote. DESTRUCTIVE and DISABLED BY DEFAULT. "
        "Requires HENCHMEN_ALLOW_FORCE_PUSH=1 in the environment and refuses to target any "
        "protected branch (main/master/develop/trunk/release*). An explicit branch name is "
        "required — current-HEAD force-pushes are rejected."
    ),
    is_destructive=True,
)
async def git_force_push(branch: str | None = None, working_dir: str = "") -> dict[str, Any]:
    """Destructive force-push. Gated and branch-restricted by design.

    Refuses to run unless ``HENCHMEN_ALLOW_FORCE_PUSH`` is set to a truthy
    value. Refuses to target any protected branch. Refuses to operate on an
    implicit ``HEAD`` — an explicit branch name must be supplied so operators
    can audit what was force-pushed from the command line alone.
    """
    if os.environ.get("HENCHMEN_ALLOW_FORCE_PUSH", "").lower() not in ("1", "true", "yes", "on"):
        return {
            "error": (
                "git_force_push is disabled. Set HENCHMEN_ALLOW_FORCE_PUSH=1 in the "
                "operative environment to enable. This tool is intentionally gated because "
                "force-push is not part of the standard Henchmen workflow and is an effective "
                "way for a hallucinated or injected agent action to destroy history."
            ),
            "success": False,
        }
    if _branch_is_protected(branch):
        return {
            "error": (
                f"refusing to force-push to protected branch '{branch or 'HEAD'}'. "
                "Supply an explicit henchmen/* branch name if this is legitimately required."
            ),
            "success": False,
        }
    try:
        safe_working_dir = _resolve_working_dir(working_dir)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}", "success": False}
    # Use --force-with-lease to avoid clobbering concurrent pushes.
    args = ["push", "--force-with-lease", "origin", branch]
    return await _run_git(*args, working_dir=safe_working_dir)


@tool(
    name="git_diff",
    category="git_ops",
    description="Show git diff of working tree changes, or staged changes when staged=True.",
)
async def git_diff(staged: bool = False, working_dir: str = "") -> dict[str, Any]:
    """Return the current git diff output."""
    try:
        safe_working_dir = _resolve_working_dir(working_dir)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}", "success": False}
    args = ["diff"]
    if staged:
        args.append("--staged")
    return await _run_git(*args, working_dir=safe_working_dir)


@tool(
    name="git_log",
    category="git_ops",
    description="Show recent git commit log entries.",
)
async def git_log(max_count: int = 10, working_dir: str = "") -> dict[str, Any]:
    """Return the last N git commit log entries."""
    try:
        safe_working_dir = _resolve_working_dir(working_dir)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}", "success": False}
    return await _run_git("log", f"--max-count={max_count}", "--oneline", "--decorate", working_dir=safe_working_dir)


@tool(
    name="git_status",
    category="git_ops",
    description="Show the working tree status (modified, staged, untracked files).",
)
async def git_status(working_dir: str = "") -> dict[str, Any]:
    """Return git status output."""
    try:
        safe_working_dir = _resolve_working_dir(working_dir)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}", "success": False}
    return await _run_git("status", "--short", working_dir=safe_working_dir)
