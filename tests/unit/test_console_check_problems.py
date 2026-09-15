"""Tests for turning a ``cli.checks`` result into a step response problem."""

from henchmen.cli.checks import CheckResult, CheckStatus
from henchmen.console.check_problems import problem_from_check
from henchmen.console.steps import StepProblem


def test_problem_from_check_uses_the_hint_as_the_action() -> None:
    result = CheckResult("Jira", CheckStatus.FAIL, "returned HTTP 401", hint="Create an API token")
    assert problem_from_check(result, field="api_token") == StepProblem(
        field="api_token", message="Jira: returned HTTP 401", action="Create an API token"
    )
