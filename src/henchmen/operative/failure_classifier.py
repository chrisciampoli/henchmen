"""Classify tool failures into actionable categories with recovery strategies.

The agent loop distinguishes between five kinds of tool-call failures so it
can react appropriately:

* ``tool_error``      — the tool call itself failed (transient, retryable).
* ``test_failure``    — tests ran but failed (semantic — fix the code).
* ``lint_error``      — lint/type checks failed (deterministic — fix violations).
* ``context_missing`` — the tool couldn't find what was requested (need different search).
* ``approach_wrong``  — repeated failures suggest the approach is fundamentally flawed.

The three original categories (transient, semantic, environmental) are mapped
to the new taxonomy for backward compatibility.

The classifier is deliberately a small heuristic function, not a model call,
so it can be used in hot paths without latency cost.
"""

from __future__ import annotations

from typing import Any, Literal

FailureClass = Literal[
    "tool_error",
    "test_failure",
    "lint_error",
    "context_missing",
    "approach_wrong",
    # Legacy aliases (still returned for backward compatibility)
    "transient",
    "semantic",
    "environmental",
    "none",
]


_TRANSIENT_MARKERS: tuple[str, ...] = (
    "too many",
    "temporarily unavailable",
    "connection reset",
    "timeout",
    "timed out",
    "rate limit",
    "rate_limit",
    "resource exhausted",
    "resource_exhausted",
    "try again",
    "eagain",
)

_ENVIRONMENTAL_MARKERS: tuple[str, ...] = (
    "permission",
    "access denied",
    "enospc",
    "disk full",
    "read-only file system",
    "command not found",
    "no space left",
)

_TEST_FAILURE_MARKERS: tuple[str, ...] = (
    "test failed",
    "tests failed",
    "assertion error",
    "assertionerror",
    "assert ",
    "expected ",
    "test_",
    "failed test",
    "failures=",
    "errors=",
    "pytest",
    "jest",
    "mocha",
)

_LINT_ERROR_MARKERS: tuple[str, ...] = (
    "lint",
    "eslint",
    "ruff",
    "flake8",
    "mypy",
    "pyright",
    "type error",
    "typeerror",
    "type-check",
    "type_check",
    "undefined variable",
    "unused import",
    "missing return",
    "incompatible type",
)

_CONTEXT_MISSING_MARKERS: tuple[str, ...] = (
    "not found",
    "no such",
    "no matches",
    "no results",
    "file not found",
    "could not find",
    "does not exist",
    "old text not found",
    "no match found",
)

# Recovery strategies mapped to each failure class
RECOVERY_STRATEGIES: dict[str, str] = {
    "tool_error": (
        "The tool call failed due to a transient error. "
        "Wait a moment and retry the same call. If it fails again, "
        "try an alternative approach."
    ),
    "test_failure": (
        "Tests are failing. Read the test output carefully to identify "
        "which specific assertion failed and why. Fix the code to make "
        "the failing test pass without breaking other tests."
    ),
    "lint_error": (
        "Lint or type check errors were found. Read the error messages, "
        "identify the specific violations, and fix them. Common fixes: "
        "add missing imports, fix type annotations, remove unused variables."
    ),
    "context_missing": (
        "The file or symbol you searched for was not found. Try: "
        "1) Search with a broader query or different keywords. "
        "2) Use grep_search to find related files. "
        "3) Check if the path is correct relative to the workspace root."
    ),
    "approach_wrong": (
        "Multiple attempts with the same approach have failed. "
        "Step back and reconsider your strategy. Try: "
        "1) Re-read the task description for clues you may have missed. "
        "2) Look at different files or a different part of the codebase. "
        "3) Consider a simpler or more direct approach."
    ),
    # Legacy aliases
    "transient": "Retry the operation — it may succeed on a subsequent attempt.",
    "semantic": "The tool ran but the result is not actionable. Adapt your strategy.",
    "environmental": "The environment is broken. Consider escalating or working around the issue.",
}


def classify_tool_failure(tool_result: Any) -> FailureClass:
    """Classify a tool result into one of the ``FailureClass`` categories.

    Accepts ``Any`` so callers do not need to type-check before invoking.
    Returns ``"none"`` if the result is not a dict or has no ``error`` field.
    """
    if not isinstance(tool_result, dict):
        return "none"

    error = tool_result.get("error")
    if not error:
        return "none"

    message = str(error).lower()
    tool_name = str(tool_result.get("tool_name", "")).lower()

    # Check for test failures first (high specificity)
    if tool_name in ("run_tests", "test_runner") or any(m in message for m in _TEST_FAILURE_MARKERS):
        return "test_failure"

    # Lint/type check errors
    if tool_name in ("run_lint", "type_check") or any(m in message for m in _LINT_ERROR_MARKERS):
        return "lint_error"

    # Transient / tool errors
    for marker in _TRANSIENT_MARKERS:
        if marker in message:
            return "tool_error"

    # Context missing (file/symbol not found)
    for marker in _CONTEXT_MISSING_MARKERS:
        if marker in message:
            return "context_missing"

    # Environmental issues → approach_wrong (escalation-worthy)
    for marker in _ENVIRONMENTAL_MARKERS:
        if marker in message:
            return "approach_wrong"

    # Default: semantic → context_missing (most common semantic failure is "not found")
    return "context_missing"


def get_recovery_strategy(failure_class: str) -> str:
    """Return an actionable recovery message for the given failure class."""
    return RECOVERY_STRATEGIES.get(failure_class, "")


# Backwards-compatible private alias — some internal callers import the
# underscore-prefixed name.
_classify_tool_failure = classify_tool_failure
