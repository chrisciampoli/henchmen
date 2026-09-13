"""Git helpers shared by the operative bootstrap and the agent runtime.

The operative must never assume the target repository's default branch is
``main``: repos on ``master``/``develop`` would otherwise fail their fetch,
report "no changes" for committed-but-unpushed work, and diff against a ref
that does not exist. Every caller resolves the base ref through
:func:`detect_base_ref` instead of hard-coding a branch name.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Last-resort branch name when the remote advertises nothing usable.
DEFAULT_BASE_BRANCH = "main"

# Candidate default branches probed, in order, when the remote HEAD symref
# is unavailable (shallow clones without ``origin/HEAD``).
_CANDIDATE_BRANCHES: tuple[str, ...] = ("main", "master", "develop", "trunk")


async def run_git(workspace_dir: str, *args: str) -> tuple[str, str, int]:
    """Run a git command in ``workspace_dir`` and return ``(stdout, stderr, returncode)``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=workspace_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
        proc.returncode or 0,
    )


async def detect_base_branch(workspace_dir: str) -> str:
    """Return the repository's default branch name (no ``origin/`` prefix)."""
    out, _, rc = await run_git(workspace_dir, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    prefix = "refs/remotes/origin/"
    if rc == 0 and out.startswith(prefix):
        return out[len(prefix) :]

    for candidate in _CANDIDATE_BRANCHES:
        _, _, rc = await run_git(workspace_dir, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{candidate}")
        if rc == 0:
            return candidate

    return DEFAULT_BASE_BRANCH


async def detect_base_ref(workspace_dir: str) -> str:
    """Return the remote-tracking ref to diff against, e.g. ``origin/main``."""
    return f"origin/{await detect_base_branch(workspace_dir)}"


async def detect_remote_default_branch(clone_url: str, token: str | None = None) -> str:
    """Ask the remote for its default branch via ``git ls-remote --symref``.

    ``clone_url`` may embed a credential; it is never logged. Any failure
    degrades to :data:`DEFAULT_BASE_BRANCH` so callers stay fail-safe.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        "ls-remote",
        "--symref",
        clone_url,
        "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        message = stderr.decode("utf-8", errors="replace")[:300]
        if token:
            message = message.replace(token, "***")
        logger.warning("Could not read remote default branch: %s", message)
        return DEFAULT_BASE_BRANCH

    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("ref:") and "HEAD" in line:
            ref = line.split()[1]
            if ref.startswith("refs/heads/"):
                return ref[len("refs/heads/") :]
    return DEFAULT_BASE_BRANCH


def parse_porcelain_names(porcelain_output: str) -> list[str]:
    """Extract file paths from ``git status --porcelain`` output.

    Handles renames (``R  old -> new`` yields ``new``) and quoted paths.
    """
    names: list[str] = []
    for line in porcelain_output.splitlines():
        if len(line) <= 3:
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        names.append(entry.strip().strip('"'))
    return names


__all__ = [
    "DEFAULT_BASE_BRANCH",
    "detect_base_branch",
    "detect_base_ref",
    "detect_remote_default_branch",
    "parse_porcelain_names",
    "run_git",
]
