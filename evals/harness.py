"""Offline evaluation harness for Henchmen schemes.

This module provides a runnable-without-GCP test harness that executes a
Henchmen scheme against a self-contained fixture repository and scores the
resulting git diff against a rubric shipped alongside the fixture.

Design notes
------------
The harness deliberately does NOT spin up Cloud Run Jobs, Pub/Sub, or any
cloud container. It substitutes an in-process "lair simulator" in place of
the real ``LairManager`` so that agentic scheme nodes call the supplied
``LLMProvider`` directly. This sacrifices some fidelity with the production
path in exchange for:

* zero cloud credentials required
* deterministic replay
* sub-minute fixture runs

Because the simulator bypasses the real operative tool loop, its scoring is
based strictly on the git diff the LLM produced via an ``apply_patch``-style
tool call. The diff-based rubric is the single source of truth — we never
grade on the agent's free-text summary.

Public surface
--------------
* ``run_fixture(...)`` - execute a single fixture and return ``FixtureResult``.
* ``run_all_fixtures(...)`` - execute every fixture and return ``EvalReport``.
* ``score_result(...)`` - score a post-run workspace against ``diff_patterns.json``.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from henchmen.models.llm import LLMResponse, Message, MessageRole, ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from henchmen.config.settings import Settings
    from henchmen.providers.interfaces.llm_provider import LLMProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic result models
# ---------------------------------------------------------------------------


class DimensionScores(BaseModel):
    """Five-dimensional quality score for a fixture run.

    Each dimension is a float in [0.0, 1.0]. The weighted composite is
    computed via ``compute_weighted_score()``.

    Weights:
        correctness  40%  — tests_pass * 0.6 + touched_expected_files * 0.2 + contains_expected_substrings * 0.2
        precision    20%  — penalises extra changed files and diff size vs expected
        conventions  15%  — lint pass, type check pass
        efficiency   15%  — steps, tokens, time normalised against per-fixture budgets
        completion   10%  — finished without error/timeout
    """

    correctness: float = Field(default=0.0, ge=0.0, le=1.0, description="Tests + files + substrings composite")
    precision: float = Field(default=0.0, ge=0.0, le=1.0, description="Penalises extra files / oversized diffs")
    conventions: float = Field(default=0.0, ge=0.0, le=1.0, description="Lint and type-check pass rate")
    efficiency: float = Field(default=0.0, ge=0.0, le=1.0, description="Steps/tokens/time vs fixture budgets")
    completion: float = Field(default=0.0, ge=0.0, le=1.0, description="Finished without error or timeout")

    _WEIGHTS: ClassVar[dict[str, float]] = {
        "correctness": 0.40,
        "precision": 0.20,
        "conventions": 0.15,
        "efficiency": 0.15,
        "completion": 0.10,
    }

    def compute_weighted_score(self) -> float:
        """Return the weighted composite score in [0.0, 1.0]."""
        raw = (
            self.correctness * self._WEIGHTS["correctness"]
            + self.precision * self._WEIGHTS["precision"]
            + self.conventions * self._WEIGHTS["conventions"]
            + self.efficiency * self._WEIGHTS["efficiency"]
            + self.completion * self._WEIGHTS["completion"]
        )
        return round(max(0.0, min(1.0, raw)), 4)


class FixtureMeta(BaseModel):
    """Optional per-fixture budget metadata loaded from ``diff_patterns.json``."""

    expected_diff_lines: int | None = Field(
        default=None, ge=0, description="Expected number of diff lines (for precision scoring)"
    )
    max_expected_steps: int | None = Field(
        default=None, ge=1, description="Budget for agent loop steps (for efficiency scoring)"
    )
    max_expected_tokens: int | None = Field(
        default=None, ge=1, description="Budget for total tokens (for efficiency scoring)"
    )
    lint_command: list[str] | None = Field(default=None, description="Lint command to run for convention scoring")


class FixtureScore(BaseModel):
    """Diff-based quality score for a single fixture run."""

    fixture_id: str = Field(..., description="Fixture directory name")
    diff_non_empty: bool = Field(..., description="Whether the agent produced any diff at all")
    touched_expected_files: bool = Field(..., description="Whether the required files were modified")
    tests_pass: bool | None = Field(
        default=None, description="Whether the fixture's test suite passes (None if no runner)"
    )
    contains_expected_substrings: bool = Field(..., description="Whether the diff contains the required substrings")
    overall_score: float = Field(..., ge=0.0, le=1.0, description="Weighted overall score in [0.0, 1.0]")
    dimensions: DimensionScores | None = Field(
        default=None, description="Five-dimensional scores (None for legacy runs)"
    )


class FixtureResult(BaseModel):
    """Outcome of running a single fixture through the harness."""

    fixture_id: str = Field(..., description="Fixture directory name")
    provider: str = Field(..., description="LLM provider name (openai, anthropic, vertex, ollama, ...)")
    model_tier: str = Field(..., description="Model tier or concrete model used")
    score: FixtureScore = Field(..., description="Diff-based score")
    wall_clock_seconds: float = Field(..., ge=0.0, description="End-to-end wall clock time")
    total_input_tokens: int = Field(default=0, ge=0, description="Sum of input tokens across LLM calls")
    total_output_tokens: int = Field(default=0, ge=0, description="Sum of output tokens across LLM calls")
    estimated_cost_usd: float = Field(default=0.0, ge=0.0, description="Estimated provider cost in USD")
    steps_taken: int = Field(default=0, ge=0, description="Number of agent loop steps executed")
    error: str | None = Field(default=None, description="Traceback summary if the run crashed")


class EvalReport(BaseModel):
    """Aggregated report across all fixtures for a provider."""

    provider: str = Field(..., description="LLM provider name")
    commit_sha: str | None = Field(default=None, description="Henchmen repo commit SHA at run time")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC), description="Report generation timestamp")
    results: list[FixtureResult] = Field(default_factory=list, description="Per-fixture results")
    aggregate_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Mean of per-fixture overall_score")


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


class _FixtureSpec(BaseModel):
    """In-memory representation of a fixture directory."""

    fixture_id: str
    root: Path
    repo_dir: Path
    task: dict[str, Any]
    diff_patterns: dict[str, Any]
    meta: FixtureMeta = Field(default_factory=FixtureMeta)


def _load_fixture(fixture_dir: Path) -> _FixtureSpec:
    """Load ``task.json`` and ``expected/diff_patterns.json`` from a fixture."""
    task_path = fixture_dir / "task.json"
    diff_path = fixture_dir / "expected" / "diff_patterns.json"
    repo_dir = fixture_dir / "repo"
    if not task_path.is_file():
        raise FileNotFoundError(f"Fixture {fixture_dir.name} missing task.json")
    if not diff_path.is_file():
        raise FileNotFoundError(f"Fixture {fixture_dir.name} missing expected/diff_patterns.json")
    if not repo_dir.is_dir():
        raise FileNotFoundError(f"Fixture {fixture_dir.name} missing repo/ directory")
    patterns = json.loads(diff_path.read_text(encoding="utf-8"))
    meta = FixtureMeta(
        expected_diff_lines=patterns.get("expected_diff_lines"),
        max_expected_steps=patterns.get("max_expected_steps"),
        max_expected_tokens=patterns.get("max_expected_tokens"),
        lint_command=patterns.get("lint_command"),
    )
    return _FixtureSpec(
        fixture_id=fixture_dir.name,
        root=fixture_dir,
        repo_dir=repo_dir,
        task=json.loads(task_path.read_text(encoding="utf-8")),
        diff_patterns=patterns,
        meta=meta,
    )


def _copy_repo_to_workspace(repo_dir: Path, workspace: Path) -> Path:
    """Copy the fixture ``repo/`` contents into ``workspace`` and ``git init`` it."""
    target = workspace / "repo"
    shutil.copytree(repo_dir, target)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "eval@henchmen.local"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "Henchmen Eval"], cwd=target, check=True)
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "eval: initial fixture state"],
        cwd=target,
        check=True,
    )
    return target


def _git_diff(workspace: Path) -> str:
    """Return ``git diff HEAD`` of the workspace."""
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def _git_changed_files(workspace: Path) -> list[str]:
    """Return paths of files changed vs HEAD (staged + unstaged + untracked)."""
    tracked = subprocess.run(
        ["git", "diff", "HEAD", "--name-only"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    files: list[str] = []
    for line in (tracked.stdout + untracked.stdout).splitlines():
        name = line.strip()
        if name:
            files.append(name)
    return files


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


_WEIGHT_DIFF_NON_EMPTY = 0.25
_WEIGHT_TOUCHED_FILES = 0.25
_WEIGHT_TESTS_PASS = 0.35
_WEIGHT_SUBSTRINGS = 0.15


def _run_fixture_tests(workspace: Path, diff_patterns: dict[str, Any]) -> bool | None:
    """Run the fixture's test command if one is declared. ``None`` means not applicable."""
    command = diff_patterns.get("test_command")
    if not command:
        return None
    try:
        result = subprocess.run(
            list(command),
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("Fixture test runner unavailable: %s", exc)
        return None
    return result.returncode == 0


def _compute_correctness(
    tests_pass: bool | None,
    touched_expected_files: bool,
    contains_expected_substrings: bool,
) -> float:
    """Correctness dimension: tests 60%, files 20%, substrings 20%.

    When ``tests_pass`` is None (no test runner), redistribute its weight
    so the dimension still ranges over [0.0, 1.0].
    """
    if tests_pass is None:
        files_w, subs_w = 0.5, 0.5
        return (files_w if touched_expected_files else 0.0) + (subs_w if contains_expected_substrings else 0.0)
    return (
        (0.6 if tests_pass else 0.0)
        + (0.2 if touched_expected_files else 0.0)
        + (0.2 if contains_expected_substrings else 0.0)
    )


def _compute_precision(
    changed_files: list[str],
    expected_files: list[str],
    diff_line_count: int,
    expected_diff_lines: int | None,
) -> float:
    """Precision dimension: penalise extra files and oversized diffs.

    Returns 1.0 for a minimal, targeted change and degrades toward 0.0 as
    the agent touches unnecessary files or produces oversized diffs.
    """
    # File precision: ratio of expected files to total changed files.
    if not changed_files:
        return 0.0
    expected_set = set(expected_files)
    touched_expected_count = sum(1 for f in changed_files if any(e in f for e in expected_set))
    extra_files = len(changed_files) - touched_expected_count
    file_precision = max(0.0, 1.0 - (extra_files * 0.25)) if changed_files else 1.0

    # Diff-size precision: ratio of expected diff lines to actual diff lines.
    if expected_diff_lines is not None and expected_diff_lines > 0 and diff_line_count > 0:
        ratio = diff_line_count / expected_diff_lines
        # Perfect at ratio=1, degrades as ratio grows past 1
        diff_precision = max(0.0, min(1.0, 1.0 / ratio)) if ratio > 1.0 else 1.0
    else:
        # No budget set — assume diff size is fine
        diff_precision = 1.0

    return round(max(0.0, min(1.0, file_precision * 0.5 + diff_precision * 0.5)), 4)


def _compute_conventions(workspace: Path, lint_command: list[str] | None) -> float:
    """Conventions dimension: lint pass = 1.0, fail = 0.0.

    When no ``lint_command`` is configured, returns 1.0 (assume conventions met).
    """
    if not lint_command:
        return 1.0
    try:
        result = subprocess.run(
            lint_command,
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Linter not available — don't penalise
        return 1.0
    return 1.0 if result.returncode == 0 else 0.0


def _compute_efficiency(
    steps: int,
    total_tokens: int,
    wall_clock: float,
    meta: FixtureMeta,
) -> float:
    """Efficiency dimension: normalise steps/tokens/time against budgets.

    Each sub-signal scores 1.0 if under budget and degrades linearly to 0.0
    at 2x the budget. When a budget is not set, that sub-signal is omitted
    and the remaining signals share its weight equally.
    """
    signals: list[float] = []

    if meta.max_expected_steps is not None and meta.max_expected_steps > 0:
        ratio = steps / meta.max_expected_steps
        signals.append(max(0.0, min(1.0, 2.0 - ratio)))

    if meta.max_expected_tokens is not None and meta.max_expected_tokens > 0:
        ratio = total_tokens / meta.max_expected_tokens
        signals.append(max(0.0, min(1.0, 2.0 - ratio)))

    if not signals:
        return 1.0  # No budget configured — assume efficient
    return round(sum(signals) / len(signals), 4)


def _compute_completion(error: str | None, finished: bool) -> float:
    """Completion dimension: 1.0 if finished cleanly, 0.0 otherwise."""
    if error is not None:
        return 0.0
    return 1.0 if finished else 0.5


def compute_dimensions(
    *,
    tests_pass: bool | None,
    touched_expected_files: bool,
    contains_expected_substrings: bool,
    changed_files: list[str],
    expected_files: list[str],
    diff_line_count: int,
    workspace: Path,
    steps: int,
    total_tokens: int,
    wall_clock: float,
    meta: FixtureMeta,
    error: str | None,
    finished: bool,
) -> DimensionScores:
    """Build a ``DimensionScores`` from raw run data."""
    return DimensionScores(
        correctness=_compute_correctness(tests_pass, touched_expected_files, contains_expected_substrings),
        precision=_compute_precision(changed_files, expected_files, diff_line_count, meta.expected_diff_lines),
        conventions=_compute_conventions(workspace, meta.lint_command),
        efficiency=_compute_efficiency(steps, total_tokens, wall_clock, meta),
        completion=_compute_completion(error, finished),
    )


def score_result(fixture_dir: Path, workspace: Path) -> FixtureScore:
    """Compute a ``FixtureScore`` for a post-run workspace.

    The workspace must be a git repository whose initial commit is the
    pristine fixture state. The diff between HEAD and the working tree is
    what we grade.
    """
    spec = _load_fixture(fixture_dir)
    diff_text = _git_diff(workspace)
    changed_files = _git_changed_files(workspace)

    diff_non_empty = bool(diff_text.strip()) or bool(changed_files)

    expected_files: list[str] = list(spec.diff_patterns.get("must_contain_file_change", []))
    touched_expected_files = (
        all(any(expected in changed for changed in changed_files) for expected in expected_files)
        if expected_files
        else True
    )

    substrings: list[str] = list(spec.diff_patterns.get("expected_substrings_in_changed_code", []))
    contains_expected_substrings = all(s in diff_text for s in substrings) if substrings else True

    tests_pass: bool | None = None
    if spec.diff_patterns.get("must_fix_tests"):
        tests_pass = _run_fixture_tests(workspace, spec.diff_patterns)

    # Weighted score. When tests_pass is None, redistribute its weight across
    # the other signals so each fixture's overall_score stays on [0, 1].
    if tests_pass is None:
        total_weight = _WEIGHT_DIFF_NON_EMPTY + _WEIGHT_TOUCHED_FILES + _WEIGHT_SUBSTRINGS
        score = (
            (_WEIGHT_DIFF_NON_EMPTY if diff_non_empty else 0.0)
            + (_WEIGHT_TOUCHED_FILES if touched_expected_files else 0.0)
            + (_WEIGHT_SUBSTRINGS if contains_expected_substrings else 0.0)
        ) / total_weight
    else:
        score = (
            (_WEIGHT_DIFF_NON_EMPTY if diff_non_empty else 0.0)
            + (_WEIGHT_TOUCHED_FILES if touched_expected_files else 0.0)
            + (_WEIGHT_TESTS_PASS if tests_pass else 0.0)
            + (_WEIGHT_SUBSTRINGS if contains_expected_substrings else 0.0)
        )

    return FixtureScore(
        fixture_id=spec.fixture_id,
        diff_non_empty=diff_non_empty,
        touched_expected_files=touched_expected_files,
        tests_pass=tests_pass,
        contains_expected_substrings=contains_expected_substrings,
        overall_score=round(max(0.0, min(1.0, score)), 4),
    )


# ---------------------------------------------------------------------------
# Minimal in-process agent loop
# ---------------------------------------------------------------------------


_APPLY_PATCH_TOOL = ToolDefinition(
    name="apply_patch",
    description=(
        "Apply an edit to a single file in the fixture workspace. Use this to make "
        "the minimal change required to fix the bug or add the feature. You may "
        "call this tool multiple times."
    ),
    parameters=[
        ToolParameter(
            name="path",
            type="string",
            description="Relative path of the file to modify (e.g. 'list_utils.py').",
        ),
        ToolParameter(
            name="contents",
            type="string",
            description="The full new contents of the file after the edit.",
        ),
    ],
)

_FINISH_TOOL = ToolDefinition(
    name="finish",
    description="Signal that you have completed all edits and the task is done.",
    parameters=[
        ToolParameter(
            name="summary",
            type="string",
            description="A one-line summary of what you changed.",
            required=False,
        ),
    ],
)


def _build_user_prompt(task: dict[str, Any], workspace: Path) -> str:
    """Construct the user prompt with task details and file contents."""
    lines = [
        f"TITLE: {task.get('title', '')}",
        "",
        f"DESCRIPTION:\n{task.get('description', '')}",
        "",
        "REPOSITORY FILES:",
    ]
    for path in sorted(workspace.rglob("*")):
        if ".git" in path.parts or not path.is_file():
            continue
        rel = path.relative_to(workspace).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lines.append(f"\n--- FILE: {rel} ---\n{content}")
    lines.append("\nCall apply_patch as many times as needed, then call finish. Make the minimal change.")
    return "\n".join(lines)


class _AgentLoopResult(BaseModel):
    """Return value of the in-process agent loop."""

    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    steps_taken: int = Field(default=0, ge=0)
    finished: bool = Field(default=False, description="Whether the agent called the finish tool")


async def _run_agent_loop(
    llm_provider: LLMProvider,
    workspace: Path,
    task: dict[str, Any],
    model: str,
    max_steps: int = 6,
) -> _AgentLoopResult:
    """Drive a minimal apply_patch/finish agent loop against ``llm_provider``.

    Returns an ``_AgentLoopResult`` with token counts, step count, and
    whether the agent cleanly called ``finish``.
    """
    messages: list[Message] = [
        Message(role=MessageRole.USER, content=_build_user_prompt(task, workspace)),
    ]
    system_prompt = (
        "You are Henchmen's offline evaluation agent. Fix the described issue by "
        "calling apply_patch with the new file contents, then call finish. Never "
        "reply with free text — always call a tool."
    )

    total_in = 0
    total_out = 0
    steps = 0
    agent_finished = False
    for _step in range(max_steps):
        steps += 1
        response: LLMResponse = await llm_provider.generate(
            messages=messages,
            model=model,
            tools=[_APPLY_PATCH_TOOL, _FINISH_TOOL],
            temperature=0.0,
            system_prompt=system_prompt,
        )
        total_in += response.usage.input_tokens
        total_out += response.usage.output_tokens
        if not response.tool_calls:
            logger.info("Agent returned no tool calls, halting loop")
            break
        for call in response.tool_calls:
            if call.name == "apply_patch":
                _apply_patch(workspace, call.arguments)
            elif call.name == "finish":
                agent_finished = True
        messages.append(Message(role=MessageRole.ASSISTANT, content=response.content, tool_calls=response.tool_calls))
        for call in response.tool_calls:
            messages.append(
                Message(
                    role=MessageRole.TOOL,
                    content="ok",
                    tool_call_id=call.id,
                )
            )
        if agent_finished:
            break
    return _AgentLoopResult(
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        steps_taken=steps,
        finished=agent_finished,
    )


def _apply_patch(workspace: Path, arguments: dict[str, Any]) -> None:
    """Write ``arguments['contents']`` to ``arguments['path']`` inside the workspace."""
    rel = str(arguments.get("path", "")).strip()
    contents = arguments.get("contents", "")
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        logger.warning("Refusing unsafe apply_patch path: %r", rel)
        return
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents if isinstance(contents, str) else str(contents), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public runners
# ---------------------------------------------------------------------------


async def run_fixture(
    fixture_dir: Path,
    llm_provider: LLMProvider,
    *,
    scheme_registry: Any = None,
    workspace_root: Path | None = None,
    settings: Settings | None = None,
) -> FixtureResult:
    """Execute a single fixture and return a scored ``FixtureResult``.

    Parameters
    ----------
    fixture_dir:
        Directory containing ``task.json``, ``repo/``, and ``expected/``.
    llm_provider:
        Any object satisfying the ``LLMProvider`` protocol.
    scheme_registry:
        Optional scheme registry. Accepted for API compatibility with the
        production executor but not required — the in-process harness reads
        the scheme name from ``task.json`` and uses it to pick an LLM tier.
    workspace_root:
        Optional parent directory for the temp workspace. Defaults to the
        system temp dir.
    settings:
        Optional Henchmen ``Settings``. Passed through for provider APIs that
        require it; the harness itself only reads the model tier.
    """
    spec = _load_fixture(fixture_dir)
    started = time.perf_counter()
    provider_name = type(llm_provider).__name__.replace("Provider", "").lower() or "unknown"

    # Pick a concrete model. Agentic schemes in production use
    # ``default/complex`` for implement_fix / implement_feature — we do the same.
    try:
        model = llm_provider.resolve_tier("default/complex")
    except Exception:
        supported = llm_provider.supported_models()
        model = supported[0] if supported else "unknown"

    error: str | None = None
    loop_result = _AgentLoopResult()
    cost_usd = 0.0
    parent = Path(workspace_root) if workspace_root else Path(tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"henchmen-eval-{spec.fixture_id}-", dir=parent) as tmp:
        workspace = _copy_repo_to_workspace(spec.repo_dir, Path(tmp))
        try:
            loop_result = await _run_agent_loop(llm_provider, workspace, spec.task, model)
        except Exception as exc:
            logger.exception("Agent loop crashed for fixture %s", spec.fixture_id)
            error = f"{type(exc).__name__}: {exc}"

        wall_clock = time.perf_counter() - started
        score: FixtureScore = score_result(fixture_dir, workspace)

        # Compute dimension scores alongside the legacy boolean score.
        diff_text = _git_diff(workspace)
        changed_files = _git_changed_files(workspace)
        expected_files: list[str] = list(spec.diff_patterns.get("must_contain_file_change", []))
        diff_line_count = len(diff_text.splitlines())
        total_tokens = loop_result.total_input_tokens + loop_result.total_output_tokens

        dimensions = compute_dimensions(
            tests_pass=score.tests_pass,
            touched_expected_files=score.touched_expected_files,
            contains_expected_substrings=score.contains_expected_substrings,
            changed_files=changed_files,
            expected_files=expected_files,
            diff_line_count=diff_line_count,
            workspace=workspace,
            steps=loop_result.steps_taken,
            total_tokens=total_tokens,
            wall_clock=wall_clock,
            meta=spec.meta,
            error=error,
            finished=loop_result.finished,
        )
        score.dimensions = dimensions

    try:
        from henchmen.observability.tracker import estimate_cost

        cost_usd = float(estimate_cost(model, loop_result.total_input_tokens, loop_result.total_output_tokens))
    except Exception:
        cost_usd = 0.0

    return FixtureResult(
        fixture_id=spec.fixture_id,
        provider=provider_name,
        model_tier=model,
        score=score,
        wall_clock_seconds=round(wall_clock, 3),
        total_input_tokens=loop_result.total_input_tokens,
        total_output_tokens=loop_result.total_output_tokens,
        estimated_cost_usd=round(cost_usd, 6),
        steps_taken=loop_result.steps_taken,
        error=error,
    )


async def run_all_fixtures(
    fixtures_dir: Path,
    llm_provider: LLMProvider,
    *,
    settings: Settings | None = None,
) -> EvalReport:
    """Run every fixture under ``fixtures_dir`` and return the aggregated report."""
    fixture_dirs = sorted(p for p in fixtures_dir.iterdir() if p.is_dir())
    results: list[FixtureResult] = []
    for fixture_dir in fixture_dirs:
        if not (fixture_dir / "task.json").is_file():
            continue
        result = await run_fixture(fixture_dir, llm_provider, settings=settings)
        results.append(result)

    aggregate = sum(r.score.overall_score for r in results) / len(results) if results else 0.0
    provider_name = results[0].provider if results else type(llm_provider).__name__.replace("Provider", "").lower()
    return EvalReport(
        provider=provider_name,
        commit_sha=_current_commit_sha(),
        results=results,
        aggregate_score=round(aggregate, 4),
    )


def _current_commit_sha() -> str | None:
    """Return the current git commit SHA of the Henchmen repo, or None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent.parent,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        sha = result.stdout.strip()
        return sha or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


__all__ = [
    "DimensionScores",
    "EvalReport",
    "FixtureMeta",
    "FixtureResult",
    "FixtureScore",
    "compute_dimensions",
    "run_all_fixtures",
    "run_fixture",
    "score_result",
]
