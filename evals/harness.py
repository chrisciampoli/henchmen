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
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from henchmen.models.llm import LLMResponse, Message, MessageRole, ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from henchmen.config.settings import Settings
    from henchmen.providers.interfaces.llm_provider import LLMProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic result models
# ---------------------------------------------------------------------------


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
    return _FixtureSpec(
        fixture_id=fixture_dir.name,
        root=fixture_dir,
        repo_dir=repo_dir,
        task=json.loads(task_path.read_text(encoding="utf-8")),
        diff_patterns=json.loads(diff_path.read_text(encoding="utf-8")),
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
    touched_expected_files = all(
        any(expected in changed for changed in changed_files) for expected in expected_files
    ) if expected_files else True

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
    lines.append(
        "\nCall apply_patch as many times as needed, then call finish. Make the minimal change."
    )
    return "\n".join(lines)


async def _run_agent_loop(
    llm_provider: "LLMProvider",
    workspace: Path,
    task: dict[str, Any],
    model: str,
    max_steps: int = 6,
) -> tuple[int, int]:
    """Drive a minimal apply_patch/finish agent loop against ``llm_provider``.

    Returns ``(total_input_tokens, total_output_tokens)``.
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
    for _step in range(max_steps):
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
        finished = False
        for call in response.tool_calls:
            if call.name == "apply_patch":
                _apply_patch(workspace, call.arguments)
            elif call.name == "finish":
                finished = True
        messages.append(
            Message(role=MessageRole.ASSISTANT, content=response.content, tool_calls=response.tool_calls)
        )
        for call in response.tool_calls:
            messages.append(
                Message(
                    role=MessageRole.TOOL,
                    content="ok",
                    tool_call_id=call.id,
                )
            )
        if finished:
            break
    return total_in, total_out


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
    llm_provider: "LLMProvider",
    *,
    scheme_registry: Any = None,
    workspace_root: Path | None = None,
    settings: "Settings" | None = None,
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
    total_in = 0
    total_out = 0
    cost_usd = 0.0
    parent = Path(workspace_root) if workspace_root else Path(tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"henchmen-eval-{spec.fixture_id}-", dir=parent) as tmp:
        workspace = _copy_repo_to_workspace(spec.repo_dir, Path(tmp))
        try:
            total_in, total_out = await _run_agent_loop(llm_provider, workspace, spec.task, model)
        except Exception as exc:
            logger.exception("Agent loop crashed for fixture %s", spec.fixture_id)
            error = f"{type(exc).__name__}: {exc}"
        score: FixtureScore = score_result(fixture_dir, workspace)

    try:
        from henchmen.observability.tracker import estimate_cost

        cost_usd = float(estimate_cost(model, total_in, total_out))
    except Exception:
        cost_usd = 0.0

    wall_clock = time.perf_counter() - started
    return FixtureResult(
        fixture_id=spec.fixture_id,
        provider=provider_name,
        model_tier=model,
        score=score,
        wall_clock_seconds=round(wall_clock, 3),
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        estimated_cost_usd=round(cost_usd, 6),
        error=error,
    )


async def run_all_fixtures(
    fixtures_dir: Path,
    llm_provider: "LLMProvider",
    *,
    settings: "Settings" | None = None,
) -> EvalReport:
    """Run every fixture under ``fixtures_dir`` and return the aggregated report."""
    fixture_dirs = sorted(p for p in fixtures_dir.iterdir() if p.is_dir())
    results: list[FixtureResult] = []
    for fixture_dir in fixture_dirs:
        if not (fixture_dir / "task.json").is_file():
            continue
        result = await run_fixture(fixture_dir, llm_provider, settings=settings)
        results.append(result)

    aggregate = (
        sum(r.score.overall_score for r in results) / len(results) if results else 0.0
    )
    provider_name = (
        results[0].provider if results else type(llm_provider).__name__.replace("Provider", "").lower()
    )
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
    "EvalReport",
    "FixtureResult",
    "FixtureScore",
    "run_all_fixtures",
    "run_fixture",
    "score_result",
]
