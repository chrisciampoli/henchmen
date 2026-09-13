"""Git operations tools - branch, commit, push, diff, log, status.

Security notes
--------------
- Every tool that accepts a ``working_dir`` routes it through
  :func:`henchmen.arsenal._workspace.ensure_in_workspace` so the operative
  cannot point git at an out-of-workspace repository.
- :func:`git_commit` validates every staged file against the workspace root
  after normalizing absolute paths — this prevents an LLM from staging files
  outside the operative's sandbox.
- :func:`git_push` and :func:`git_force_push` validate the push target before
  handing it to git. The destination side of a ``src:dst`` refspec is checked
  against the protected list (``main``, ``master``, ``develop``, ``trunk``,
  ``release*``), and option-like values (``--mirror``) or force refspecs
  (``+branch:main``) are rejected outright — Henchmen delivers work through
  human-reviewable pull requests, never by pushing to a protected branch.
- :func:`git_force_push` additionally requires ``settings.allow_force_push``
  (``HENCHMEN_ALLOW_FORCE_PUSH``, default OFF). Force-push is never needed for
  a healthy Henchmen workflow; leaving it off protects the target repo's
  history from hallucinated or prompt-injected agent actions.
"""

import os
import re
from typing import Any

from henchmen.arsenal._process import run_command
from henchmen.arsenal._workspace import current_workspace_dir, ensure_in_workspace
from henchmen.arsenal.registry import tool

# Branches that MUST NEVER be pushed to by an operative. Match is
# case-insensitive and applied after stripping ``origin/`` and any leading
# ``refs/heads/``.
_PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "trunk"})
_PROTECTED_PREFIXES = ("release", "rel/", "stable")

# A branch name git will treat as a ref rather than an option. Rejects an
# empty value, a leading ``-`` (parsed as a git option), a leading ``+``
# (force refspec), whitespace, and shell-significant characters.
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def _resolve_working_dir(working_dir: str) -> str:
    """Return a workspace-checked absolute path, or an empty string if unset.

    Raises :class:`PermissionError` if the supplied ``working_dir`` escapes
    the workspace root.
    """
    if not working_dir:
        return ""
    return ensure_in_workspace(working_dir)


def _branch_is_protected(branch: str | None) -> bool:
    """Return True if ``branch`` names a protected branch.

    Accepts a bare name, an ``origin/``- or ``refs/heads/``-prefixed ref, or a
    ``src:dst`` refspec — for a refspec only the destination side matters,
    since that is the ref the remote would end up writing.
    """
    if not branch:
        # HEAD / current branch — we can't tell without consulting git, so
        # conservatively refuse. Callers that legitimately want to force-push
        # MUST pass the explicit ``henchmen/*`` branch name.
        return True
    name = branch.strip().lower().split(":")[-1]
    name = name.lstrip("+")
    if name.startswith("origin/"):
        name = name[len("origin/") :]
    if name.startswith("refs/heads/"):
        name = name[len("refs/heads/") :]
    if name in _PROTECTED_BRANCHES:
        return True
    return any(name.startswith(prefix) for prefix in _PROTECTED_PREFIXES)


def _push_target_error(branch: str) -> str | None:
    """Return an error message if ``branch`` is not a safe push target.

    Rejects git options, force refspecs, multi-colon refspecs and anything
    outside the conservative branch-name character set, then applies the
    protected-branch check to the destination side.
    """
    if not branch or branch != branch.strip():
        return "branch must be a non-empty name without surrounding whitespace"
    parts = branch.split(":")
    if len(parts) > 2:
        return f"invalid push target '{branch}': expected 'branch' or 'src:dst'"
    for part in parts:
        if not _BRANCH_RE.fullmatch(part):
            return (
                f"invalid push target '{branch}': branch names must match "
                "[A-Za-z0-9][A-Za-z0-9._/-]* (no options, force refspecs, or whitespace)"
            )
    if _branch_is_protected(parts[-1]):
        return (
            f"refusing to push to protected branch '{parts[-1]}'. Henchmen delivers work "
            "through pull requests — push the task's henchmen/* branch instead."
        )
    return None


async def _run_git(*args: str, working_dir: str = "") -> dict[str, Any]:
    """Run a git command and return stdout/stderr/returncode."""
    return await run_command("git", *args, cwd=working_dir)


