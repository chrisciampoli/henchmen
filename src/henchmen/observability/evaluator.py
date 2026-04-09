"""Post-operative evaluation using Vertex AI GenAI Evaluation Service.

Evaluates operative results for quality metrics: task fulfillment,
tool call validity, and safety compliance.  Results are persisted to
Firestore alongside the task execution record.

Feature-flagged via ``vertex_ai_evaluation_enabled`` in settings.
"""

import logging
from typing import Any

from henchmen.models.evaluation import EvaluationResult
from henchmen.models.operative import OperativeReport

logger = logging.getLogger(__name__)


class OperativeEvaluator:
    """Evaluates operative results using Vertex AI GenAI Evaluation."""

    def __init__(self, project_id: str, region: str = "us-central1") -> None:
        self.project_id = project_id
        self.region = region

    async def evaluate_operative_result(
        self,
        task_title: str,
        task_description: str,
        report: OperativeReport,
        node_instruction: str = "",
    ) -> EvaluationResult:
        """Run GenAI Evaluation on an operative's result.

        Args:
            task_title: The original task title.
            task_description: The original task description.
            report: The operative's completion report.
            node_instruction: The scheme node instruction template.

        Returns:
            EvaluationResult with quality scores.
        """
        try:
            import vertexai
            from vertexai.evaluation import EvalTask

            vertexai.init(project=self.project_id, location=self.region)

            # Build evaluation dataset — single row with the operative's output
            instruction = node_instruction or f"Fix: {task_title}"
            eval_data = [
                {
                    "instruction": instruction,
                    "context": task_description,
                    "response": report.summary,
                }
            ]

            # Run evaluation with fulfillment and safety metrics
            eval_task = EvalTask(
                dataset=eval_data,
                metrics=["fulfillment", "safety"],  # type: ignore[list-item]
            )
            result = eval_task.evaluate()

            # Extract scores from evaluation result
            summary_metrics = getattr(result, "summary_metrics", {}) or {}
            fulfillment = float(summary_metrics.get("fulfillment/mean", 0.0))
            safety = float(summary_metrics.get("safety/mean", 0.0))

            # Overall quality = weighted average
            overall = fulfillment * 0.7 + safety * 0.3

            return EvaluationResult(
                fulfillment_score=fulfillment,
                tool_call_valid_score=0.0,  # Requires trajectory data
                safety_score=safety,
                overall_quality=overall,
            )

        except ImportError:
            logger.info("vertexai.evaluation not available, skipping evaluation")
            return EvaluationResult(evaluation_error="vertexai.evaluation not available")
        except Exception as exc:
            logger.warning("Operative evaluation failed: %s", exc)
            return EvaluationResult(evaluation_error=str(exc))


async def evaluate_and_record(
    evaluator: OperativeEvaluator,
    tracker: Any,
    task_id: str,
    task_title: str,
    task_description: str,
    report: OperativeReport,
    node_instruction: str = "",
) -> EvaluationResult:
    """Evaluate operative result and record scores in Firestore.

    Convenience function that combines evaluation + persistence.
    """
    result = await evaluator.evaluate_operative_result(
        task_title=task_title,
        task_description=task_description,
        report=report,
        node_instruction=node_instruction,
    )

    # Record evaluation scores in Firestore
    if tracker and not result.evaluation_error:
        try:
            import asyncio

            update_data = {
                "evaluation_scores": {
                    "fulfillment": result.fulfillment_score,
                    "tool_call_valid": result.tool_call_valid_score,
                    "safety": result.safety_score,
                    "overall_quality": result.overall_quality,
                },
            }
            await asyncio.to_thread(tracker._collection.document(task_id).update, update_data)
            logger.info(
                "Recorded evaluation for task %s: quality=%.2f",
                task_id,
                result.overall_quality,
            )
        except Exception as exc:
            logger.warning("Failed to record evaluation for task %s: %s", task_id, exc)

    return result
