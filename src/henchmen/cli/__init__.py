"""Henchmen CLI — setup wizard, diagnostics, evals and a single-process dev server."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from henchmen.config.settings import Settings
    from henchmen.evals.harness import EvalReport, FixtureResult

_LOG_LEVELS = ("critical", "error", "warning", "info", "debug")
_BASELINE_REGRESSION_THRESHOLD = 0.05
_BASELINE_SCHEMA_VERSION = 2


def _add_eval_run_arguments(parser: argparse.ArgumentParser, *, provider_required: bool) -> None:
    """Register the ``eval run`` flags.

    Applied to both the ``eval`` parser and its ``run`` sub-parser so the
    documented short form (``henchmen eval --provider openai``) and the
    explicit form (``henchmen eval run --provider openai``) both parse.
    """
    parser.add_argument(
        "--provider",
        required=provider_required,
        default=None,
        help="LLM provider to evaluate: gcp, aws, local, openai, anthropic (aliases: vertex, bedrock, ollama)",
    )
    parser.add_argument(
        "--fixture",
        default=None,
        help="Run a single fixture by directory name (default: run all fixtures)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run every fixture (the default; accepted for explicitness)",
    )
    parser.add_argument(
        "--fixtures-dir",
        default="evals/fixtures",
        help="Path to the fixtures directory (default: evals/fixtures)",
    )
    parser.add_argument(
        "--baseline-path",
        default="evals/baseline.json",
        help="Path to baseline.json (default: evals/baseline.json)",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Overwrite the baseline for this provider with the current run",
    )
    parser.add_argument(
        "--compare-baseline",
        action="store_true",
        help="Compare current run to baseline; exit non-zero on >5%% regression",
    )


def main() -> None:
    """Entry point for the henchmen CLI."""
    parser = argparse.ArgumentParser(description="Henchmen AI Agent Factory")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Run all services in a single process (local dev)")
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1; pass 0.0.0.0 to expose on the network)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind to (default: HENCHMEN_LOCAL_SERVE_PORT, itself 8000)",
    )
    serve_parser.add_argument("--log-level", default="info", choices=_LOG_LEVELS, help="Log level")

    build_parser = subparsers.add_parser("build-operative", help="Build the local operative Docker image")
    build_parser.add_argument("--no-cache", action="store_true", help="Build without Docker cache")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Diagnose the local environment (settings, Docker, git, LLM/GitHub/Slack/Jira credentials, ...)",
    )
    from henchmen.cli.doctor import add_doctor_arguments

    add_doctor_arguments(doctor_parser)

    subparsers.add_parser("chat", help="Interactive task builder REPL (uses the configured LLM provider)")

    embed_parser = subparsers.add_parser("embed", help="Index a repository into the RAG Engine corpus (owner/name)")
    from henchmen.cli.embed import add_embed_arguments

    add_embed_arguments(embed_parser)

    config_parser = subparsers.add_parser(
        "config", help="Print the effective configuration (env + .env.local + defaults), secrets masked"
    )
    from henchmen.cli.config_cmd import add_config_arguments

    add_config_arguments(config_parser)

    init_parser = subparsers.add_parser(
        "init",
        aliases=["setup"],
        help="Interactive setup: choose providers and models, connect GitHub/Slack/Jira, write .env.local",
    )
    from henchmen.cli.init import add_init_arguments

    add_init_arguments(init_parser)

    eval_parser = subparsers.add_parser("eval", help="Run the offline evaluation harness")
    _add_eval_run_arguments(eval_parser, provider_required=False)
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command")

    # --- henchmen eval run ---
    eval_run_parser = eval_subparsers.add_parser("run", help="Run eval fixtures and save results to SQLite")
    _add_eval_run_arguments(eval_run_parser, provider_required=True)

    # --- henchmen eval compare ---
    eval_compare_parser = eval_subparsers.add_parser("compare", help="Compare two eval runs dimension-by-dimension")
    eval_compare_parser.add_argument("run_a", help="First run ID")
    eval_compare_parser.add_argument("run_b", help="Second run ID")

    # --- henchmen eval history ---
    eval_history_parser = eval_subparsers.add_parser("history", help="Show past eval runs")
    eval_history_parser.add_argument("--provider", default=None, help="Filter by provider")
    eval_history_parser.add_argument("--limit", type=int, default=20, help="Max runs to show")

    args = parser.parse_args()

    if args.command == "serve":
        _serve(args)
    elif args.command == "build-operative":
        _build_operative(args)
    elif args.command == "eval":
        _dispatch_eval(args, eval_parser)
    elif args.command == "doctor":
        from henchmen.cli import doctor

        sys.exit(doctor.run_doctor_cli(args))
    elif args.command == "chat":
        from henchmen.cli import chat

        sys.exit(chat.run_chat_cli())
    elif args.command == "embed":
        from henchmen.cli.embed import run_embed_cli

        sys.exit(run_embed_cli(args))
    elif args.command == "config":
        from henchmen.cli.config_cmd import run_config_cli

        sys.exit(run_config_cli(args))
    elif args.command in ("init", "setup"):
        from henchmen.cli.init import run_init_cli

        sys.exit(run_init_cli(args))
    else:
        parser.print_help()
        sys.exit(1)


# ---------------------------------------------------------------------------
# Environment defaults
# ---------------------------------------------------------------------------


def _dotenv_keys() -> set[str]:
    """Keys defined in the dotenv files ``Settings`` itself reads.

    ``os.environ`` outranks the dotenv files in pydantic-settings, so a
    ``setdefault`` would silently override a value the user put in
    ``.env.local``. Commands seed defaults only for keys absent from both.
    """
    from dotenv import dotenv_values

    from henchmen.config.paths import env_files

    keys: set[str] = set()
    for env_file in env_files():
        try:
            keys.update(str(key).upper() for key in dotenv_values(env_file))
        except OSError:  # pragma: no cover - unreadable dotenv
            continue
    return keys


def _default_env(key: str, value: str, *, file_keys: set[str]) -> None:
    """Seed ``key`` only when neither the process env nor a dotenv file defines it."""
    if key not in os.environ and key not in file_keys:
        os.environ[key] = value


def _build_settings_or_exit() -> Settings:
    """Build ``Settings``, turning a validation error into an actionable exit."""
    from henchmen.config import paths
    from henchmen.config.settings import get_settings

    try:
        return get_settings()
    except ValueError as exc:  # pydantic ValidationError subclasses ValueError
        first = (str(exc).strip().splitlines() or ["invalid settings"])[0]
        print(f"ERROR: invalid configuration: {first}", file=sys.stderr)
        # Data-dir installs keep configuration in <data dir>/henchmen.env, not .env.local.
        print(f"Hint: run `henchmen init` to (re)write {paths.config_file()}.", file=sys.stderr)
        sys.exit(2)


def _build_operative(args: argparse.Namespace) -> None:
    """Build the local operative Docker image."""
    import subprocess
    from pathlib import Path

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("henchmen")

    dockerfile = Path("containers/operative/Dockerfile")
    if not dockerfile.is_file():
        print(
            f"ERROR: {dockerfile} not found — run `henchmen build-operative` from the Henchmen repo root.",
            file=sys.stderr,
        )
        sys.exit(2)

    logger.info("Building henchmen-operative:local image...")
    cmd = [
        "docker",
        "build",
        "-f",
        str(dockerfile),
        "-t",
        "henchmen-operative:local",
        ".",
    ]
    if getattr(args, "no_cache", False):
        cmd.insert(2, "--no-cache")
    try:
        result = subprocess.run(cmd)
    except FileNotFoundError:
        print("ERROR: docker CLI not found on PATH — install Docker Desktop first.", file=sys.stderr)
        sys.exit(2)
    if result.returncode != 0:
        print("ERROR: Failed to build operative image", file=sys.stderr)
        sys.exit(1)
    print("Successfully built henchmen-operative:local")


# ---------------------------------------------------------------------------
# henchmen eval
# ---------------------------------------------------------------------------


def _dispatch_eval(args: argparse.Namespace, eval_parser: argparse.ArgumentParser) -> None:
    """Route eval sub-subcommands to their handlers.

    A bare ``henchmen eval --provider X`` (no sub-subcommand) is treated as
    ``run`` — the form every doc and the evals workflow use.
    """
    cmd = getattr(args, "eval_command", None)
    if cmd == "run":
        _eval_run(args)
    elif cmd == "compare":
        _eval_compare(args)
    elif cmd == "history":
        _eval_history(args)
    elif getattr(args, "provider", None):
        _eval_run(args)
    else:
        eval_parser.print_help()
        sys.exit(1)


def _eval_run(args: argparse.Namespace) -> None:
    """Run the offline evaluation harness for a given LLM provider."""
    import asyncio
    from pathlib import Path
    from uuid import uuid4

    from henchmen.providers.tiers import normalize_llm_provider

    logging.basicConfig(level=logging.INFO)
    # Check the history store before any fixture runs: discovering a missing
    # aiosqlite only after every fixture's LLM calls were paid for wastes them.
    _require_storage_or_exit()

    provider = normalize_llm_provider(args.provider or "")
    if not provider:
        print("ERROR: --provider is required.", file=sys.stderr)
        sys.exit(2)
    if args.fixture and getattr(args, "all", False):
        print("ERROR: --fixture and --all are mutually exclusive.", file=sys.stderr)
        sys.exit(2)
    if args.fixture and (args.write_baseline or args.compare_baseline):
        print(
            "ERROR: --write-baseline / --compare-baseline describe a full run; drop --fixture.",
            file=sys.stderr,
        )
        sys.exit(2)

    fixtures_dir = Path(args.fixtures_dir).resolve()
    baseline_path = Path(args.baseline_path).resolve()
    if not fixtures_dir.is_dir():
        print(f"ERROR: fixtures dir not found: {fixtures_dir}", file=sys.stderr)
        print("Hint: run `henchmen eval` from the Henchmen repo root, or pass --fixtures-dir.", file=sys.stderr)
        sys.exit(2)

    # The harness needs an LLM only: default the base provider to local so
    # Settings does not demand GCP credentials, without overriding .env.local.
    _default_env("HENCHMEN_PROVIDER", "local", file_keys=_dotenv_keys())
    os.environ["HENCHMEN_LLM_PROVIDER"] = provider
    settings = _build_settings_or_exit()

    from henchmen.providers.registry import ProviderRegistry

    registry = ProviderRegistry(settings)
    try:
        llm_provider = registry.get_llm_provider()
    except Exception as exc:
        print(f"ERROR: failed to resolve LLM provider {args.provider!r}: {exc}", file=sys.stderr)
        sys.exit(2)

    from henchmen.evals.harness import run_all_fixtures, run_fixture

    if args.fixture:
        target = fixtures_dir / args.fixture
        if not target.is_dir():
            print(f"ERROR: fixture not found: {target}", file=sys.stderr)
            sys.exit(2)
        result = asyncio.run(run_fixture(target, llm_provider, settings=settings, provider_name=provider))
        _print_fixture_result(result)
        _save_single_fixture_run(result, str(uuid4()))
        if result.error:
            sys.exit(1)
        return

    report = asyncio.run(run_all_fixtures(fixtures_dir, llm_provider, settings=settings, provider_name=provider))
    _print_eval_report(report)

    # Save full run to SQLite history (before the guard, so failed runs show up
    # in `henchmen eval history`).
    run_id = str(uuid4())
    _save_report_to_storage(report, run_id)

    errored = [r.fixture_id for r in report.results if r.error or r.score.test_runner_error]
    if not report.results or errored:
        detail = "no fixtures ran" if not report.results else f"{len(errored)} fixture(s) errored: {', '.join(errored)}"
        print(f"ERROR: {detail}; refusing to write or compare the baseline.", file=sys.stderr)
        sys.exit(1)

    if args.write_baseline:
        _write_baseline(baseline_path, provider, report, fixtures_dir)
        print(f"Baseline updated: {baseline_path}")
        return

    if args.compare_baseline:
        sys.exit(_compare_baseline(baseline_path, provider, report))


def _require_storage_or_exit() -> None:
    """Exit with an install hint when the optional ``aiosqlite`` dependency is absent."""
    from henchmen.evals.storage import AiosqliteMissingError, require_aiosqlite

    try:
        require_aiosqlite()
    except AiosqliteMissingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)


def _save_report_to_storage(report: EvalReport, run_id: str) -> None:
    """Persist an EvalReport to the SQLite history store (best effort)."""
    import asyncio

    from henchmen.evals.storage import AiosqliteMissingError, EvalRun, FixtureResultRow, save_run

    fixture_rows: list[FixtureResultRow] = []
    for r in report.results:
        dims = r.score.dimensions
        fixture_rows.append(
            FixtureResultRow(
                fixture_id=r.fixture_id,
                correctness=dims.correctness if dims else 0.0,
                precision=dims.precision if dims else 0.0,
                conventions=dims.conventions if dims else 0.0,
                efficiency=dims.efficiency if dims else 0.0,
                completion=dims.completion if dims else 0.0,
                wall_clock=r.wall_clock_seconds,
                tokens=r.total_input_tokens + r.total_output_tokens,
                cost=r.estimated_cost_usd,
            )
        )

    eval_run = EvalRun(
        id=run_id,
        provider=report.provider,
        commit_sha=report.commit_sha,
        timestamp=report.timestamp.isoformat(),
        aggregate_score=report.aggregate_score,
        fixture_results=fixture_rows,
    )
    try:
        asyncio.run(save_run(eval_run))
    except AiosqliteMissingError as exc:
        print(f"WARNING: run not saved to history — {exc}", file=sys.stderr)
        return
    print(f"Run saved: {run_id}")


def _save_single_fixture_run(result: FixtureResult, run_id: str) -> None:
    """Persist a single-fixture result to the SQLite history store (best effort)."""
    import asyncio

    from henchmen.evals.storage import AiosqliteMissingError, EvalRun, FixtureResultRow, save_run

    dims = result.score.dimensions
    row = FixtureResultRow(
        fixture_id=result.fixture_id,
        correctness=dims.correctness if dims else 0.0,
        precision=dims.precision if dims else 0.0,
        conventions=dims.conventions if dims else 0.0,
        efficiency=dims.efficiency if dims else 0.0,
        completion=dims.completion if dims else 0.0,
        wall_clock=result.wall_clock_seconds,
        tokens=result.total_input_tokens + result.total_output_tokens,
        cost=result.estimated_cost_usd,
    )
    eval_run = EvalRun(
        id=run_id,
        provider=result.provider,
        aggregate_score=result.score.overall_score,
        fixture_results=[row],
    )
    try:
        asyncio.run(save_run(eval_run))
    except AiosqliteMissingError as exc:
        print(f"WARNING: run not saved to history — {exc}", file=sys.stderr)


def _eval_compare(args: argparse.Namespace) -> None:
    """Compare two eval runs dimension-by-dimension."""
    import asyncio

    _require_storage_or_exit()
    from henchmen.evals.storage import compare_runs

    logging.basicConfig(level=logging.INFO)

    try:
        comparison = asyncio.run(compare_runs(args.run_a, args.run_b))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    print(f"Comparing runs: {comparison.run_a_id} vs {comparison.run_b_id}")
    print(
        f"Aggregate:  {comparison.run_a_aggregate:.4f} -> {comparison.run_b_aggregate:.4f}  "
        f"delta={comparison.aggregate_delta:+.4f}"
    )
    print("-" * 60)
    for d in comparison.dimension_deltas:
        print(f"  {d.dimension:14s}  {d.run_a_avg:.4f} -> {d.run_b_avg:.4f}  delta={d.delta:+.4f}")


def _eval_history(args: argparse.Namespace) -> None:
    """Show past eval runs from SQLite history."""
    import asyncio

    from henchmen.providers.tiers import normalize_llm_provider

    _require_storage_or_exit()
    from henchmen.evals.storage import list_runs

    logging.basicConfig(level=logging.INFO)

    provider = normalize_llm_provider(args.provider) if args.provider else None
    runs = asyncio.run(list_runs(provider=provider, limit=args.limit))

    if not runs:
        print("No eval runs found.")
        return

    print(f"{'ID':36s}  {'Provider':12s}  {'Score':6s}  {'Fixtures':8s}  {'Timestamp'}")
    print("-" * 90)
    for r in runs:
        print(f"{r.id:36s}  {r.provider:12s}  {r.aggregate_score:.4f}  {r.fixture_count:8d}  {r.timestamp}")


def _print_fixture_result(result: FixtureResult) -> None:
    s = result.score
    tests = "n/a" if s.tests_pass is None else ("pass" if s.tests_pass else "fail")
    print(f"Fixture:   {result.fixture_id}")
    print(f"Provider:  {result.provider}  model={result.model_tier}")
    print(
        f"Score:     {s.overall_score:.2f}  (diff_nonempty={s.diff_non_empty}, "
        f"files={s.touched_expected_files}, tests={tests}, substrings={s.contains_expected_substrings})"
    )
    if s.dimensions is not None:
        print(f"Weighted:  {s.dimensions.compute_weighted_score():.2f}  (five-dimension composite)")
    print(
        f"Wall:      {result.wall_clock_seconds:.2f}s  "
        f"tokens={result.total_input_tokens}/{result.total_output_tokens}  "
        f"cost=${result.estimated_cost_usd:.4f}"
    )
    if s.test_runner_error:
        print(f"TESTS:     {s.test_runner_error}")
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
        weighted = s.dimensions.compute_weighted_score() if s.dimensions else 0.0
        flag = " ERR" if (r.error or s.test_runner_error) else ""
        print(
            f"  {r.fixture_id:32s}  score={s.overall_score:.2f}  weighted={weighted:.2f}  tests={tests}  "
            f"{r.wall_clock_seconds:5.2f}s  ${r.estimated_cost_usd:.4f}{flag}"
        )
    print("=" * 60)


def _write_baseline(path: Path, provider: str, report: EvalReport, fixtures_dir: Path) -> None:
    """Merge this run into ``baseline.json`` under the canonical provider key.

    The provider entry is *updated*, never replaced, so the hand-written
    ``how_to_populate`` / ``notes`` guidance in the stub survives a run.
    """
    import json
    from typing import Any

    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"ERROR: {path} is not valid JSON: {exc}", file=sys.stderr)
            sys.exit(2)

    tiers = sorted({r.model_tier for r in report.results if r.model_tier})
    providers: dict[str, Any] = data.setdefault("providers", {})
    entry: dict[str, Any] = dict(providers.get(provider) or {})
    entry.update(
        {
            "aggregate_score": report.aggregate_score,
            "fixtures": {r.fixture_id: r.score.overall_score for r in report.results},
            "model_tier": ", ".join(tiers) or None,
            "runs": int(entry.get("runs") or 0) + 1,
            "last_run": report.timestamp.isoformat(),
            "commit_sha": report.commit_sha,
        }
    )
    providers[provider] = entry

    data["version"] = _BASELINE_SCHEMA_VERSION
    data["last_updated"] = report.timestamp.date().isoformat()
    if fixtures_dir.is_dir():
        data["fixtures"] = sorted(p.name for p in fixtures_dir.iterdir() if (p / "task.json").is_file())
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _compare_baseline(path: Path, provider: str, report: EvalReport) -> int:
    import json

    if not path.is_file():
        print(f"ERROR: baseline not found: {path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: {path} is not valid JSON: {exc}", file=sys.stderr)
        return 2
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


# ---------------------------------------------------------------------------
# henchmen serve
# ---------------------------------------------------------------------------


def _serve(args: argparse.Namespace) -> None:
    """Run Henchmen in one process: setup mode (Console only) or run mode (all services)."""
    from henchmen.cli.serve import (
        RestartSignal,
        build_serve_app,
        build_setup_app,
        configure_serve_logging,
        console_url,
        serve_app,
    )
    from henchmen.config import paths

    _default_env("HENCHMEN_PROVIDER", "local", file_keys=_dotenv_keys())
    if args.port is not None:
        os.environ["HENCHMEN_LOCAL_SERVE_PORT"] = str(args.port)

    configure_serve_logging(args.log_level)
    logger = logging.getLogger("henchmen")
    restart = RestartSignal()

    console = None
    setup_token: str | None = None
    state_file = paths.setup_state_file()
    secrets_dir = paths.secrets_dir()
    if state_file is not None and secrets_dir is not None:
        from henchmen.console.app import ConsoleMode, create_console_app
        from henchmen.console.auth import ConsoleAuth
        from henchmen.console.state import SetupStateStore

        store = SetupStateStore(state_file)
        try:
            state = store.load()
        except (ValueError, OSError) as exc:
            # ValueError: corrupt JSON; OSError: e.g. a data volume the container user cannot read.
            print(f"ERROR: {exc}", file=sys.stderr)
            print(f"Hint: restore or delete {state_file} to restart setup.", file=sys.stderr)
            sys.exit(2)

        try:
            auth = ConsoleAuth.load(secrets_dir, setup_token=os.environ.get(paths.SETUP_TOKEN_ENV) or None)
        except OSError as exc:
            print(f"ERROR: could not read or write {secrets_dir}: {exc}", file=sys.stderr)
            print("Hint: check permissions on the data volume.", file=sys.stderr)
            sys.exit(2)

        if not state.completed:
            raw_port = args.port or os.environ.get("HENCHMEN_LOCAL_SERVE_PORT") or "8000"
            try:
                console_port = int(raw_port)
            except ValueError:
                print(
                    f"ERROR: HENCHMEN_LOCAL_SERVE_PORT must be an integer, got {raw_port!r}.",
                    file=sys.stderr,
                )
                sys.exit(2)
            logger.info("Setup is not complete; serving only the setup Console")
            print(f"Open Henchmen setup: {console_url(console_port, auth.setup_token)}", flush=True)
            setup_console = create_console_app(
                mode=ConsoleMode.SETUP,
                store=store,
                auth=auth,
                config_file=paths.config_file(),
                on_apply=restart.request,
            )
            code = serve_app(
                build_setup_app(setup_console),
                host=args.host,
                port=console_port,
                log_level=args.log_level,
                restart=restart,
            )
            sys.exit(code)

        console = create_console_app(
            mode=ConsoleMode.RUN,
            store=store,
            auth=auth,
            config_file=paths.config_file(),
            on_apply=restart.request,
        )
        setup_token = auth.setup_token

    from henchmen.providers.tiers import active_llm_provider

    settings = _build_settings_or_exit()
    port = int(settings.local_serve_port)

    llm = active_llm_provider(settings)
    if (settings.provider == "gcp" or llm == "gcp") and not settings.gcp_project_id:
        print(
            "ERROR: HENCHMEN_GCP_PROJECT_ID is required when the provider is gcp (Vertex AI).",
            file=sys.stderr,
        )
        print("Hint: run `henchmen init`, or set HENCHMEN_PROVIDER=local for a fully local run.", file=sys.stderr)
        sys.exit(2)

    if console is not None and setup_token is not None:
        # Printed only now, from the port Settings actually resolved (which may
        # come from <data dir>/henchmen.env), not the pre-Settings bootstrap guess.
        print(f"Open Henchmen: {console_url(port, setup_token)}", flush=True)

    logger.info(
        "Starting Henchmen in single-process mode (provider=%s, llm=%s, environment=%s)",
        settings.provider,
        llm,
        settings.environment.value,
    )
    app = build_serve_app(settings, port, console=console)
    sys.exit(serve_app(app, host=args.host, port=port, log_level=args.log_level, restart=restart))
