"""Henchmen CLI — single-process server for local development."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    from pathlib import Path

    from evals.harness import EvalReport, FixtureResult


def main() -> None:
    """Entry point for the henchmen CLI."""
    parser = argparse.ArgumentParser(description="Henchmen AI Agent Factory")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Run all services in a single process (local dev)")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    serve_parser.add_argument("--log-level", default="info", help="Log level")

    build_parser = subparsers.add_parser("build-operative", help="Build the local operative Docker image")
    build_parser.add_argument("--no-cache", action="store_true", help="Build without Docker cache")

    subparsers.add_parser(
        "doctor",
        help="Diagnose the local environment (Docker, git, LLM credentials, operative image, ...)",
    )

    eval_parser = subparsers.add_parser("eval", help="Run the offline evaluation harness")
    eval_parser.add_argument(
        "--provider",
        required=True,
        help="LLM provider to evaluate (openai, anthropic, vertex, ollama, local)",
    )
    eval_parser.add_argument(
        "--fixture",
        default=None,
        help="Run a single fixture by directory name (default: run all fixtures)",
    )
    eval_parser.add_argument(
        "--fixtures-dir",
        default="evals/fixtures",
        help="Path to the fixtures directory (default: evals/fixtures)",
    )
    eval_parser.add_argument(
        "--baseline-path",
        default="evals/baseline.json",
        help="Path to baseline.json (default: evals/baseline.json)",
    )
    eval_parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Overwrite the baseline for this provider with the current run",
    )
    eval_parser.add_argument(
        "--compare-baseline",
        action="store_true",
        help="Compare current run to baseline; exit non-zero on >5%% regression",
    )

    args = parser.parse_args()

    if args.command == "serve":
        _serve(args)
    elif args.command == "build-operative":
        _build_operative(args)
    elif args.command == "eval":
        _eval(args)
    elif args.command == "doctor":
        from henchmen.cli import doctor

        sys.exit(doctor.run_doctor_cli())
    else:
        parser.print_help()
        sys.exit(1)


def _check_operative_image() -> bool:
    """Check if the local operative Docker image exists."""
    import subprocess

    result = subprocess.run(
        ["docker", "image", "inspect", "henchmen-operative:local"],
        capture_output=True,
    )
    return result.returncode == 0


def _build_operative(args: argparse.Namespace) -> None:
    """Build the local operative Docker image."""
    import subprocess

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("henchmen")
    logger.info("Building henchmen-operative:local image...")
    cmd = [
        "docker",
        "build",
        "-f",
        "containers/operative/Dockerfile",
        "-t",
        "henchmen-operative:local",
        ".",
    ]
    if getattr(args, "no_cache", False):
        cmd.insert(2, "--no-cache")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("ERROR: Failed to build operative image", file=sys.stderr)
        sys.exit(1)
    print("Successfully built henchmen-operative:local")


_BASELINE_REGRESSION_THRESHOLD = 0.05


def _eval(args: argparse.Namespace) -> None:
    """Run the offline evaluation harness for a given LLM provider."""
    import asyncio
    from pathlib import Path

    from henchmen.config.settings import get_settings

    logging.basicConfig(level=logging.INFO)

    fixtures_dir = Path(args.fixtures_dir).resolve()
    baseline_path = Path(args.baseline_path).resolve()
    if not fixtures_dir.is_dir():
        print(f"ERROR: fixtures dir not found: {fixtures_dir}", file=sys.stderr)
        sys.exit(2)

    # Force the provider override before building settings so get_settings() picks it up.
    os.environ["HENCHMEN_LLM_PROVIDER"] = args.provider
    settings = get_settings()

    from henchmen.providers.registry import ProviderRegistry

    registry = ProviderRegistry(settings)
    try:
        llm_provider = registry.get_llm_provider()
    except Exception as exc:
        print(f"ERROR: failed to resolve LLM provider {args.provider!r}: {exc}", file=sys.stderr)
        sys.exit(2)

    from evals.harness import run_all_fixtures, run_fixture

    if args.fixture:
        target = fixtures_dir / args.fixture
        if not target.is_dir():
            print(f"ERROR: fixture not found: {target}", file=sys.stderr)
            sys.exit(2)
        result = asyncio.run(run_fixture(target, llm_provider, settings=settings))
        _print_fixture_result(result)
        if result.error:
            sys.exit(1)
        return

    report = asyncio.run(run_all_fixtures(fixtures_dir, llm_provider, settings=settings))
    _print_eval_report(report)

    if args.write_baseline:
        _write_baseline(baseline_path, args.provider, report)
        print(f"Baseline updated: {baseline_path}")
        return

    if args.compare_baseline:
        exit_code = _compare_baseline(baseline_path, args.provider, report)
        sys.exit(exit_code)

    # Default: fail if any fixture errored.
    if any(r.error for r in report.results):
        sys.exit(1)


def _print_fixture_result(result: FixtureResult) -> None:
    s = result.score
    tests = "n/a" if s.tests_pass is None else ("pass" if s.tests_pass else "fail")
    print(f"Fixture:   {result.fixture_id}")
    print(f"Provider:  {result.provider}  model={result.model_tier}")
    print(
        f"Score:     {s.overall_score:.2f}  (diff_nonempty={s.diff_non_empty}, "
        f"files={s.touched_expected_files}, tests={tests}, substrings={s.contains_expected_substrings})"
    )
    print(
        f"Wall:      {result.wall_clock_seconds:.2f}s  "
        f"tokens={result.total_input_tokens}/{result.total_output_tokens}  "
        f"cost=${result.estimated_cost_usd:.4f}"
    )
    if result.error:
        print(f"ERROR:     {result.error}")


def _print_eval_report(report: EvalReport) -> None:
    print("=" * 60)
    print(f"Eval report: provider={report.provider}  aggregate={report.aggregate_score:.3f}")
    print(f"Commit: {report.commit_sha or 'unknown'}  at {report.timestamp.isoformat()}")
    print("-" * 60)
    for r in report.results:
        s = r.score
        tests = "-" if s.tests_pass is None else ("P" if s.tests_pass else "F")
        flag = " ERR" if r.error else ""
        print(
            f"  {r.fixture_id:32s}  score={s.overall_score:.2f}  tests={tests}  "
            f"{r.wall_clock_seconds:5.2f}s  ${r.estimated_cost_usd:.4f}{flag}"
        )
    print("=" * 60)


def _write_baseline(path: Path, provider: str, report: EvalReport) -> None:
    import json
    from datetime import date
    from typing import Any

    data: dict[str, Any] = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("version", 1)
    data["last_updated"] = date.today().isoformat()
    providers: dict[str, Any] = data.setdefault("providers", {})
    providers[provider] = {
        "aggregate_score": report.aggregate_score,
        "commit_sha": report.commit_sha,
        "timestamp": report.timestamp.isoformat(),
        "per_fixture": {r.fixture_id: r.score.overall_score for r in report.results},
        "note": "auto-written by `henchmen eval --write-baseline`",
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _compare_baseline(path: Path, provider: str, report: EvalReport) -> int:
    import json

    if not path.is_file():
        print(f"ERROR: baseline not found: {path}", file=sys.stderr)
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = (data.get("providers") or {}).get(provider) or {}
    baseline_score = entry.get("aggregate_score")
    if baseline_score is None:
        print(f"No baseline for provider {provider!r} — run --write-baseline first.")
        return 0
    delta = report.aggregate_score - float(baseline_score)
    print(
        f"Baseline comparison: current={report.aggregate_score:.3f}  "
        f"baseline={float(baseline_score):.3f}  delta={delta:+.3f}"
    )
    if delta < -_BASELINE_REGRESSION_THRESHOLD:
        print(
            f"REGRESSION: aggregate dropped by {abs(delta):.3f} (> {_BASELINE_REGRESSION_THRESHOLD:.2f}) — failing.",
            file=sys.stderr,
        )
        return 1
    return 0


def _serve(args: argparse.Namespace) -> None:
    """Run Dispatch + Mastermind + Forge in a single process."""
    # pydantic-settings loads .env.local then .env automatically via the
    # Settings.model_config env_file tuple — no manual parsing needed here.

    os.environ.setdefault("HENCHMEN_PROVIDER", "local")
    os.environ.setdefault("HENCHMEN_ENVIRONMENT", "dev")
    os.environ.setdefault("HENCHMEN_GCP_PROJECT_ID", "local-dev")

    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    logger = logging.getLogger("henchmen")
    logger.info("Starting Henchmen in single-process mode (provider=local)")

    from fastapi import FastAPI

    app = FastAPI(title="Henchmen (Local Dev)", version="0.1.0")

    from henchmen.dispatch.server import app as dispatch_app
    from henchmen.forge.server import app as forge_app
    from henchmen.mastermind.server import app as mastermind_app

    app.mount("/dispatch", dispatch_app)
    app.mount("/mastermind", mastermind_app)
    app.mount("/forge", forge_app)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "mode": "local", "services": ["dispatch", "mastermind", "forge"]}

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
