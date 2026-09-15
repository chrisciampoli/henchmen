"""Tests for the henchmen CLI: argument parsing, eval dispatch, baselines, serve defaults."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.cli import (
    _build_operative,
    _compare_baseline,
    _default_env,
    _write_baseline,
    main,
)
from henchmen.evals.harness import DimensionScores, EvalReport, FixtureResult, FixtureScore

# ---------------------------------------------------------------------------
# Top-level parsing
# ---------------------------------------------------------------------------


def test_cli_no_args_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("sys.argv", ["henchmen"]), pytest.raises(SystemExit):
        main()
    captured = capsys.readouterr()
    assert "usage" in captured.out.lower() or "henchmen" in captured.out.lower()


def test_cli_serve_help_mentions_host(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("sys.argv", ["henchmen", "serve", "--help"]), pytest.raises(SystemExit):
        main()
    assert "host" in capsys.readouterr().out.lower()


def test_cli_help_mentions_chat_and_init(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("sys.argv", ["henchmen", "--help"]), pytest.raises(SystemExit):
        main()
    out = capsys.readouterr().out
    assert "chat" in out
    assert "init" in out


class TestServeParsing:
    def test_host_defaults_to_loopback(self) -> None:
        """Binding 0.0.0.0 by default exposed unauthenticated task creation to the LAN."""
        with patch("sys.argv", ["henchmen", "serve"]), patch("henchmen.cli._serve") as serve:
            main()
        assert serve.call_args[0][0].host == "127.0.0.1"

    def test_port_defaults_to_none_so_settings_decide(self) -> None:
        with patch("sys.argv", ["henchmen", "serve"]), patch("henchmen.cli._serve") as serve:
            main()
        assert serve.call_args[0][0].port is None

    def test_invalid_log_level_is_an_argparse_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("sys.argv", ["henchmen", "serve", "--log-level", "trace"]), pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        assert "invalid choice" in capsys.readouterr().err


class TestServeApp:
    def test_app_reports_the_package_version(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The single-process app must not hard-code a version that drifts from the package."""
        import argparse
        import os

        import henchmen
        from henchmen.cli import _serve

        for key in [k for k in os.environ if k.startswith("HENCHMEN_")]:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        monkeypatch.setattr("henchmen.providers.registry.ProviderRegistry", MagicMock())
        monkeypatch.setattr("henchmen.providers.local.memory.set_shared_broker", MagicMock())
        from henchmen.config.settings import get_settings
        from henchmen.dispatch.server import app as dispatch_app
        from henchmen.forge.server import app as forge_app
        from henchmen.mastermind.server import app as mastermind_app

        # _serve injects providers into the module-level sub-apps; restore them
        # so no other test sees this test's mocks.
        sub_apps = (dispatch_app, forge_app, mastermind_app)
        saved = [dict(sub.state._state) for sub in sub_apps]
        get_settings.cache_clear()
        run = MagicMock(return_value=0)
        monkeypatch.setattr("henchmen.cli.serve.serve_app", run)
        try:
            with pytest.raises(SystemExit) as exit_info:
                _serve(argparse.Namespace(host="127.0.0.1", port=None, log_level="info"))
        finally:
            get_settings.cache_clear()
            for sub, state in zip(sub_apps, saved, strict=True):
                sub.state._state.clear()
                sub.state._state.update(state)

        assert exit_info.value.code == 0
        app = run.call_args.args[0]
        assert app.version == henchmen.__version__
        assert run.call_args.kwargs["host"] == "127.0.0.1"
        assert run.call_args.kwargs["port"] == 8000
        assert run.call_args.kwargs["log_level"] == "info"


