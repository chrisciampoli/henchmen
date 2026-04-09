"""Utility functions for integration tests."""

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime


def create_mock_operative_report(
    task_id: str,
    node_id: str,
    scheme_id: str,
    status: str = "completed",
    confidence: float = 0.9,
) -> dict:
    """Create a mock OperativeReport dict for testing.

    Args:
        task_id: ID of the parent HenchmenTask.
        node_id: Scheme node that was executed.
        scheme_id: Scheme definition the operative belongs to.
        status: Terminal status of the operative (e.g. "completed", "failed").
        confidence: Self-assessed confidence score (0.0 – 1.0).

    Returns:
        A dict that mirrors the OperativeReport model's serialised form.
    """
    now = datetime.now(UTC).isoformat()
    return {
        "task_id": task_id,
        "scheme_id": scheme_id,
        "node_id": node_id,
        "operative_id": f"mock-operative-{uuid.uuid4().hex[:8]}",
        "status": status,
        "git_diff": (
            "--- a/src/auth.py\n+++ b/src/auth.py\n"
            "@@ -1,4 +1,5 @@\n"
            " def login(username, password):\n"
            '+    """Authenticate user."""\n'
        )
        if status == "completed"
        else None,
        "summary": (
            f"Node {node_id!r} executed successfully for task {task_id}."
            if status == "completed"
            else f"Node {node_id!r} failed for task {task_id}."
        ),
        "confidence_score": confidence,
        "files_changed": ["src/auth.py"] if status == "completed" else [],
        "error": None if status == "completed" else "Operative encountered an unexpected error.",
        "started_at": now,
        "completed_at": now,
    }


def create_mock_ci_result(
    pr_url: str,
    status: str = "passed",
    request_id: str = "",
) -> dict:
    """Create a mock CI result dict for testing.

    Args:
        pr_url: URL of the pull request under test.
        status: CI outcome – "passed", "failed", or "pending".
        request_id: Optional identifier linking this result to a forge request.

    Returns:
        A dict representing a forge/CI result payload.
    """
    return {
        "request_id": request_id or str(uuid.uuid4()),
        "pr_url": pr_url,
        "status": status,
        "checks": [
            {"name": "unit-tests", "status": status, "conclusion": status},
            {"name": "lint", "status": status, "conclusion": status},
        ],
        "logs_url": f"{pr_url}/checks",
        "completed_at": datetime.now(UTC).isoformat(),
    }


def wait_for_condition(
    condition_fn: Callable[[], bool],
    timeout: float = 5.0,
    interval: float = 0.1,
) -> bool:
    """Poll a condition function until it returns True or the timeout elapses.

    Args:
        condition_fn: Zero-argument callable that returns True when the condition is met.
        timeout: Maximum number of seconds to wait.
        interval: Seconds between successive polls.

    Returns:
        True if the condition was met within the timeout; False otherwise.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition_fn():
            return True
        time.sleep(interval)
    return condition_fn()  # one final check at deadline
