"""Tests for the scheme executor's optional post-operative evaluation (Vertex AI only)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.config.settings import Settings
from henchmen.mastermind.scheme_executor.executor import SchemeExecutor
from henchmen.models.scheme import SchemeNode
from henchmen.models.task import HenchmenTask


def _executor(**settings_overrides: object) -> SchemeExecutor:
    base: dict[str, object] = {"provider": "local", "gcp_project_id": "proj"}
    base.update(settings_overrides)
    settings = Settings(_env_file=None, **base)  # type: ignore[call-arg, arg-type]
    return SchemeExecutor(MagicMock(), MagicMock(), settings, tracker=MagicMock())


class TestMaybeEvaluate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["local", "aws"])
    async def test_skipped_off_gcp_even_when_enabled(
        self, provider: str, sample_task: HenchmenTask, sample_scheme_node: SchemeNode
    ) -> None:
        executor = _executor(provider=provider, vertex_ai_evaluation_enabled=True)
        with (
            patch("henchmen.observability.evaluator.OperativeEvaluator") as evaluator,
            patch("henchmen.observability.evaluator.evaluate_and_record", new=AsyncMock()) as record,
        ):
            await executor._maybe_evaluate(sample_task, sample_scheme_node, MagicMock())
        evaluator.assert_not_called()
        record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skipped_on_gcp_when_disabled(
        self, sample_task: HenchmenTask, sample_scheme_node: SchemeNode
    ) -> None:
        executor = _executor(provider="gcp", vertex_ai_evaluation_enabled=False)
        with patch("henchmen.observability.evaluator.evaluate_and_record", new=AsyncMock()) as record:
            await executor._maybe_evaluate(sample_task, sample_scheme_node, MagicMock())
        record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_on_gcp_when_enabled(self, sample_task: HenchmenTask, sample_scheme_node: SchemeNode) -> None:
        executor = _executor(provider="gcp", vertex_ai_evaluation_enabled=True)
        outcome = MagicMock(evaluation_error=None, overall_quality=0.9, fulfillment_score=0.8)
        with (
            patch("henchmen.observability.evaluator.OperativeEvaluator") as evaluator,
            patch("henchmen.observability.evaluator.evaluate_and_record", new=AsyncMock(return_value=outcome)) as rec,
        ):
            await executor._maybe_evaluate(sample_task, sample_scheme_node, MagicMock())
        evaluator.assert_called_once_with(project_id="proj", region=executor.settings.gcp_region)
        rec.assert_awaited_once()