@tool(
    name="git_branch_create",
    category="git_ops",
    description="Create and checkout a new git branch from a base branch.",
)
async def git_branch_create(branch_name: str, base_branch: str = "main", working_dir: str = "") -> dict[str, Any]:
    """Create a new branch based on base_branch and check it out."""
    for value, label in ((branch_name, "branch_name"), (base_branch, "base_branch")):
        if not value or not _BRANCH_RE.fullmatch(value):
            return {"error": f"invalid {label} '{value}': not a valid git branch name", "success": False}
    try:
        safe_working_dir = _resolve_working_dir(working_dir)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}", "success": False}
    # A failed fetch is tolerated: the repo may be local-only.
    await _run_git("fetch", "origin", base_branch, working_dir=safe_working_dir)
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
    a clear access-denied error. Likewise, when explicit staging fails we
    return that failure rather than falling back to ``git add -A``: staging
    files the model did not ask for is a silently wrong commit.
    """
    import json as _json

    if not message or not message.strip():
        return {"error": "commit message must be a non-empty string", "success": False}

    try:
        safe_working_dir = _resolve_working_dir(working_dir)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}", "success": False}

    # When the caller did not supply a working_dir, run where the operative
    # is working — it chdirs into the clone at ``<root>/<task id>``, which is
    # the git repository. The workspace root itself is only the parent and has
    # no ``.git``, so defaulting to it makes every git_commit fail with
    # "not a git repository".
    if not safe_working_dir:
        safe_working_dir = current_workspace_dir()

    # Normalize files: models sometimes pass a JSON string instead of a list
    file_list: list[str] | None = None
    if isinstance(files, str):
        try:
            parsed = _json.loads(files)
            # Single filename as string, or list of names already.
            file_list = [str(f) for f in parsed] if isinstance(parsed, list) else [files]
        except _json.JSONDecodeError:
            # Space-separated or single file
            file_list = files.split() if " " in files else [files]
    elif isinstance(files, list):
        file_list = files

    if file_list:
        # Resolve each path against the workspace and bail out if any escape.
        cleaned: list[str] = []
        base_dir = safe_working_dir
        for f in file_list:
            # Resolve absolute against workspace, relative against working_dir.
            candidate = f if os.path.isabs(f) else os.path.join(base_dir, f)
            try:
                resolved = ensure_in_workspace(candidate)
            except PermissionError as exc:
                return {"error": f"staged file '{f}' is outside workspace: {exc}", "success": False}
            cleaned.append(os.path.relpath(resolved, base_dir))
        add_result = await _run_git("add", "--", *cleaned, working_dir=safe_working_dir)
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
    description=(
        "Push the current branch to the remote, setting upstream. Refuses to target a "
        "protected branch (main/master/develop/trunk/release*) — Henchmen delivers work "
        "through pull requests."
    ),
)
async def git_push(branch: str | None = None, working_dir: str = "") -> dict[str, Any]:
    """Push to the remote. Force-push is intentionally NOT exposed by default."""
    if branch:
        target_error = _push_target_error(branch)
        if target_error:
            return {"error": target_error, "success": False}
    try:
        safe_working_dir = _resolve_working_dir(working_dir)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}", "success": False}
    return await _run_git("push", "--set-upstream", "origin", branch or "HEAD", working_dir=safe_working_dir)


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

    Refuses to run unless ``settings.allow_force_push`` is enabled. Refuses to
    target any protected branch. Refuses to operate on an implicit ``HEAD`` —
    an explicit branch name must be supplied so operators can audit what was
    force-pushed from the command line alone.
    """
    from henchmen.config.settings import get_settings

    if not get_settings().allow_force_push:
        return {
            "error": (
                "git_force_push is disabled. Set HENCHMEN_ALLOW_FORCE_PUSH=1 in the "
                "operative environment to enable. This tool is intentionally gated because "
                "force-push is not part of the standard Henchmen workflow and is an effective "
                "way for a hallucinated or injected agent action to destroy history."
            ),
            "success": False,
        }
    if not branch:
        return {
            "error": (
                "refusing to force-push the implicit current branch. Supply an explicit "
                "henchmen/* branch name if this is legitimately required."
            ),
            "success": False,
        }
    target_error = _push_target_error(branch)
    if target_error:
        return {"error": target_error, "success": False}
    try:
        safe_working_dir = _resolve_working_dir(working_dir)
    except PermissionError as exc:
        return {"error": f"access denied: {exc}", "success": False}
    # Use --force-with-lease to avoid clobbering concurrent pushes.
    return await _run_git("push", "--force-with-lease", "origin", branch, working_dir=safe_working_dir)


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
    count = max(1, min(int(max_count), 200))
    return await _run_git("log", f"--max-count={count}", "--oneline", "--decorate", working_dir=safe_working_dir)


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
