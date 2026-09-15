"""Turn a ``cli.checks`` result into a step response problem.

Kept outside ``henchmen.console.steps`` (never named ``responses.py`` there)
so it is never mistaken for a step module during discovery (ruling C11/C15):
the response models themselves (``StepProblem``, ``StepSuccess``,
``StepFailure``) and the ``step_succeeded``/``step_failed`` helpers are
defined exactly once, in ``henchmen.console.steps``.
"""

from __future__ import annotations

from henchmen.cli.checks import CheckResult
from henchmen.console.steps import StepProblem
from henchmen.utils.redaction import redact

__all__ = ["problem_from_check"]


def problem_from_check(result: CheckResult, *, field: str | None = None) -> StepProblem:
    """Turn a ``cli.checks`` result into a problem; its hint becomes the action.

    A check's message and hint can echo back attacker- or user-supplied text
    (a submitted key, a basic-auth URL) verbatim from an SDK exception or an
    HTTP error body, so both are redacted before they ever reach a step
    response.
    """
    message = redact(f"{result.name}: {result.message}")
    action = redact(result.hint) if result.hint else result.hint
    return StepProblem(field=field, message=message, action=action)
