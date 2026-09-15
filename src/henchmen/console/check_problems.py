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

__all__ = ["problem_from_check"]


def problem_from_check(result: CheckResult, *, field: str | None = None) -> StepProblem:
    """Turn a ``cli.checks`` result into a problem; its hint becomes the action."""
    return StepProblem(field=field, message=f"{result.name}: {result.message}", action=result.hint)
