"""Local CI gates clone and diff inside the gate container; nothing is bind-mounted (D-P9)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
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
_HAS_GIT = shutil.which("git") is not None


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


class _FakeStream:
    """A minimal async stream: ``.read(n)`` yields from a fixed buffer, then EOF (``b""``)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        size = n if n is not None and n >= 0 else len(self._data) - self._pos
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


class _HangingStream:
    """A stream whose ``.read`` never returns within any test's timeout."""

    async def read(self, n: int = -1) -> bytes:
        await asyncio.sleep(5)
        return b""


def _gate_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    """A fake ``docker run`` process exposing the streaming interface `_run_gate_in_container` reads."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = _FakeStream(stdout)
    proc.stderr = _FakeStream(stderr)
    proc.wait = AsyncMock(return_value=returncode)
    return proc


def _hanging_proc() -> MagicMock:
    """A fake ``docker run`` process that never produces output or exits on its own.

    ``.wait()`` only resolves once ``.kill()`` has been called, mirroring a
    real process: it never returns on its own (so the initial `gather` times
    out), but a `proc.kill()` afterward must not leave a second `await
    proc.wait()` hanging too.
    """
    proc = MagicMock()
    proc.returncode = None
    proc.stdout = _HangingStream()
    proc.stderr = _HangingStream()
    killed = asyncio.Event()

    async def _wait() -> int:
        await killed.wait()
        proc.returncode = 0
        return 0

    proc.wait = _wait
    proc.kill = MagicMock(side_effect=killed.set)
    return proc


def _communicate_proc(returncode: int, stdout: bytes = b"") -> MagicMock:
    """A fake process for the simple one-shot ``docker kill`` subprocess call."""
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
        result, exec_mock = await _check(_local_settings(), _gate_proc(0, stdout=_marker("pass", "lint passed")))
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
        _, exec_mock = await _check(settings, _gate_proc(0, stdout=_marker("pass", "ok")), check="tests")
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
            (0, b"HENCHMEN_GATE_RESULT {not json\n", "without a result"),
        ],
    )
    async def test_every_non_pass_outcome_fails_closed(self, returncode: int, stdout: bytes, fragment: str) -> None:
        result, _ = await _check(_local_settings(), _gate_proc(returncode, stdout=stdout))
        assert result["condition"] == "fail"
        assert fragment in result["message"]
        assert TOKEN not in result["message"] and TOKEN not in result["output"]

    @pytest.mark.asyncio
    async def test_a_hung_gate_is_killed_and_fails(self) -> None:
        from henchmen.mastermind.scheme_executor import handlers

        hung = _hanging_proc()
        killer = _communicate_proc(0, b"")
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
        """When `docker kill` itself errors, cleanup falls back to `docker rm -f` and still fails cleanly."""
        from henchmen.mastermind.scheme_executor import handlers

        hung = _hanging_proc()
        remover = _communicate_proc(0, b"")
        with (
            patch("henchmen.config.settings.get_settings", return_value=_local_settings()),
            patch.object(
                handlers.asyncio,
                "create_subprocess_exec",
                AsyncMock(side_effect=[hung, OSError("docker not running"), remover]),
            ) as exec_mock,
            patch.object(handlers, "_gate_timeout_seconds", return_value=0.01),
        ):
            result = await handlers._run_ci_check(MagicMock(), _task(), "tests")
        assert result["condition"] == "fail"
        assert "did not finish" in result["message"]
        rm_argv = exec_mock.await_args_list[2].args
        assert rm_argv[:3] == ("docker", "rm", "-f") and rm_argv[3].startswith("henchmen-gate-")

    @pytest.mark.asyncio
    async def test_a_cancelled_run_kills_the_container_and_cancellederror_propagates(self) -> None:
        """Cancelling `_run_gate_in_container` itself (not a timeout) must still clean up
        the container, and must not swallow the cancellation into a fail result.
        """
        from henchmen.mastermind.scheme_executor import handlers

        hung = _hanging_proc()
        killer = _communicate_proc(0, b"")
        with patch.object(
            handlers.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[hung, killer])
        ) as exec_mock:
            task = asyncio.ensure_future(
                handlers._run_gate_in_container(
                    _local_settings(), "tests", repo="acme/widgets", branch="henchmen/t", base_branch="main"
                )
            )
            await asyncio.sleep(0.01)  # let it reach the gather before cancelling
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        kill_argv = exec_mock.await_args_list[1].args
        assert kill_argv[:2] == ("docker", "kill") and kill_argv[2].startswith("henchmen-gate-")
        hung.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_hung_kill_does_not_hang_the_handler(self) -> None:
        """`_kill_gate_container` itself hanging (an unresponsive docker daemon) must not hang
        the handler forever -- cleanup is bounded and gives up, logging, well short of it.
        """
        from henchmen.mastermind.scheme_executor import handlers

        hung = _hanging_proc()

        async def _slow_kill(name: str) -> None:
            await asyncio.sleep(5)

        with (
            patch("henchmen.config.settings.get_settings", return_value=_local_settings()),
            patch.object(handlers.asyncio, "create_subprocess_exec", AsyncMock(return_value=hung)),
            patch.object(handlers, "_gate_timeout_seconds", return_value=0.01),
            patch.object(handlers, "_kill_gate_container", AsyncMock(side_effect=_slow_kill)) as kill_mock,
            patch.object(handlers, "_GATE_CLEANUP_TIMEOUT_SECONDS", 0.01),
        ):
            result = await asyncio.wait_for(handlers._run_ci_check(MagicMock(), _task(), "tests"), timeout=2.0)
        assert result["condition"] == "fail"
        assert "did not finish" in result["message"]
        kill_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_outer_cancel_during_cleanup_still_propagates(self) -> None:
        """A cancellation aimed at this coroutine itself, arriving *while cleanup is already
        running*, must still propagate as CancelledError -- the bounded
        `asyncio.wait_for` around cleanup only converts its own internal
        timeout, never an external cancellation, into a caught exception.
        """
        from henchmen.mastermind.scheme_executor import handlers

        hung = _hanging_proc()
        cleanup_started = asyncio.Event()

        async def _slow_kill(name: str) -> None:
            cleanup_started.set()
            await asyncio.sleep(5)

        with (
            patch.object(handlers.asyncio, "create_subprocess_exec", AsyncMock(return_value=hung)),
            patch.object(handlers, "_gate_timeout_seconds", return_value=0.01),
            patch.object(handlers, "_kill_gate_container", AsyncMock(side_effect=_slow_kill)),
        ):
            task = asyncio.ensure_future(
                handlers._run_gate_in_container(
                    _local_settings(), "tests", repo="acme/widgets", branch="henchmen/t", base_branch="main"
                )
            )
            await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

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


class TestBoundedGateOutput:
    """Ruling 4 (D-P9): a flooding gate container must not exhaust server memory."""

    @pytest.mark.asyncio
    async def test_flooded_stdout_is_capped_and_the_final_marker_is_still_found(self) -> None:
        from henchmen.mastermind.scheme_executor import handlers

        noise = b"x" * handlers._GATE_READ_CHUNK_BYTES
        chunk_count = (handlers._GATE_OUTPUT_TAIL_BYTES * 10) // len(noise) + 1
        # A newline separates the flood from the marker line: real stdout content
        # is line-oriented, and `parse_gate_result` looks at the *last line*.
        stdout = noise * chunk_count + b"\n" + _marker("pass", "lint passed")
        assert len(stdout) > handlers._GATE_OUTPUT_TAIL_BYTES * 10

        result, _ = await _check(_local_settings(), _gate_proc(0, stdout=stdout))
        assert result == {"condition": "pass", "message": "lint passed", "output": ""}

    def test_bounded_tail_retains_at_most_cap_plus_one_chunk(self) -> None:
        from henchmen.mastermind.scheme_executor.handlers import _BoundedTail

        cap = 1000
        chunk = b"a" * 300
        tail = _BoundedTail(cap=cap)
        for _ in range(50):  # 15000 bytes, 15x the cap
            tail.add(chunk)
        value = tail.getvalue()
        assert len(value) <= cap + len(chunk)
        assert value.endswith(chunk)

    def test_tail_chars_untrimmed_text_is_returned_unchanged(self) -> None:
        from henchmen.mastermind.scheme_executor.handlers import _tail_chars

        assert _tail_chars("short", limit=100) == "short"

    def test_tail_chars_drops_the_partial_leading_line_when_trimmed(self) -> None:
        from henchmen.mastermind.scheme_executor.handlers import _tail_chars

        text = "A" * 20 + "\nCLEAN LINE"
        assert _tail_chars(text, limit=15) == "CLEAN LINE"

    def test_tail_chars_drops_to_the_next_whitespace_when_no_newline_survives(self) -> None:
        """A tail with no newline anywhere in the kept window must not leave a partial
        token/word fragment at the very start -- drop up to the next whitespace instead.
        """
        from henchmen.mastermind.scheme_executor.handlers import _tail_chars

        text = "ghp_supersecrettokenfragment CLEAN_WORD_AFTER"
        result = _tail_chars(text, limit=len("secrettokenfragment CLEAN_WORD_AFTER"))
        assert result == "CLEAN_WORD_AFTER"
        assert "ghp_" not in result and "secrettokenfragment" not in result

    def test_tail_chars_with_no_whitespace_at_all_falls_back_to_the_raw_slice(self) -> None:
        from henchmen.mastermind.scheme_executor.handlers import _tail_chars

        text = "a" * 40
        result = _tail_chars(text, limit=10)
        assert result == "a" * 10

    @pytest.mark.asyncio
    async def test_stderr_flood_does_not_block_stdout_from_being_read(self) -> None:
        """stdout and stderr must be drained concurrently, not stdout-then-stderr.

        `_CoupledStreams.stdout` only yields its payload once `.stderr` has been
        fully drained -- modeling a child process whose stdout write is stuck
        behind a full, undrained stderr pipe. If the implementation read stdout
        to EOF before ever touching stderr, this would hang forever; the outer
        `asyncio.wait_for` below turns that into a test failure instead of an
        actually-hung test process.
        """
        from henchmen.mastermind.scheme_executor import handlers

        class _CoupledStreams:
            def __init__(self, stderr_chunk_count: int, stdout_payload: bytes) -> None:
                self._stderr_remaining = stderr_chunk_count
                self._stdout_payload = stdout_payload
                self._stderr_drained = asyncio.Event()
                self._stdout_sent = False

            async def read_stdout(self, n: int = -1) -> bytes:
                if self._stdout_sent:
                    return b""
                await self._stderr_drained.wait()
                self._stdout_sent = True
                return self._stdout_payload

            async def read_stderr(self, n: int = -1) -> bytes:
                if self._stderr_remaining <= 0:
                    self._stderr_drained.set()
                    return b""
                self._stderr_remaining -= 1
                return b"e" * handlers._GATE_READ_CHUNK_BYTES

        class _BoundStream:
            def __init__(self, read: Any) -> None:
                self.read = read

        coupled = _CoupledStreams(stderr_chunk_count=50, stdout_payload=_marker("pass", "tests passed"))
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = _BoundStream(coupled.read_stdout)
        proc.stderr = _BoundStream(coupled.read_stderr)
        proc.wait = AsyncMock(return_value=0)

        with (
            patch("henchmen.config.settings.get_settings", return_value=_local_settings()),
            patch.object(handlers.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch.object(
                handlers.tempfile,
                "mkdtemp",
                side_effect=AssertionError("local gates must not create a host workspace"),
            ),
        ):
            result = await asyncio.wait_for(handlers._run_ci_check(MagicMock(), _task(), "tests"), timeout=2.0)
        assert result == {"condition": "pass", "message": "tests passed", "output": ""}

    @pytest.mark.asyncio
    async def test_no_result_fallback_shows_stderr_first_with_partial_leading_line_dropped(self) -> None:
        from henchmen.mastermind.scheme_executor import handlers

        limit = handlers._GATE_OUTPUT_LIMIT
        stderr = b"OLD_STDERR_START\n" + b"e" * (limit * 2) + b"\nSTDERR_TAIL_MARKER\n"
        stdout = b"OLD_STDOUT_START\n" + b"o" * (limit * 2) + b"\nSTDOUT_TAIL_MARKER\n"

        result, _ = await _check(_local_settings(), _gate_proc(0, stdout=stdout, stderr=stderr))

        assert result["condition"] == "fail"
        assert "without a result" in result["message"]
        output = result["output"]
        assert output == "STDERR_TAIL_MARKER\nSTDOUT_TAIL_MARKER\n"
        assert output.index("STDERR_TAIL_MARKER") < output.index("STDOUT_TAIL_MARKER")
        assert "OLD_STDERR_START" not in output
        assert "OLD_STDOUT_START" not in output


class TestGateResourceLimits:
    """Ruling 1: the gate container gets the same resource limits a lair gets, plus a pids cap."""

    @pytest.mark.asyncio
    async def test_the_gate_container_gets_lair_memory_and_cpu_limits_and_a_pids_cap(self) -> None:
        settings = _local_settings(lair_default_memory="2Gi", lair_default_cpu="1.5")
        _, exec_mock = await _check(settings, _gate_proc(0, stdout=_marker("pass", "ok")))
        argv = list(exec_mock.await_args.args)
        assert argv[argv.index("--memory") + 1] == "2g"
        assert argv[argv.index("--cpus") + 1] == "1.5"
        assert argv[argv.index("--pids-limit") + 1] == "1024"

    @pytest.mark.asyncio
    async def test_reuses_the_docker_orchestrator_cpu_limit_helper(self) -> None:
        """No duplicated `--cpus` formatting logic: an unparsable cpu value is omitted, exactly
        as `providers.local.docker.cpu_limit` (used by lairs) already behaves.
        """
        settings = _local_settings(lair_default_cpu="not-a-number")
        _, exec_mock = await _check(settings, _gate_proc(0, stdout=_marker("pass", "ok")))
        argv = list(exec_mock.await_args.args)
        assert "--cpus" not in argv
        assert "--memory" in argv  # still present -- only --cpus is conditional


class TestTokenNeverReachesRepoCode:
    """Ruling 2 (D-P9): inside the gate container only, once cloning/diffing is done,

    repo-controlled code must not be able to read the GitHub token back out of
    `.git/config` or its own environment. The cloud host path is unaffected
    (global constraint: non-desktop behaviour is unchanged).
    """

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_GIT, reason="git is not installed")
    async def test_the_git_remote_no_longer_holds_the_token_after_planning(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
        }
        subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True, env=git_env)
        subprocess.run(
            ["git", "remote", "add", "origin", f"https://x-access-token:{TOKEN}@github.com/acme/widgets.git"],
            cwd=repo_dir,
            check=True,
            env=git_env,
        )

        result = await ci_gate._strip_remote_token(str(repo_dir), "acme/widgets", "lint", TOKEN)

        assert result is None
        remote_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
            env=git_env,
        ).stdout.strip()
        assert remote_url == "https://github.com/acme/widgets.git"
        assert TOKEN not in remote_url

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_GIT, reason="git is not installed")
    async def test_a_remote_url_reset_failure_fails_the_gate_closed(self, tmp_path: Path) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
        # No "origin" remote configured -- `git remote set-url origin` must fail.
        result = await ci_gate._strip_remote_token(str(repo_dir), "acme/widgets", "lint", TOKEN)
        assert result is not None
        assert result.condition == "fail"
        assert "could not remove the token from the git remote" in result.message

    @pytest.mark.asyncio
    async def test_run_script_env_has_no_github_token_variables(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", TOKEN)
        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
        monkeypatch.setenv("GH_TOKEN", TOKEN)
        monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
        before = dict(os.environ)

        with patch.object(
            ci_gate.asyncio, "create_subprocess_exec", AsyncMock(return_value=_communicate_proc(0, b"ok"))
        ) as exec_mock:
            await ci_gate._run_script(str(tmp_path), "true")

        env = exec_mock.await_args.kwargs["env"]
        assert "HENCHMEN_GITHUB_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env
        assert "GH_TOKEN" not in env
        assert env.get("SOME_OTHER_VAR") == "keep-me"
        assert dict(os.environ) == before  # os.environ itself is never mutated

    @pytest.mark.asyncio
    async def test_run_gate_resets_the_remote_before_running_the_script(self, tmp_path: Path) -> None:
        """The container path (run_gate) strips the remote token; plan_gate itself never does."""
        _write(tmp_path, "pyproject.toml")
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(side_effect=AssertionError("tests need no diff"))),
            patch.object(ci_gate, "_strip_remote_token", AsyncMock(return_value=None)) as strip,
            patch.object(ci_gate, "_run_script", AsyncMock(return_value=(0, "3 passed"))) as run,
        ):
            result = await _gate("tests", tmp_path)

        assert result.condition == "pass"
        strip.assert_awaited_once_with(str(tmp_path), "acme/widgets", "tests", TOKEN)
        run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_plan_gate_never_resets_the_remote_the_cloud_path_shares_it(self, tmp_path: Path) -> None:
        """Global constraint: non-desktop behaviour is unchanged -- only `run_gate` scrubs, not `plan_gate`."""
        _write(tmp_path, "pyproject.toml")
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(side_effect=AssertionError("tests need no diff"))),
            patch.object(ci_gate, "_strip_remote_token", AsyncMock()) as strip,
        ):
            planned = await ci_gate.plan_gate(
                "tests",
                repo="acme/widgets",
                branch="henchmen/t",
                base_branch="main",
                token=TOKEN,
                workspace=str(tmp_path),
            )
        assert isinstance(planned, ci_gate.GatePlan)
        strip.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_on_host_inherits_the_ambient_environment_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Global constraint: non-desktop behaviour is unchanged -- the cloud path must not scrub."""
        from henchmen.mastermind.scheme_executor import handlers
        from henchmen.mastermind.scheme_executor.lint_scope import CheckCommand
        from henchmen.utils.stack_detector import Stack

        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
        stack = Stack(name="python", test_command=["python", "-m", "pytest"], install_command=None)
        commands = (CheckCommand(argv=("python", "-m", "pytest")),)
        ok_proc = MagicMock()
        ok_proc.returncode = 0
        ok_proc.communicate = AsyncMock(return_value=(b"3 passed", b""))

        with patch.object(handlers.asyncio, "create_subprocess_exec", AsyncMock(return_value=ok_proc)) as exec_mock:
            await handlers._run_on_host(str(tmp_path), stack, commands)

        # No `env=` kwarg at all: the subprocess inherits this process's full,
        # unfiltered environment (including GITHUB_TOKEN), exactly as before.
        assert "env" not in exec_mock.await_args.kwargs
