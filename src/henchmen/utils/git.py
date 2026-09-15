"""Shared git helpers used across Henchmen components."""

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

# GitHub's wording when a token without the `workflows` permission (App) or scope (OAuth/PAT) pushes a
# commit that changes .github/workflows/. Henchmen never requests that permission (amendment A4).
_WORKFLOW_REFUSAL_RE = re.compile(
    r"refusing to allow an? [\w ]+ to create or update workflow|without `?workflows?`? (permission|scope)",
    re.IGNORECASE,
)

WORKFLOW_PUSH_REFUSED_MESSAGE = (
    "Henchmen could not push its branch because the change edits GitHub Actions workflows "
    "(.github/workflows/). Henchmen is not allowed to change CI workflows, so a person needs to make "
    "that part of the change."
)

WORKFLOW_PUSH_TOOL_ERROR = (
    "push refused: this branch changes files under .github/workflows/, and Henchmen may not modify CI "
    "workflows. Undo those changes (for example `git checkout origin/<base branch> -- .github/workflows` "
    "and commit), then push again. If the task cannot be done without changing a workflow, stop and report "
    "that a person must make that change."
)


class WorkflowPushRefusedError(RuntimeError):
    """GitHub refused a push because it changes CI workflows."""


def is_workflow_push_refusal(stderr: str) -> bool:
    """True when ``git push`` output is GitHub refusing a change to ``.github/workflows/``."""
    return bool(stderr) and _WORKFLOW_REFUSAL_RE.search(stderr) is not None


async def clone_repo(
    repo: str,
    branch: str,
    workspace: str,
    token: str | None = None,
    *,
    depth: int | None = None,
    single_branch: bool = True,
    no_checkout: bool = False,
) -> None:
    """Clone a GitHub repo into *workspace*.

    Args:
        repo: GitHub repo in ``owner/name`` format.
        branch: Branch to clone.
        workspace: Local directory to clone into.
        token: GitHub token (used in the authenticated clone URL).
        depth: If set, pass ``--depth=<N>`` for a shallow clone.
        single_branch: If True (default), clone only the specified branch.
        no_checkout: If True, pass ``--no-checkout`` (useful for sparse checkouts).

    Raises:
        RuntimeError: If the ``git clone`` process returns a non-zero exit code.
            The error message is sanitised to avoid leaking the token.
    """
    clone_url = f"https://x-access-token:{token}@github.com/{repo}.git" if token else f"https://github.com/{repo}.git"

    cmd: list[str] = ["git", "clone"]
    if depth is not None:
        cmd.append(f"--depth={depth}")
    if single_branch:
        cmd.extend(["--branch", branch, "--single-branch"])
    else:
        cmd.extend(["--branch", branch])
    if no_checkout:
        cmd.append("--no-checkout")
    cmd.extend([clone_url, workspace])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err_msg = stderr.decode(errors="replace")[:500]
        if token:
            err_msg = err_msg.replace(token, "***")
        raise RuntimeError(f"git clone failed: {err_msg}")


def build_clone_url(repo: str, token: str | None = None) -> str:
    """Return the HTTPS clone URL for a GitHub repo, optionally authenticated."""
    if token:
        return f"https://x-access-token:{token}@github.com/{repo}.git"
    return f"https://github.com/{repo}.git"


async def configure_git_identity(workspace: str, email: str, name: str = "Henchmen Operative") -> None:
    """Set ``user.email`` and ``user.name`` in the git config for *workspace*."""
    for key, value in [("user.email", email), ("user.name", name)]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "config",
            key,
            value,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()


async def fetch_remote_ref(workspace: str, remote: str = "origin", ref: str = "main") -> None:
    """Fetch a remote ref and map it to ``refs/remotes/<remote>/<ref>``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "fetch",
        remote,
        f"{ref}:refs/remotes/{remote}/{ref}",
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("git fetch %s %s failed: %s", remote, ref, stderr.decode(errors="replace")[:300])