class TestDefaultEnv:
    def test_does_not_override_process_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HENCHMEN_PROVIDER", "gcp")
        _default_env("HENCHMEN_PROVIDER", "local", file_keys=set())
        assert __import__("os").environ["HENCHMEN_PROVIDER"] == "gcp"

    def test_does_not_override_dotenv_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HENCHMEN_PROVIDER", raising=False)
        _default_env("HENCHMEN_PROVIDER", "local", file_keys={"HENCHMEN_PROVIDER"})
        assert "HENCHMEN_PROVIDER" not in __import__("os").environ

    def test_seeds_when_absent_everywhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HENCHMEN_PROVIDER", raising=False)
        _default_env("HENCHMEN_PROVIDER", "local", file_keys=set())
        assert __import__("os").environ["HENCHMEN_PROVIDER"] == "local"

    def test_seeded_defaults_are_recorded_for_apply_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import henchmen.cli as cli

        monkeypatch.setattr(cli, "_SEEDED_ENV_DEFAULTS", {})
        monkeypatch.delenv("HENCHMEN_PROVIDER", raising=False)
        _default_env("HENCHMEN_PROVIDER", "local", file_keys=set())
        _default_env("HENCHMEN_LLM_PROVIDER", "local", file_keys={"HENCHMEN_LLM_PROVIDER"})
        assert cli._seeded_env_defaults() == {"HENCHMEN_PROVIDER": "local"}


# ---------------------------------------------------------------------------
# henchmen eval — argument parsing
# ---------------------------------------------------------------------------


class TestEvalParsing:
    def test_documented_bare_form_runs(self) -> None:
        """`henchmen eval --provider openai` (no `run`) is what every doc and the workflow use."""
        argv = ["henchmen", "eval", "--provider", "openai", "--all", "--write-baseline"]
        with patch("sys.argv", argv), patch("henchmen.cli._eval_run") as run:
            main()
        args = run.call_args[0][0]
        assert args.provider == "openai"
        assert args.all is True
        assert args.write_baseline is True

    def test_explicit_run_subcommand(self) -> None:
        argv = ["henchmen", "eval", "run", "--provider", "openai", "--fixture", "bugfix_off_by_one"]
        with patch("sys.argv", argv), patch("henchmen.cli._eval_run") as run:
            main()
        args = run.call_args[0][0]
        assert args.provider == "openai"
        assert args.fixture == "bugfix_off_by_one"

    def test_run_without_provider_is_an_argparse_error(self) -> None:
        with patch("sys.argv", ["henchmen", "eval", "run"]), pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2

    def test_bare_eval_without_provider_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("sys.argv", ["henchmen", "eval"]), pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "usage" in capsys.readouterr().out.lower()

    def test_history_still_routes_to_history(self) -> None:
        argv = ["henchmen", "eval", "history", "--provider", "local"]
        with patch("sys.argv", argv), patch("henchmen.cli._eval_history") as history:
            main()
        history.assert_called_once()


# ---------------------------------------------------------------------------
# henchmen eval run — guards
# ---------------------------------------------------------------------------


def _report(*, aggregate: float = 0.9, error: str | None = None, runner_error: str | None = None) -> EvalReport:
    score = FixtureScore(
        fixture_id="bugfix_off_by_one",
        diff_non_empty=True,
        touched_expected_files=True,
        tests_pass=True,
        contains_expected_substrings=True,
        overall_score=aggregate,
        dimensions=DimensionScores(correctness=1.0, precision=1.0, conventions=1.0, efficiency=1.0, completion=1.0),
        test_runner_error=runner_error,
    )
    result = FixtureResult(
        fixture_id="bugfix_off_by_one",
        provider="openai",
        model_tier="gpt-4.1",
        score=score,
        wall_clock_seconds=1.0,
        error=error,
    )
    return EvalReport(
        provider="openai",
        commit_sha="deadbeef",
        timestamp=datetime(2026, 4, 12, 10, 0, tzinfo=UTC),
        results=[result],
        aggregate_score=aggregate,
    )


