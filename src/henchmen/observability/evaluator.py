"""Post-operative evaluation using Vertex AI GenAI Evaluation Service.

Evaluates operative results for quality metrics: task fulfillment,
tool call validity, and safety compliance.  Results are persisted to
Firestore alongside the task execution record.

Feature-flagged via ``vertex_ai_evaluation_enabled`` in settings.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from henchmen.models.evaluation import EvaluationResult
from henchmen.models.operative import OperativeReport, OperativeStatus

if TYPE_CHECKING:
    from henchmen.observability.tracker import TaskTracker

logger = logging.getLogger(__name__)

# Seconds to wait on the (blocking, LLM-judge-backed) Vertex evaluation call
# before giving up and falling back to the diff signal.
_EVALUATION_TIMEOUT_SECONDS = 120.0

# Path classification for files_changed. Markers are anchored to path segments
# and file names: a bare ``"test_" in path`` substring test misfiles source
# files such as ``latest_report.py`` or ``contest_rules.py`` as tests, which
# drops diff_signal from 0.7 to 0.3 and with it 40% of overall_quality.
_TEST_DIR_SEGMENTS: frozenset[str] = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing"})
_TEST_NAME_MARKERS: tuple[str, ...] = ("_test.", ".test.", ".spec.", "_spec.")
_DOC_DIR_SEGMENTS: frozenset[str] = frozenset({"doc", "docs"})
_DOC_EXTENSIONS: tuple[str, ...] = (".md", ".rst", ".txt", ".adoc")
_DOC_NAME_PREFIXES: tuple[str, ...] = ("readme", "changelog")


def _segments(path: str) -> list[str]:
    """Lower-cased path segments, with Windows separators normalised."""
    return [segment for segment in path.replace("\\", "/").lower().split("/") if segment]


def _is_test_path(path: str) -> bool:
    parts = _segments(path)
    if not parts:
        return False
    name, directories = parts[-1], parts[:-1]
    if any(directory in _TEST_DIR_SEGMENTS for directory in directories):
        return True
    if name.startswith("test_") or name.startswith("test."):
        return True
    return any(marker in name for marker in _TEST_NAME_MARKERS)


def _is_doc_path(path: str) -> bool:
    parts = _segments(path)
    if not parts:
        return False
    name, directories = parts[-1], parts[:-1]
    if name.endswith(_DOC_EXTENSIONS):
        return True
    if any(directory in _DOC_DIR_SEGMENTS for directory in directories):
        return True
    return name.startswith(_DOC_NAME_PREFIXES)


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


def _clamp_unit(value: float) -> float:
    """Clamp a score into the 0.0-1.0 range EvaluationResult accepts."""
    return max(0.0, min(1.0, value))


def _build_result(
    diff_signal: float,
    fulfillment: float,
    safety: float,
    report: OperativeReport,
    vertex_error: str | None,
) -> EvaluationResult:
    """Combine the three signals into an EvaluationResult (L8 weighting)."""
    overall = diff_signal * 0.4 + fulfillment * 0.4 + safety * 0.2

    # Hard override: zero-diff completions are suspicious.
    if diff_signal == 0.0 and report.status == OperativeStatus.COMPLETED:
        logger.warning(
            "Zero-diff completion for task %s — overriding overall_quality to 0",
            report.task_id,
        )
        overall = 0.0

    return EvaluationResult(
        fulfillment_score=_clamp_unit(fulfillment),
        tool_call_valid_score=_clamp_unit(diff_signal),  # reuse unused field for diff signal visibility
        safety_score=_clamp_unit(safety),
        overall_quality=_clamp_unit(overall),
        evaluation_error=vertex_error,
    )


class OperativeEvaluator:
    """Evaluates operative results using Vertex AI GenAI Evaluation."""

    def __init__(self, project_id: str, region: str = "us-central1") -> None:
        self.project_id = project_id
        self.region = region

    def _run_vertex_evaluation(self, instruction: str, context: str, response: str) -> dict[str, float]:
        """Blocking Vertex evaluation call — always run via ``asyncio.to_thread``.

        ``EvalTask`` rejects a list-of-dicts dataset and its string metric
        registry has no "fulfillment" entry, so the dataset is a dict of columns
        and the metrics are the SDK's own ``PointwiseMetric`` examples.
        """
        import vertexai
        from vertexai.evaluation import EvalTask, MetricPromptTemplateExamples

        vertexai.init(project=self.project_id, location=self.region)

        eval_task = EvalTask(
            dataset={
                "instruction": [instruction],
                "context": [context],
                "response": [response],
            },
            metrics=[
                MetricPromptTemplateExamples.Pointwise.INSTRUCTION_FOLLOWING,
                MetricPromptTemplateExamples.Pointwise.SAFETY,
            ],
        )
        result = eval_task.evaluate()
        summary_metrics = getattr(result, "summary_metrics", {}) or {}
        return {str(key): float(value) for key, value in summary_metrics.items() if value is not None}

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

        if not self.project_id:
            # Vertex evaluation needs a GCP project; without one `vertexai.init`
            # would fail per node on a local/AWS deployment.
            logger.debug("No GCP project configured, using diff-signal only")
            return _build_result(diff_signal, fulfillment, safety, report, "no GCP project configured")

        try:
            instruction = node_instruction or f"Fix: {task_title}"
            summary_metrics = await asyncio.wait_for(
                asyncio.to_thread(
                    self._run_vertex_evaluation,
                    instruction,
                    task_description,
                    report.summary,
                ),
                timeout=_EVALUATION_TIMEOUT_SECONDS,
            )
            # The INSTRUCTION_FOLLOWING rubric scores 1-5; EvaluationResult
            # requires 0-1. SAFETY is already binary 0/1 and must not be
            # rescaled or a "safe" verdict would land below the 0.0 floor.
            raw_fulfillment = summary_metrics.get("instruction_following/mean")
            if raw_fulfillment is not None:
                fulfillment = _clamp_unit((float(raw_fulfillment) - 1.0) / 4.0)
            raw_safety = summary_metrics.get("safety/mean")
            if raw_safety is not None:
                safety = _clamp_unit(float(raw_safety))
        except ImportError:
            logger.info("vertexai.evaluation not available, using diff-signal only")
            vertex_error = "vertexai.evaluation not available"
        except TimeoutError:
            logger.warning(
                "Vertex evaluation timed out after %.0fs, using diff-signal only", _EVALUATION_TIMEOUT_SECONDS
            )
            vertex_error = f"Vertex evaluation timed out after {_EVALUATION_TIMEOUT_SECONDS:.0f}s"
        except Exception as exc:
            logger.warning("Vertex evaluation failed, using diff-signal only: %s", exc)
            vertex_error = str(exc)

        return _build_result(diff_signal, fulfillment, safety, report, vertex_error)


async def evaluate_and_record(
    evaluator: OperativeEvaluator,
    tracker: TaskTracker | None,
    task_id: str,
    task_title: str,
    task_description: str,
    report: OperativeReport,
    node_instruction: str = "",
) -> EvaluationResult:
    """Evaluate an operative result and persist the scores on the task document.

    Persistence goes through the injected ``DocumentStore`` and happens even
    when the Vertex call failed — the diff-signal fallback is the only quality
    signal available in that case, and throwing it away left
    ``evaluation_scores`` permanently absent.
    """
    result = await evaluator.evaluate_operative_result(
        task_title=task_title,
        task_description=task_description,
        report=report,
        node_instruction=node_instruction,
    )

    if tracker is not None:
        await tracker.record_evaluation(task_id, result)

    return result
