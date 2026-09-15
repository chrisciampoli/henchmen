"""Local CI gates clone and diff inside the gate container; nothing is bind-mounted (D-P9)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.config.settings import Settings
from henchmen.mastermind.scheme_executor import ci_gate
from henchmen.mastermind.scheme_executor.ci_gate import (
    GATE_RESULT_MARKER,
    GateResult,
    install_script,
    parse_gate_result,
    run_gate,
)
from henchmen.mastermind.scheme_executor.lint_scope import LintScopeError
from henchmen.utils.stack_detector import Stack

TOKEN = "ghp_" + "s" * 36


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def _gate(check: str, workspace: Path) -> GateResult:
    return await run_gate(  # type: ignore[arg-type]
        check, repo="acme/widgets", branch="henchmen/t", base_branch="main", token=TOKEN, workspace=str(workspace)
    )


class TestRunGate:
    @pytest.mark.asyncio
    async def test_clone_failure_fails_and_hides_the_token(self, tmp_path: Path) -> None:
        with patch.object(ci_gate, "clone_repo", AsyncMock(side_effect=RuntimeError(f"auth failed for {TOKEN}"))):
            result = await _gate("lint", tmp_path)
        assert result.condition == "fail"
        assert "clone failed" in result.message
        assert TOKEN not in result.message

    @pytest.mark.asyncio
    async def test_undetectable_stack_fails(self, tmp_path: Path) -> None:
        with patch.object(ci_gate, "clone_repo", AsyncMock()):
            result = await _gate("tests", tmp_path)
        assert result.condition == "fail"
        assert "could not detect the project stack" in result.message

    @pytest.mark.asyncio
    async def test_uncomputable_diff_fails_closed(self, tmp_path: Path) -> None:
        _write(tmp_path, "pyproject.toml")
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(side_effect=LintScopeError(f"git fetch failed: {TOKEN}"))),
            patch.object(ci_gate, "_run_script", AsyncMock()) as run,
        ):
            result = await _gate("lint", tmp_path)
        assert result.condition == "fail"
        assert "could not determine the files changed against main" in result.message
        assert TOKEN not in result.message
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_relevant_changes_pass_without_running_anything(self, tmp_path: Path) -> None:
        _write(tmp_path, "pyproject.toml")
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(return_value=["README.md"])),
            patch.object(ci_gate, "_run_script", AsyncMock()) as run,
        ):
            result = await _gate("lint", tmp_path)
        assert result.condition == "pass"
        assert "no changed Python files" in result.message
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lint_runs_the_scoped_script_in_the_gate_workspace(self, tmp_path: Path) -> None:
        _write(tmp_path, "pyproject.toml")
        _write(tmp_path, "src/changed.py")
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(return_value=["src/changed.py"])) as diff,
            patch.object(ci_gate, "_run_script", AsyncMock(return_value=(1, f"E501 in src/changed.py {TOKEN}"))) as run,
        ):
            result = await _gate("lint", tmp_path)
        diff.assert_awaited_once_with(str(tmp_path), "main")
        workspace, script = run.await_args.args
        assert workspace == str(tmp_path)
        assert "python -m ruff check --force-exclude ./src/changed.py" in script
        assert result.condition == "fail"
        assert "E501" in result.output and TOKEN not in result.output

    @pytest.mark.asyncio
    async def test_tests_pass_on_exit_zero(self, tmp_path: Path) -> None:
        _write(tmp_path, "pyproject.toml")
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(side_effect=AssertionError("tests need no diff"))),
            patch.object(ci_gate, "_run_script", AsyncMock(return_value=(0, "3 passed"))) as run,
        ):
            result = await _gate("tests", tmp_path)
        assert result.condition == "pass"
        assert "python -m pytest" in run.await_args.args[1]


class TestMain:
    def test_prints_the_result_last_and_exits_zero_only_on_pass(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", TOKEN)
        passed = GateResult(condition="pass", message="lint passed")
        with patch.object(ci_gate, "run_gate", AsyncMock(return_value=passed)) as gate:
            assert ci_gate.main(["lint", "--repo=acme/widgets", "--branch=henchmen/t", "--base=main"]) == 0
        assert gate.await_args.kwargs["token"] == TOKEN
        last = capsys.readouterr().out.strip().splitlines()[-1]
        assert parse_gate_result(last) == passed

        with patch.object(ci_gate, "run_gate", AsyncMock(return_value=GateResult(condition="fail", message="x"))):
            assert ci_gate.main(["tests", "--repo=acme/widgets", "--branch=b", "--base=main"]) == 1

    def test_an_unexpected_error_is_a_fail(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", TOKEN)
        with patch.object(ci_gate, "run_gate", AsyncMock(side_effect=OSError(f"disk full {TOKEN}"))):
            assert ci_gate.main(["lint", "--repo=a/b", "--branch=b", "--base=main"]) == 1
        result = parse_gate_result(capsys.readouterr().out)
        assert result is not None and result.condition == "fail"
        assert TOKEN not in result.message


def test_parse_gate_result_takes_the_last_marker_and_rejects_garbage() -> None:
    old = GateResult(condition="fail", message="old")
    good = GateResult(condition="pass", message="ok")
    stdout = "\n".join(
        ["noise", GATE_RESULT_MARKER + old.model_dump_json(), GATE_RESULT_MARKER + good.model_dump_json(), ""]
    )
    assert parse_gate_result(stdout) == good
    assert parse_gate_result("no marker here") is None
    assert parse_gate_result(f"{GATE_RESULT_MARKER}{{not json") is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("node-pnpm", "pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile"),
        ("node-npm", "npm ci || npm install --no-audit"),
        ("go", None),
    ],
)
def test_install_script_keeps_the_lockfile_fallbacks(name: str, expected: str | None) -> None:
    commands = {"node-pnpm": ["pnpm", "install", "--frozen-lockfile"], "node-npm": ["npm", "ci"]}
    assert install_script(Stack(name=name, install_command=commands.get(name))) == expected


# ---------------------------------------------------------------------------
# The Mastermind side: _run_ci_check in local mode
# ---------------------------------------------------------------------------


def _task() -> MagicMock:
    task = MagicMock()
    task.context.repo = "acme/widgets"
    task.context.branch = "main"
    task.branch_name = "henchmen/t"
    task.id = "t-1"
    return task


def _local_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "provider": "local",
        "github_token": TOKEN,
        "operative_image": "ghcr.io/acme/henchmen/operative:1.0.0",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _proc(returncode: int, stdout: bytes) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, None))
    return proc


def _marker(condition: str, message: str, output: str = "") -> bytes:
    result = GateResult(condition=condition, message=message, output=output)  # type: ignore[arg-type]
    return f"{GATE_RESULT_MARKER}{result.model_dump_json()}\n".encode()


async def _check(settings: Settings, proc: MagicMock, check: str = "lint") -> tuple[dict[str, Any], Any]:
    from henchmen.mastermind.scheme_executor import handlers

    with (
        patch("henchmen.config.settings.get_settings", return_value=settings),
        patch.object(handlers.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)) as exec_mock,
        patch.object(
            handlers.tempfile,
            "mkdtemp",
            side_effect=AssertionError("local gates must not create a host workspace"),
        ),
    ):
        result = await handlers._run_ci_check(MagicMock(), _task(), check)
    return result, exec_mock


class TestLocalGateInvocation:
    @pytest.mark.asyncio
    async def test_no_bind_mount_and_no_token_on_the_command_line(self) -> None:
        result, exec_mock = await _check(_local_settings(), _proc(0, _marker("pass", "lint passed")))
        argv = list(exec_mock.await_args.args)
        assert result == {"condition": "pass", "message": "lint passed", "output": ""}
        assert "-v" not in argv and "--volume" not in argv and "--mount" not in argv
        assert argv[argv.index("-e") + 1] == "HENCHMEN_GITHUB_TOKEN"
        assert all(TOKEN not in arg for arg in argv)
        assert exec_mock.await_args.kwargs["env"]["HENCHMEN_GITHUB_TOKEN"] == TOKEN
        image_at = argv.index("ghcr.io/acme/henchmen/operative:1.0.0")
        assert argv[argv.index("--entrypoint") + 1] == "python"
        assert argv[image_at + 1 :] == [
            "-m",
            "henchmen.mastermind.scheme_executor.ci_gate",
            "lint",
            "--repo=acme/widgets",
            "--branch=henchmen/t",
            "--base=main",
        ]
        assert "--network" not in argv

    @pytest.mark.asyncio
    async def test_joins_the_configured_network_and_uses_the_default_image(self) -> None:
        settings = _local_settings(operative_image="", local_docker_network="henchmen")
        _, exec_mock = await _check(settings, _proc(0, _marker("pass", "ok")), check="tests")
        argv = list(exec_mock.await_args.args)
        assert argv[argv.index("--network") + 1] == "henchmen"
        assert "henchmen-operative:local" in argv

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("returncode", "stdout", "fragment"),
        [
            (0, b"crashed before printing\n", "without a result"),
            (1, _marker("pass", "lint passed"), "reported a pass but exited 1"),
            (
                1,
                _marker("fail", f"lint failed — could not determine the files changed against main: {TOKEN}"),
                "could not determine",
            ),
            (137, b"", "without a result"),
        ],
    )
    async def test_every_non_pass_outcome_fails_closed(self, returncode: int, stdout: bytes, fragment: str) -> None:
        result, _ = await _check(_local_settings(), _proc(returncode, stdout))
        assert result["condition"] == "fail"
        assert fragment in result["message"]
        assert TOKEN not in result["message"] and TOKEN not in result["output"]

    @pytest.mark.asyncio
    async def test_a_hung_gate_is_killed_and_fails(self) -> None:
        from henchmen.mastermind.scheme_executor import handlers

        async def _hang() -> tuple[bytes, None]:
            await asyncio.sleep(5)
            return b"", None

        hung = MagicMock()
        hung.returncode = None
        hung.communicate = _hang
        killer = _proc(0, b"")
        with (
            patch("henchmen.config.settings.get_settings", return_value=_local_settings()),
            patch.object(
                handlers.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[hung, killer])
            ) as exec_mock,
            patch.object(handlers, "_gate_timeout_seconds", return_value=0.01),
        ):
            result = await handlers._run_ci_check(MagicMock(), _task(), "tests")
        assert result["condition"] == "fail"
        assert "did not finish" in result["message"]
        kill_argv = exec_mock.await_args_list[1].args
        assert kill_argv[:2] == ("docker", "kill") and kill_argv[2].startswith("henchmen-gate-")
        hung.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_hung_gate_survives_a_failed_kill(self) -> None:
        """The timeout path must still return a clean fail result even when `docker kill` itself errors."""
        from henchmen.mastermind.scheme_executor import handlers

        async def _hang() -> tuple[bytes, None]:
            await asyncio.sleep(5)
            return b"", None

        hung = MagicMock()
        hung.returncode = None
        hung.communicate = _hang
        with (
            patch("henchmen.config.settings.get_settings", return_value=_local_settings()),
            patch.object(
                handlers.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[hung, OSError("docker not running")])
            ),
            patch.object(handlers, "_gate_timeout_seconds", return_value=0.01),
        ):
            result = await handlers._run_ci_check(MagicMock(), _task(), "tests")
        assert result["condition"] == "fail"
        assert "did not finish" in result["message"]

    @pytest.mark.asyncio
    async def test_docker_unavailable_fails(self) -> None:
        from henchmen.mastermind.scheme_executor import handlers

        with (
            patch("henchmen.config.settings.get_settings", return_value=_local_settings()),
            patch.object(
                handlers.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError("docker"))
            ),
        ):
            result = await handlers._run_ci_check(MagicMock(), _task(), "lint")
        assert result["condition"] == "fail"
        assert "error" in result["message"]
