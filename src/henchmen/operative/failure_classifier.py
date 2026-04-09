"""Classify tool failures into transient / semantic / environmental categories.

The agent loop distinguishes between three kinds of tool-call failures so it
can react appropriately:

* ``transient``   — the tool call could succeed if retried (e.g. rate limit,
  temporary network blip, ``OSError: too many open files``). The agent should
  retry the same call.
* ``semantic``    — the tool ran correctly but returned a result the model
  cannot act on as written (e.g. ``grep_search returned no matches``, "old
  text not found" from ``file_edit``). Retrying the identical call is
  pointless; the model must adapt its strategy.
* ``environmental`` — the tool failed because the surrounding environment is
  broken (missing dependency, permission denied, disk full). Retrying will
  not help; the operative should escalate.

The classifier is deliberately a small heuristic function, not a model call,
so it can be used in hot paths without latency cost.
"""

from __future__ import annotations

from typing import Any, Literal

FailureClass = Literal["transient", "semantic", "environmental", "none"]


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
    "not found",
    "no such",
    "enospc",
    "disk full",
    "read-only file system",
    "command not found",
    "no space left",
)


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

    for marker in _TRANSIENT_MARKERS:
        if marker in message:
            return "transient"

    for marker in _ENVIRONMENTAL_MARKERS:
        if marker in message:
            return "environmental"

    return "semantic"


# Backwards-compatible private alias — some internal callers import the
# underscore-prefixed name.
_classify_tool_failure = classify_tool_failure
