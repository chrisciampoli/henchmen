"""Git operations tools - branch, commit, push, diff, log, status."""

import asyncio
import os
from typing import Any

from henchmen.arsenal.registry import tool


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
    fetch_result = await _run_git("fetch", "origin", base_branch, working_dir=working_dir)
    if not fetch_result["success"]:
        # Continue even if fetch fails (local-only repo)
        pass
    result = await _run_git("checkout", "-b", branch_name, f"origin/{base_branch}", working_dir=working_dir)
    if not result["success"]:
        # Try without origin/ prefix
        result = await _run_git("checkout", "-b", branch_name, base_branch, working_dir=working_dir)
    result["branch_name"] = branch_name
    return result


@tool(
    name="git_commit",
    category="git_ops",
    description="Stage specified files (or all changes) and create a commit with the given message.",
)
async def git_commit(message: str, files: list[str] | str | None = None, working_dir: str = "") -> dict[str, Any]:
    """Stage files and commit. If files is None, stages all changes."""
    import json as _json

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
        # Strip workspace prefix from absolute paths
        cleaned: list[str] = []
        for f in file_list:
            if working_dir and os.path.isabs(f) and f.startswith(working_dir):
                cleaned.append(os.path.relpath(f, working_dir))
            elif f.startswith("/"):
                # Absolute path but doesn't match workspace — try relative anyway
                cleaned.append(os.path.basename(f))
            else:
                cleaned.append(f)
        add_result = await _run_git("add", "--", *cleaned, working_dir=working_dir)
        # If specific file staging fails, fall back to staging all changes
        if not add_result["success"]:
            add_result = await _run_git("add", "-A", working_dir=working_dir)
    else:
        add_result = await _run_git("add", "-A", working_dir=working_dir)
    if not add_result["success"]:
        return add_result

    result = await _run_git("commit", "-m", message, working_dir=working_dir)
    result["message"] = message
    return result


@tool(
    name="git_push",
    category="git_ops",
    description="Push the current branch to the remote.",
)
async def git_push(branch: str | None = None, working_dir: str = "") -> dict[str, Any]:
    """Push to the remote. Use git_force_push for destructive force-push."""
    args = ["push", "origin"]
    if branch:
        args.append(branch)
    else:
        args.extend(["--set-upstream", "origin", "HEAD"])
    return await _run_git(*args, working_dir=working_dir)


@tool(
    name="git_force_push",
    category="git_ops",
    description="Force-push the current branch to the remote. DESTRUCTIVE: overwrites remote history.",
    is_destructive=True,
)
async def git_force_push(branch: str | None = None, working_dir: str = "") -> dict[str, Any]:
    """Destructive force-push to the remote. Overwrites remote branch history."""
    args = ["push", "--force", "origin"]
    if branch:
        args.append(branch)
    else:
        args.extend(["HEAD"])
    return await _run_git(*args, working_dir=working_dir)


@tool(
    name="git_diff",
    category="git_ops",
    description="Show git diff of working tree changes, or staged changes when staged=True.",
)
async def git_diff(staged: bool = False, working_dir: str = "") -> dict[str, Any]:
    """Return the current git diff output."""
    args = ["diff"]
    if staged:
        args.append("--staged")
    return await _run_git(*args, working_dir=working_dir)


@tool(
    name="git_log",
    category="git_ops",
    description="Show recent git commit log entries.",
)
async def git_log(max_count: int = 10, working_dir: str = "") -> dict[str, Any]:
    """Return the last N git commit log entries."""
    return await _run_git("log", f"--max-count={max_count}", "--oneline", "--decorate", working_dir=working_dir)


@tool(
    name="git_status",
    category="git_ops",
    description="Show the working tree status (modified, staged, untracked files).",
)
async def git_status(working_dir: str = "") -> dict[str, Any]:
    """Return git status output."""
    return await _run_git("status", "--short", working_dir=working_dir)
