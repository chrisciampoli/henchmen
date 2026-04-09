"""Post-operative evaluation using Vertex AI GenAI Evaluation Service.

Evaluates operative results for quality metrics: task fulfillment,
tool call validity, and safety compliance.  Results are persisted to
Firestore alongside the task execution record.

Feature-flagged via ``vertex_ai_evaluation_enabled`` in settings.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from henchmen.models.evaluation import EvaluationResult
from henchmen.models.operative import OperativeReport, OperativeStatus

logger = logging.getLogger(__name__)

# File path markers used to classify files_changed into source vs test/doc.
_TEST_PATH_MARKERS: tuple[str, ...] = (
    "/tests/",
    "\\tests\\",
    "tests/",
    "test_",
    "_test.",
    ".test.",
    ".spec.",
    "spec/",
    "__tests__/",
)
_DOC_EXTENSIONS: tuple[str, ...] = (".md", ".rst", ".txt", ".adoc")
_DOC_PATH_MARKERS: tuple[str, ...] = ("docs/", "doc/", "README", "CHANGELOG")


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in _TEST_PATH_MARKERS)


def _is_doc_path(path: str) -> bool:
    if path.endswith(_DOC_EXTENSIONS):
        return True
    return any(marker in path for marker in _DOC_PATH_MARKERS)


def _is_source_path(path: str) -> bool:
    """A source file is anything that is neither a test nor a doc."""
    return not _is_test_path(path) and not _is_doc_path(path)


def _task_is_doc_only(task_title: str, task_description: str) -> bool:
    """Best-effort heuristic — True when the task is purely documentation."""
    text = f"{task_title}\n{task_description}".lower()
    doc_signals = ("documentation", "docs:", "readme", "changelog", "typo in doc")
    if any(signal in text for signal in doc_signals):
        # And no implementation verbs
        impl_signals = ("implement", "fix bug", "add feature", "refactor", "optimize")
        if not any(signal in text for signal in impl_signals):
            return True
    return False


def _extract_mentioned_paths(task_title: str, task_description: str) -> list[str]:
    """Extract file paths and dotted module names referenced in the task.

    Looks for tokens that look like ``a/b/c.py``, ``src/foo/bar.ts``, etc.
    Returns lowercased tokens for case-insensitive matching.
    """
    text = f"{task_title}\n{task_description}"
    # Naive but effective: any token with a slash and a dot, or ending in a
    # known code extension.
    pattern = re.compile(
        r"[\w\-./\\]+\.(?:py|ts|tsx|js|jsx|go|rs|java|kt|swift|c|cc|cpp|h|hpp|"
        r"rb|php|cs|scala|sql|yaml|yml|toml|json|md|rst)",
        re.IGNORECASE,
    )
    return [match.group(0).lower() for match in pattern.finditer(text)]


def _extract_mentioned_symbols(task_title: str, task_description: str) -> list[str]:
    """Extract snake_case and CamelCase identifiers referenced in the task."""
    text = f"{task_title}\n{task_description}"
    # snake_case with at least one underscore, or CamelCase with 2+ caps
    pattern = re.compile(r"\b(?:[a-z][a-z0-9_]*_[a-z0-9_]+|[A-Z][a-zA-Z0-9]*[A-Z][a-zA-Z0-9]*)\b")
    return [match.group(0) for match in pattern.finditer(text)]


def compute_diff_signal(
    task_title: str,
    task_description: str,
    report: OperativeReport,
) -> float:
    """Return a 0.0–1.0 score reflecting whether the diff matches the task.

    Scoring rules:

    * Empty ``files_changed`` → 0.0.
    * Files changed but none are source files (and the task is not doc-only)
      → 0.3 (weak signal — maybe only tests or docs touched).
    * Source files changed → 0.7 base.
    * If the task mentions specific files or symbols and at least one
      appears in ``files_changed`` → +0.3 (capped at 1.0).
    """
    files_changed = report.files_changed or []
    if not files_changed:
        return 0.0

    is_doc_task = _task_is_doc_only(task_title, task_description)
    source_files = [f for f in files_changed if _is_source_path(f)]

    if not source_files and not is_doc_task:
        return 0.3

    score = 0.7

    mentioned_paths = _extract_mentioned_paths(task_title, task_description)
    mentioned_symbols = _extract_mentioned_symbols(task_title, task_description)

    if mentioned_paths or mentioned_symbols:
        matched = False
        lower_files = [f.lower() for f in files_changed]
        for mentioned in mentioned_paths:
            if any(mentioned in lf or lf.endswith(mentioned) for lf in lower_files):
                matched = True
                break
        if not matched and mentioned_symbols:
            # Match symbol against file stem (e.g. parse_date → parse_date.py
            # or dates.py containing parse_date). File-name substring match is
            # a cheap heuristic.
            for sym in mentioned_symbols:
                if any(sym.lower() in lf for lf in lower_files):
                    matched = True
                    break
        if matched:
            score = min(1.0, score + 0.3)

    return score


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

        Scoring (L8 fix) combines three signals:

        * ``diff_signal`` — derived from ``report.files_changed`` (0.4 weight).
          An empty diff scores 0; only touching tests/docs on a code task
          scores 0.3; touching source files scores 0.7 and a mention-match
          bonus pushes it toward 1.0.
        * ``fulfillment`` — the Vertex AI fulfillment metric (0.4 weight).
          Still computed from ``report.summary`` via the Evaluation API when
          available; falls back to 0.5 when the API is not reachable.
        * ``safety`` — the Vertex AI safety metric (0.2 weight). Falls back
          to 1.0 (assumed safe) when the API is unavailable.

        As a hard override, any completed operative whose ``diff_signal`` is
        0 (zero-diff completion) has its ``overall_quality`` set to 0 — those
        are suspicious and should not be trusted regardless of what the
        summary text says.

        Args:
            task_title: The original task title.
            task_description: The original task description.
            report: The operative's completion report.
            node_instruction: The scheme node instruction template.

        Returns:
            EvaluationResult with quality scores.
        """
        diff_signal = compute_diff_signal(task_title, task_description, report)

        fulfillment = 0.5  # neutral fallback when the Vertex API is unavailable
        safety = 1.0  # assume safe until proven otherwise
        vertex_error: str | None = None

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
            fulfillment = float(summary_metrics.get("fulfillment/mean", fulfillment))
            safety = float(summary_metrics.get("safety/mean", safety))
        except ImportError:
            logger.info("vertexai.evaluation not available, using diff-signal only")
            vertex_error = "vertexai.evaluation not available"
        except Exception as exc:
            logger.warning("Vertex evaluation failed, using diff-signal only: %s", exc)
            vertex_error = str(exc)

        # Weighted combination (L8): 0.4 diff + 0.4 fulfillment + 0.2 safety.
        overall = diff_signal * 0.4 + fulfillment * 0.4 + safety * 0.2

        # Hard override: zero-diff completions are suspicious.
        if diff_signal == 0.0 and report.status == OperativeStatus.COMPLETED:
            logger.warning(
                "Zero-diff completion for task %s — overriding overall_quality to 0",
                report.task_id,
            )
            overall = 0.0

        return EvaluationResult(
            fulfillment_score=fulfillment,
            tool_call_valid_score=diff_signal,  # reuse unused field for diff signal visibility
            safety_score=safety,
            overall_quality=overall,
            evaluation_error=vertex_error,
        )


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
