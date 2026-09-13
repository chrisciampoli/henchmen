"""Shared subprocess execution for Arsenal tools.

Every Arsenal tool that shells out routes through :func:`run_command`:

- **Timeouts.** An unbounded ``communicate()`` lets a child that never exits
  (a Node ``test`` script that drops into watch mode, a grep over a stalled
  network mount) hold the Operative until the Cloud Run Job wall clock kills
  it, which surfaces as ``TIMED_OUT`` with no diagnostic. An expired command
  is killed and reported as a failure, never as a pass.
- **Lossy decoding.** Output is decoded with ``errors="replace"`` so a latin-1
  source file in a diff or a test run that emits invalid bytes returns the
  output instead of raising ``UnicodeDecodeError`` at the tool boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

# Per-category defaults. Git and search operations are interactive-speed;
# test/lint/typecheck runs are given the bulk of a node's budget.
DEFAULT_TIMEOUT_SECONDS = 120.0
SEARCH_TIMEOUT_SECONDS = 60.0
TEST_TIMEOUT_SECONDS = 900.0


async def run_command(
    *args: str,
    cwd: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a command and return ``stdout``/``stderr``/``return_code``/``success``.

    Never raises for an ordinary failure: a missing binary, a non-zero exit or
    a timeout all come back as ``success: False`` so the calling tool stays
    fail-closed and the model sees a usable error string.
    """
    kwargs: dict[str, Any] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if cwd:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = env

    try:
        proc = await asyncio.create_subprocess_exec(*args, **kwargs)
    except (OSError, ValueError) as exc:
        return {
            "stdout": "",
            "stderr": str(exc),
            "return_code": -1,
            "success": False,
            "error": f"failed to run {args[0]!r}: {exc}",
        }

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        await _kill(proc)
        return {
            "stdout": "",
            "stderr": "",
            "return_code": -1,
            "success": False,
            "timed_out": True,
            "error": f"command timed out after {timeout_seconds:.0f}s: {' '.join(args)}",
        }

    return {
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "return_code": proc.returncode,
        "success": proc.returncode == 0,
    }


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Terminate a timed-out child and reap it, ignoring races with its exit."""
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        return
    with contextlib.suppress(TimeoutError, ProcessLookupError):
        await asyncio.wait_for(proc.wait(), timeout=5.0)


def subprocess_env(**overrides: str) -> dict[str, str]:
    """Return ``os.environ`` plus ``overrides`` for a child process.

    Used to pin ``CI=1`` for test runners: many Node test scripts enter watch
    mode when ``CI`` is unset, which is the classic way an Operative hangs.
    """
    env = dict(os.environ)
    env.update(overrides)
    return env