@pytest.fixture
def eval_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A cwd with a fixtures dir, no HENCHMEN_ env leakage, and stubbed providers/storage."""
    import os

    for key in [k for k in os.environ if k.startswith("HENCHMEN_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    fixtures = tmp_path / "evals" / "fixtures" / "bugfix_off_by_one"
    fixtures.mkdir(parents=True)
    (fixtures / "task.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("henchmen.cli._save_report_to_storage", lambda *a, **kw: None)
    monkeypatch.setattr("henchmen.cli._save_single_fixture_run", lambda *a, **kw: None)
    monkeypatch.setattr("henchmen.evals.storage.require_aiosqlite", MagicMock())
    registry = MagicMock()
    registry.return_value.get_llm_provider.return_value = MagicMock()
    monkeypatch.setattr("henchmen.providers.registry.ProviderRegistry", registry)
    yield tmp_path


def _run_eval(argv: list[str]) -> int:
    with patch("sys.argv", argv), pytest.raises(SystemExit) as exc:
        main()
    return int(exc.value.code or 0)


@contextmanager
def _patched_harness(report: EvalReport):
    """Replace the harness runners so no LLM call or fixture execution happens."""
    with (
        patch("henchmen.evals.harness.run_all_fixtures", AsyncMock(return_value=report)),
        patch("henchmen.evals.harness.run_fixture", AsyncMock(return_value=report.results[0])),
    ):
        yield


class TestEvalRunGuards:
    def test_missing_aiosqlite_exits_before_any_fixture_runs(
        self, eval_env: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """A bare install must fail before paying for LLM calls, not after."""
        from henchmen.evals.storage import AiosqliteMissingError

        monkeypatch.setattr("henchmen.evals.storage.require_aiosqlite", MagicMock(side_effect=AiosqliteMissingError()))
        report = _report()
        with _patched_harness(report):
            import henchmen.evals.harness as harness

            code = _run_eval(["henchmen", "eval", "run", "--provider", "openai"])
            run_all = harness.run_all_fixtures
        assert code == 2
        assert '.[evals]"' in capsys.readouterr().err
        assert isinstance(run_all, AsyncMock)
        run_all.assert_not_awaited()

    def test_fixture_with_write_baseline_is_rejected(self, eval_env: Path, capsys) -> None:
        code = _run_eval(
            ["henchmen", "eval", "run", "--provider", "openai", "--fixture", "bugfix_off_by_one", "--write-baseline"]
        )
        assert code == 2
        assert "drop --fixture" in capsys.readouterr().err

    def test_fixture_and_all_are_mutually_exclusive(self, eval_env: Path, capsys) -> None:
        code = _run_eval(["henchmen", "eval", "run", "--provider", "openai", "--fixture", "bugfix_off_by_one", "--all"])
        assert code == 2
        assert "mutually exclusive" in capsys.readouterr().err

    def test_missing_fixtures_dir_exits_two(self, eval_env: Path, capsys) -> None:
        code = _run_eval(["henchmen", "eval", "run", "--provider", "openai", "--fixtures-dir", "nope/fixtures"])
        assert code == 2
        assert "fixtures dir not found" in capsys.readouterr().err

    def test_no_gcp_credentials_needed_for_openai(self, eval_env: Path) -> None:
        """Settings defaults to provider=gcp; an openai eval must not demand a GCP project."""
        argv = ["henchmen", "eval", "run", "--provider", "openai", "--write-baseline"]
        with _patched_harness(_report()), patch("sys.argv", argv):
            main()  # completes instead of raising pydantic ValidationError / SystemExit(2)
        assert (eval_env / "evals" / "baseline.json").is_file()

    def test_errored_fixture_blocks_baseline_write(self, eval_env: Path, capsys) -> None:
        baseline = eval_env / "evals" / "baseline.json"
        with _patched_harness(_report(error="RuntimeError: 401")):
            code = _run_eval(["henchmen", "eval", "run", "--provider", "openai", "--write-baseline"])
        assert code == 1
        assert "refusing to write" in capsys.readouterr().err
        assert not baseline.exists()

    def test_unavailable_test_runner_blocks_baseline_write(self, eval_env: Path) -> None:
        baseline = eval_env / "evals" / "baseline.json"
        with _patched_harness(_report(runner_error="test runner 'pytest' not found on PATH")):
            code = _run_eval(["henchmen", "eval", "run", "--provider", "openai", "--write-baseline"])
        assert code == 1
        assert not baseline.exists()

    def test_provider_alias_is_normalised(self, eval_env: Path) -> None:
        """`--provider ollama` must resolve to the registry name the baseline keys off."""
        import os

        with _patched_harness(_report()), patch("sys.argv", ["henchmen", "eval", "run", "--provider", "ollama"]):
            main()
        assert os.environ["HENCHMEN_LLM_PROVIDER"] == "local"


# ---------------------------------------------------------------------------
# Baseline read/write
# ---------------------------------------------------------------------------


class TestBaseline:
    def test_write_preserves_hand_written_stub_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "providers": {
                        "openai": {
                            "aggregate_score": None,
                            "runs": 3,
                            "how_to_populate": "keep me",
                            "notes": "keep me too",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        fixtures = tmp_path / "fixtures" / "bugfix_off_by_one"
        fixtures.mkdir(parents=True)
        (fixtures / "task.json").write_text("{}", encoding="utf-8")

        _write_baseline(path, "openai", _report(aggregate=0.8), tmp_path / "fixtures")

        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data["providers"]["openai"]
        assert entry["how_to_populate"] == "keep me"
        assert entry["notes"] == "keep me too"
        assert entry["aggregate_score"] == 0.8
        assert entry["runs"] == 4
        assert entry["fixtures"] == {"bugfix_off_by_one": 0.8}
        assert entry["model_tier"] == "gpt-4.1"
        assert data["version"] == 2
        assert data["last_updated"] == "2026-04-12"  # UTC date, not naive local
        assert data["fixtures"] == ["bugfix_off_by_one"]

    def test_write_creates_file_when_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        _write_baseline(path, "local", _report(aggregate=0.5), tmp_path / "missing")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["providers"]["local"]["aggregate_score"] == 0.5
        assert data["providers"]["local"]["runs"] == 1

    def test_compare_reads_what_write_wrote(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        _write_baseline(path, "openai", _report(aggregate=0.9), tmp_path / "missing")
        assert _compare_baseline(path, "openai", _report(aggregate=0.9)) == 0

    def test_compare_flags_regression(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        _write_baseline(path, "openai", _report(aggregate=0.9), tmp_path / "missing")
        assert _compare_baseline(path, "openai", _report(aggregate=0.5)) == 1

    def test_compare_without_baseline_file_exits_two(self, tmp_path: Path) -> None:
        assert _compare_baseline(tmp_path / "nope.json", "openai", _report()) == 2

    def test_compare_without_provider_entry_passes(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        assert _compare_baseline(path, "openai", _report()) == 0


# ---------------------------------------------------------------------------
# build-operative
# ---------------------------------------------------------------------------


class TestBuildOperative:
    def test_missing_dockerfile_exits_two(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.chdir(tmp_path)
        args = MagicMock(no_cache=False)
        with pytest.raises(SystemExit) as exc:
            _build_operative(args)
        assert exc.value.code == 2
        assert "repo root" in capsys.readouterr().err

    def test_missing_docker_cli_exits_two(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "containers" / "operative").mkdir(parents=True)
        (tmp_path / "containers" / "operative" / "Dockerfile").write_text("FROM python\n", encoding="utf-8")
        args = MagicMock(no_cache=False)
        with patch("subprocess.run", side_effect=FileNotFoundError("docker")), pytest.raises(SystemExit) as exc:
            _build_operative(args)
        assert exc.value.code == 2
        assert "docker CLI not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Eval history / storage plumbing
# ---------------------------------------------------------------------------


class TestEvalStoragePlumbing:
    def test_missing_aiosqlite_exits_with_install_hint(self, capsys) -> None:
        from henchmen.evals.storage import AiosqliteMissingError

        with (
            patch("henchmen.evals.storage.require_aiosqlite", side_effect=AiosqliteMissingError()),
            patch("sys.argv", ["henchmen", "eval", "history"]),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        assert exc.value.code == 2
        # aiosqlite ships in the [evals] extra (dev tooling carries no runtime deps).
        assert 'pip install -e ".[evals]"' in capsys.readouterr().err

    def test_history_normalises_provider_alias(self) -> None:
        list_runs = AsyncMock(return_value=[])
        with (
            patch("henchmen.evals.storage.require_aiosqlite", MagicMock()),
            patch("henchmen.evals.storage.list_runs", list_runs),
            patch("sys.argv", ["henchmen", "eval", "history", "--provider", "vertex"]),
        ):
            main()
        assert list_runs.await_args.kwargs["provider"] == "gcp"
