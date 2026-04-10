"""Unit tests for the offline evaluation harness."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals.harness import (
    EvalReport,
    FixtureResult,
    FixtureScore,
    run_fixture,
    score_result,
)
from henchmen.models.llm import LLMResponse, Message, TokenUsage, ToolDefinition

# ---------------------------------------------------------------------------
# Pydantic model serialization
# ---------------------------------------------------------------------------


class TestModelsSerialize:
    """The report models must round-trip cleanly via Pydantic."""

    def test_fixture_score_round_trip(self) -> None:
        score = FixtureScore(
            fixture_id="bugfix_off_by_one",
            diff_non_empty=True,
            touched_expected_files=True,
            tests_pass=True,
            contains_expected_substrings=True,
            overall_score=1.0,
        )
        data = score.model_dump()
        restored = FixtureScore.model_validate(data)
        assert restored == score

    def test_fixture_result_accepts_none_tests_pass(self) -> None:
        score = FixtureScore(
            fixture_id="feature_add_logging",
            diff_non_empty=True,
            touched_expected_files=True,
            tests_pass=None,
            contains_expected_substrings=True,
            overall_score=1.0,
        )
        result = FixtureResult(
            fixture_id="feature_add_logging",
            provider="openai",
            model_tier="gpt-4o",
            score=score,
            wall_clock_seconds=1.5,
            total_input_tokens=100,
            total_output_tokens=50,
            estimated_cost_usd=0.0012,
        )
        data = result.model_dump()
        assert data["score"]["tests_pass"] is None
        assert FixtureResult.model_validate(data) == result

    def test_eval_report_json_round_trip(self) -> None:
        report = EvalReport(
            provider="openai",
            commit_sha="deadbeef",
            timestamp=datetime.now(UTC),
            results=[],
            aggregate_score=0.0,
        )
        raw = report.model_dump_json()
        restored = EvalReport.model_validate_json(raw)
        assert restored.provider == "openai"
        assert restored.aggregate_score == 0.0


# ---------------------------------------------------------------------------
# score_result
# ---------------------------------------------------------------------------


def _init_fake_fixture(tmp_path: Path, diff_patterns: dict) -> tuple[Path, Path]:
    """Create a fixture dir + initialised workspace and return ``(fixture, workspace)``."""
    fixture = tmp_path / "fake_fixture"
    (fixture / "repo").mkdir(parents=True)
    (fixture / "expected").mkdir()
    (fixture / "task.json").write_text(
        json.dumps({"title": "t", "description": "d", "scheme": "bugfix_standard"}),
        encoding="utf-8",
    )
    (fixture / "expected" / "diff_patterns.json").write_text(json.dumps(diff_patterns), encoding="utf-8")
    (fixture / "repo" / "sample.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    shutil.copytree(fixture / "repo", workspace / "repo")
    ws = workspace / "repo"
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True)
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=ws, check=True)
    return fixture, ws


class TestScoreResult:
    """``score_result`` should grade strictly off the git diff."""

    def test_perfect_score_when_diff_matches_rubric(self, tmp_path: Path) -> None:
        fixture, ws = _init_fake_fixture(
            tmp_path,
            {
                "must_contain_file_change": ["sample.py"],
                "must_fix_tests": False,
                "expected_substrings_in_changed_code": ["return 2"],
                "test_command": None,
            },
        )
        (ws / "sample.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        score = score_result(fixture, ws)
        assert score.diff_non_empty is True
        assert score.touched_expected_files is True
        assert score.contains_expected_substrings is True
        assert score.tests_pass is None
        assert score.overall_score == pytest.approx(1.0)

    def test_zero_score_when_no_changes(self, tmp_path: Path) -> None:
        fixture, ws = _init_fake_fixture(
            tmp_path,
            {
                "must_contain_file_change": ["sample.py"],
                "must_fix_tests": False,
                "expected_substrings_in_changed_code": ["return 2"],
                "test_command": None,
            },
        )
        score = score_result(fixture, ws)
        assert score.diff_non_empty is False
        assert score.touched_expected_files is False
        assert score.contains_expected_substrings is False
        assert score.overall_score == pytest.approx(0.0)

    def test_partial_score_when_wrong_file_touched(self, tmp_path: Path) -> None:
        fixture, ws = _init_fake_fixture(
            tmp_path,
            {
                "must_contain_file_change": ["sample.py"],
                "must_fix_tests": False,
                "expected_substrings_in_changed_code": ["hello"],
                "test_command": None,
            },
        )
        (ws / "other.py").write_text("hello = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=ws, check=True)
        score = score_result(fixture, ws)
        assert score.diff_non_empty is True
        assert score.touched_expected_files is False
        assert score.contains_expected_substrings is True
        assert 0.0 < score.overall_score < 1.0


# ---------------------------------------------------------------------------
# run_fixture smoke test with a mock LLMProvider
# ---------------------------------------------------------------------------


class _MockLLMProvider:
    """Minimal LLMProvider that returns zero tool calls — an agent that does nothing."""

    async def generate(
        self,
        messages: list[Message],
        model: str,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        system_prompt: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            content="no-op",
            tool_calls=[],
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            model=model,
            finish_reason="stop",
        )

    async def count_tokens(self, text: str, model: str) -> int:
        return len(text) // 4

    def supported_models(self) -> list[str]:
        return ["mock-model"]

    def resolve_tier(self, tier: str) -> str:
        return "mock-model"


REPO_ROOT = Path(__file__).resolve().parents[2]
BUGFIX_FIXTURE = REPO_ROOT / "evals" / "fixtures" / "bugfix_off_by_one"


FEATURE_FIXTURE = REPO_ROOT / "evals" / "fixtures" / "feature_add_logging"


@pytest.mark.asyncio
async def test_run_fixture_with_mock_provider_produces_zero_score(tmp_path: Path) -> None:
    """A provider that returns no tool calls should produce a near-zero overall score.

    We use the feature_add_logging fixture because it has no test runner, so
    the smoke test does not depend on pytest being importable inside the
    workspace's isolated subprocess.
    """
    if not FEATURE_FIXTURE.is_dir():
        pytest.skip("feature_add_logging fixture not checked in")

    provider = _MockLLMProvider()
    result = await run_fixture(
        FEATURE_FIXTURE,
        provider,
        workspace_root=tmp_path,
        settings=None,
    )
    assert isinstance(result, FixtureResult)
    assert result.fixture_id == "feature_add_logging"
    assert result.score.diff_non_empty is False
    assert result.score.touched_expected_files is False
    assert result.score.contains_expected_substrings is False
    assert result.score.tests_pass is None  # fixture has no test runner
    assert result.score.overall_score == pytest.approx(0.0)
    assert result.total_input_tokens >= 10
    assert result.total_output_tokens >= 5
    assert result.error is None
