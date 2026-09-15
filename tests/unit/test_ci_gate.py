"""Local CI gates clone and diff inside the gate container; nothing is bind-mounted (D-P9)."""

from __future__ import annotations

import asyncio
import io
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
    Sandbox,
    install_script,
    parse_gate_result,
    run_gate,
)
from henchmen.mastermind.scheme_executor.lint_scope import LintScopeError
from henchmen.utils.stack_detector import Stack

TOKEN = "ghp_" + "s" * 36
_HAS_GIT = shutil.which("git") is not None
SANDBOX = Sandbox(uid=65534, gid=65534, home="/tmp/henchmen-gate-home-x")


@pytest.fixture
def sandboxed() -> Any:
    """Privilege dropping needs root on Linux; unit tests stand in a ready sandbox."""
    with patch.object(ci_gate, "_prepare_sandbox", return_value=SANDBOX) as prepare:
        yield prepare


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def _gate(check: str, workspace: Path) -> GateResult:
    return await run_gate(  # type: ignore[arg-type]
        check, repo="acme/widgets", branch="henchmen/t", base_branch="main", token=TOKEN, workspace=str(workspace)
    )


@pytest.mark.usefixtures("sandboxed")
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
        assert run.await_args.kwargs["sandbox"] == SANDBOX
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
        monkeypatch.setattr(ci_gate.sys, "stdin", io.StringIO(f"{TOKEN}\n"))
        passed = GateResult(condition="pass", message="lint passed")
        with patch.object(ci_gate, "run_gate", AsyncMock(return_value=passed)) as gate:
            assert ci_gate.main(["lint", "--repo=acme/widgets", "--branch=henchmen/t", "--base=main"]) == 0
        assert gate.await_args.kwargs["token"] == TOKEN
        last = capsys.readouterr().out.strip().splitlines()[-1]
        assert parse_gate_result(last) == passed

        monkeypatch.setattr(ci_gate.sys, "stdin", io.StringIO(""))
        with patch.object(ci_gate, "run_gate", AsyncMock(return_value=GateResult(condition="fail", message="x"))):
            assert ci_gate.main(["tests", "--repo=acme/widgets", "--branch=b", "--base=main"]) == 1

    def test_the_token_comes_from_stdin_never_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", "ghp_" + "e" * 36)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "e" * 36)
        monkeypatch.setattr(ci_gate.sys, "stdin", io.StringIO(f"{TOKEN}\nignored second line\n"))
        passed = GateResult(condition="pass", message="ok")
        with patch.object(ci_gate, "run_gate", AsyncMock(return_value=passed)) as gate:
            assert ci_gate.main(["lint", "--repo=a/b", "--branch=b", "--base=main"]) == 0
        assert gate.await_args.kwargs["token"] == TOKEN

    def test_fix_dispatches_to_run_fix_with_the_author_identity(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(ci_gate.sys, "stdin", io.StringIO(f"{TOKEN}\n"))
        done = GateResult(condition="pass", message="fix_lint: auto-fix made no changes")
        with patch.object(ci_gate, "run_fix", AsyncMock(return_value=done)) as fix:
            code = ci_gate.main(
                ["fix", "--repo=a/b", "--branch=henchmen/t", "--base=main", "--author-name=Ann", "--author-email=a@x"]
            )
        assert code == 0
        kwargs = fix.await_args.kwargs
        assert kwargs["token"] == TOKEN and kwargs["branch"] == "henchmen/t"
        assert (kwargs["author_name"], kwargs["author_email"]) == ("Ann", "a@x")
        assert parse_gate_result(capsys.readouterr().out) == done

    def test_an_unexpected_error_is_a_fail(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(ci_gate.sys, "stdin", io.StringIO(f"{TOKEN}\n"))
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
    proc.stdin = _fake_stdin()
    proc.stdout = _FakeStream(stdout)
    proc.stderr = _FakeStream(stderr)
    proc.wait = AsyncMock(return_value=returncode)
    return proc


def _fake_stdin() -> MagicMock:
    stdin = MagicMock()
    stdin.drain = AsyncMock()
    return stdin


def _stdin_bytes(proc: MagicMock) -> bytes:
    return b"".join(call.args[0] for call in proc.stdin.write.call_args_list)


def _hanging_proc() -> MagicMock:
    """A fake ``docker run`` process that never produces output or exits on its own.

    ``.wait()`` only resolves once ``.kill()`` has been called, mirroring a
    real process: it never returns on its own (so the initial `gather` times
    out), but a `proc.kill()` afterward must not leave a second `await
    proc.wait()` hanging too.
    """
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = _fake_stdin()
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
    async def test_no_bind_mount_and_the_token_only_travels_over_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even a token in the server's own environment must not reach the docker CLI.
        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
        proc = _gate_proc(0, stdout=_marker("pass", "lint passed"))
        result, exec_mock = await _check(_local_settings(), proc)
        argv = list(exec_mock.await_args.args)
        assert result == {"condition": "pass", "message": "lint passed", "output": ""}
        assert "-v" not in argv and "--volume" not in argv and "--mount" not in argv
        assert "-e" not in argv and "--env" not in argv
        assert all(TOKEN not in arg for arg in argv)
        env = exec_mock.await_args.kwargs["env"]
        assert all(TOKEN not in value for value in env.values())
        assert not {"HENCHMEN_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"} & set(env)
        # The token is written to the container's stdin (docker run -i), then stdin is closed.
        assert "-i" in argv[: argv.index("--entrypoint")]
        assert exec_mock.await_args.kwargs["stdin"] == asyncio.subprocess.PIPE
        assert _stdin_bytes(proc) == f"{TOKEN}\n".encode()
        proc.stdin.close.assert_called_once()
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
    async def test_the_gate_starts_as_root_with_only_privilege_dropping_capabilities(self) -> None:
        _, exec_mock = await _check(_local_settings(), _gate_proc(0, stdout=_marker("pass", "ok")))
        argv = list(exec_mock.await_args.args)
        before_image = argv[: argv.index("ghcr.io/acme/henchmen/operative:1.0.0")]
        assert before_image[before_image.index("--user") + 1] == "0:0"
        assert before_image[before_image.index("--cap-drop") + 1] == "ALL"
        added = {before_image[i + 1] for i, arg in enumerate(before_image) if arg == "--cap-add"}
        assert added == {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"}
        assert before_image[before_image.index("--security-opt") + 1] == "no-new-privileges"

    @pytest.mark.asyncio
    async def test_no_token_configured_still_closes_stdin(self) -> None:
        proc = _gate_proc(0, stdout=_marker("pass", "ok"))
        await _check(_local_settings(github_token=""), proc)
        assert _stdin_bytes(proc) == b"\n"
        proc.stdin.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_gate_that_closed_stdin_early_fails_on_its_missing_result(self) -> None:
        proc = _gate_proc(125, stderr=b"docker: invalid reference format\n")
        proc.stdin.drain = AsyncMock(side_effect=BrokenPipeError())
        result, _ = await _check(_local_settings(), proc)
        assert result["condition"] == "fail"
        assert "exited 125 without a result" in result["message"]
        proc.stdin.close.assert_called_once()

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
                handlers.run_gate_in_container(
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
                handlers.run_gate_in_container(
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

    def test_tail_chars_with_no_whitespace_at_all_keeps_nothing(self) -> None:
        """A window with no whitespace could be one long secret fragment: fail closed (D9)."""
        from henchmen.mastermind.scheme_executor.handlers import _tail_chars

        text = "ghp_" + "a" * 40
        assert _tail_chars(text, limit=10) == ""

    def test_tail_chars_window_starting_on_whitespace_keeps_the_first_whole_word(self) -> None:
        """The cut landed on a word boundary, so nothing is partial: only the leading whitespace goes (D9)."""
        from henchmen.mastermind.scheme_executor.handlers import _tail_chars

        text = "PARTIAL_PREFIX" + "   WHOLE next"
        assert _tail_chars(text, limit=len("   WHOLE next")) == "WHOLE next"

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
        proc.stdin = _fake_stdin()
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

        limit = handlers.GATE_OUTPUT_LIMIT
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
            await ci_gate._run_script(str(tmp_path), "true", sandbox=SANDBOX)

        kwargs = exec_mock.await_args.kwargs
        # Repo-controlled code runs as the unprivileged user with its own HOME.
        assert (kwargs["user"], kwargs["group"], kwargs["extra_groups"]) == (65534, 65534, [])
        env = kwargs["env"]
        assert env["HOME"] == SANDBOX.home
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
            patch.object(ci_gate, "_prepare_sandbox", return_value=SANDBOX),
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


# ---------------------------------------------------------------------------
# Privilege separation and `ci_gate fix` (decision C18)
# ---------------------------------------------------------------------------


class TestSandbox:
    def test_a_gate_not_started_as_root_fails_closed(self, tmp_path: Path) -> None:
        with patch.object(ci_gate, "_running_as_root", return_value=False):
            result = ci_gate._prepare_sandbox(str(tmp_path), "lint")
        assert isinstance(result, GateResult) and result.condition == "fail"
        assert "must start as root" in result.message

    def test_root_hands_the_workspace_and_a_fresh_home_to_the_unprivileged_user(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        with (
            patch.object(ci_gate, "_running_as_root", return_value=True),
            patch.object(ci_gate.tempfile, "mkdtemp", return_value=str(home)),
            patch.object(ci_gate, "_chown_tree") as chown,
        ):
            sandbox = ci_gate._prepare_sandbox(str(tmp_path), "tests")
        assert sandbox == Sandbox(uid=65534, gid=65534, home=str(home))
        chowned = {call.args for call in chown.call_args_list}
        assert chowned == {(str(home), 65534, 65534), (str(tmp_path), 65534, 65534)}

    def test_a_failed_chown_fails_closed(self, tmp_path: Path) -> None:
        with (
            patch.object(ci_gate, "_running_as_root", return_value=True),
            patch.object(ci_gate, "_chown_tree", side_effect=PermissionError("Operation not permitted")),
        ):
            result = ci_gate._prepare_sandbox(str(tmp_path), "lint")
        assert isinstance(result, GateResult) and result.condition == "fail"
        assert "could not hand the workspace over" in result.message

    @pytest.mark.asyncio
    async def test_run_gate_never_runs_repo_code_when_the_sandbox_cannot_be_prepared(self, tmp_path: Path) -> None:
        _write(tmp_path, "pyproject.toml")
        refused = GateResult(condition="fail", message="tests failed (the gate container must start as root)")
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "_prepare_sandbox", return_value=refused),
            patch.object(ci_gate, "_run_script", AsyncMock()) as run,
        ):
            result = await _gate("tests", tmp_path)
        assert result == refused
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kill_runs_as_the_unprivileged_user(self) -> None:
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        with patch.object(ci_gate.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)) as exec_mock:
            await ci_gate._kill_unprivileged(SANDBOX)
        assert exec_mock.await_args.args[:2] == ("/bin/bash", "-c")
        assert "kill -KILL -1" in exec_mock.await_args.args[2]
        assert exec_mock.await_args.kwargs["user"] == 65534


class TestPlanFix:
    @pytest.mark.asyncio
    async def test_fix_is_scoped_to_the_changed_files_and_keeps_them(self, tmp_path: Path) -> None:
        _write(tmp_path, "pyproject.toml")
        _write(tmp_path, "src/changed.py")
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(return_value=["src/changed.py", "README.md"])),
        ):
            planned = await _plan_fix(tmp_path)
        assert isinstance(planned, ci_gate.GatePlan)
        assert planned.changed == ("src/changed.py", "README.md")
        (command,) = planned.commands
        assert command.argv[-1] == "--fix" and "./src/changed.py" in command.argv

    @pytest.mark.asyncio
    async def test_nothing_to_fix_is_a_pass_without_a_plan(self, tmp_path: Path) -> None:
        _write(tmp_path, "go.mod")
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(return_value=["main.go"])),
        ):
            planned = await _plan_fix(tmp_path)
        assert isinstance(planned, GateResult) and planned.condition == "pass"
        assert "nothing to auto-fix" in planned.message

    @pytest.mark.asyncio
    async def test_a_fix_clone_failure_fails_and_hides_the_token(self, tmp_path: Path) -> None:
        with patch.object(ci_gate, "clone_repo", AsyncMock(side_effect=RuntimeError(f"denied {TOKEN}"))):
            planned = await _plan_fix(tmp_path)
        assert isinstance(planned, GateResult) and planned.condition == "fail"
        assert planned.message.startswith("fix_lint failed (clone failed)")
        assert TOKEN not in planned.message


async def _plan_fix(workspace: Path) -> ci_gate.GatePlan | GateResult:
    return await ci_gate.plan_gate(
        "fix", repo="acme/widgets", branch="henchmen/t", base_branch="main", token=TOKEN, workspace=str(workspace)
    )


def _fix_plan(changed: tuple[str, ...] = ("src/app.py",)) -> ci_gate.GatePlan:
    from henchmen.mastermind.scheme_executor.lint_scope import CheckCommand

    stack = Stack(name="python", lint_command=["python", "-m", "ruff", "check", "."])
    command = CheckCommand(argv=("python", "-m", "ruff", "check", "--force-exclude", "./src/app.py", "--fix"))
    return ci_gate.GatePlan(stack=stack, commands=(command,), changed=changed)


class _FixHarness:
    """Patches every side effect of `run_fix` and records the order they happen in."""

    def __init__(self, status: str, failing_step: str | None = None, fixer_error: Exception | None = None) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.git_calls: list[tuple[tuple[str, ...], str | None]] = []
        self._status = status
        self._failing_step = failing_step
        self._fixer_error = fixer_error

    async def fixer(self, argv: tuple[str, ...], cwd: str, sandbox: Sandbox) -> tuple[int, str]:
        self.events.append(("fixer", argv, sandbox))
        if self._fixer_error is not None:
            raise self._fixer_error
        return 0, "Found 1 error (1 fixed)"

    async def kill(self, sandbox: Sandbox) -> None:
        self.events.append(("kill", sandbox))

    async def git(
        self, git_dir: str, workspace: str, private: str, *args: str, token: str | None = None
    ) -> tuple[int, str, str]:
        self.events.append(("git", args))
        name = next(arg for arg in args if arg in {"status", "checkout", "add", "commit", "push"})
        self.git_calls.append((args[args.index(name) :], token))
        if name == "status":
            return 0, self._status, ""
        if name == self._failing_step:
            return 1, "", f"remote: denied for https://x-access-token:{TOKEN}@github.com"
        return 0, "", ""

    def patches(self) -> Any:
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(patch.object(ci_gate, "plan_gate", AsyncMock(return_value=_fix_plan())))
        stack.enter_context(patch.object(ci_gate, "_strip_remote_token", AsyncMock(return_value=None)))
        stack.enter_context(patch.object(ci_gate, "_isolate_git_dir", return_value="/private/git"))
        stack.enter_context(patch.object(ci_gate, "_prepare_sandbox", return_value=SANDBOX))
        stack.enter_context(patch.object(ci_gate, "_run_unprivileged", AsyncMock(side_effect=self.fixer)))
        stack.enter_context(patch.object(ci_gate, "_kill_unprivileged", AsyncMock(side_effect=self.kill)))
        stack.enter_context(patch.object(ci_gate, "_isolated_git", AsyncMock(side_effect=self.git)))
        return stack


async def _fix(workspace: Path) -> GateResult:
    return await ci_gate.run_fix(
        repo="acme/widgets",
        branch="henchmen/t",
        base_branch="main",
        token=TOKEN,
        workspace=str(workspace),
        private_dir="/private",
        author_name="Henchmen Bot",
        author_email="bot@example.com",
    )


class TestRunFix:
    @pytest.mark.asyncio
    async def test_fixes_run_unprivileged_then_every_process_dies_before_root_git_pushes(self, tmp_path: Path) -> None:
        harness = _FixHarness(status=" M src/app.py\0 M src/untouched.py\0")
        with harness.patches():
            result = await _fix(tmp_path)

        assert result.condition == "pass"
        assert result.message == "fix_lint: auto-fixed lint issues and pushed"
        kinds = [event[0] for event in harness.events]
        assert kinds.index("fixer") < kinds.index("kill") < kinds.index("git")
        assert harness.events[kinds.index("fixer")][2] == SANDBOX
        steps = [args for args, _ in harness.git_calls]
        assert ("checkout", "--", "src/untouched.py") in steps
        assert ("add", "--", "src/app.py") in steps
        assert ("push", "origin", "HEAD:refs/heads/henchmen/t") in steps
        # Only the push ever receives the token.
        assert [token for args, token in harness.git_calls if args[0] == "push"] == [TOKEN]
        assert all(token is None for args, token in harness.git_calls if args[0] != "push")

    @pytest.mark.asyncio
    async def test_the_commit_carries_the_configured_author(self, tmp_path: Path) -> None:
        harness = _FixHarness(status=" M src/app.py\0")
        recorded: list[tuple[str, ...]] = []
        original = harness.git

        async def _git(*args: Any, token: str | None = None) -> tuple[int, str, str]:
            recorded.append(tuple(args[3:]))
            return await original(*args, token=token)

        with harness.patches(), patch.object(ci_gate, "_isolated_git", AsyncMock(side_effect=_git)):
            await _fix(tmp_path)
        commit = next(args for args in recorded if "commit" in args)
        assert "user.name=Henchmen Bot" in commit and "user.email=bot@example.com" in commit

    @pytest.mark.asyncio
    async def test_no_in_scope_change_commits_nothing(self, tmp_path: Path) -> None:
        harness = _FixHarness(status=" M src/untouched.py\0")
        with harness.patches():
            result = await _fix(tmp_path)
        assert result.condition == "pass"
        assert result.message == "fix_lint: auto-fix made no changes"
        assert not any(args[0] in {"add", "push"} for args, _ in harness.git_calls)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("step", ["checkout", "add", "commit", "push"])
    async def test_every_git_failure_fails_closed_without_the_token(self, tmp_path: Path, step: str) -> None:
        harness = _FixHarness(status=" M src/app.py\0 M src/untouched.py\0", failing_step=step)
        with harness.patches():
            result = await _fix(tmp_path)
        assert result.condition == "fail"
        assert TOKEN not in result.message and TOKEN not in result.output
        if step != "push":
            assert not any(args[0] == "push" for args, _ in harness.git_calls)

    @pytest.mark.asyncio
    async def test_a_crashing_fixer_still_kills_every_unprivileged_process(self, tmp_path: Path) -> None:
        harness = _FixHarness(status="", fixer_error=FileNotFoundError("npx"))
        with harness.patches(), pytest.raises(FileNotFoundError):
            await _fix(tmp_path)
        assert ("kill", SANDBOX) in harness.events
        assert harness.git_calls == []

    @pytest.mark.asyncio
    async def test_a_planning_result_is_returned_without_touching_anything(self, tmp_path: Path) -> None:
        nothing = GateResult(condition="pass", message="fix_lint: nothing to auto-fix (no changed Python files)")
        with (
            patch.object(ci_gate, "plan_gate", AsyncMock(return_value=nothing)),
            patch.object(ci_gate, "_prepare_sandbox", side_effect=AssertionError("no sandbox needed")),
        ):
            assert await _fix(tmp_path) == nothing

    def test_isolated_git_env_carries_the_token_only_as_a_push_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
        plain = ci_gate._isolated_git_env("/private")
        assert "GITHUB_TOKEN" not in plain and "GIT_CONFIG_COUNT" not in plain
        assert plain["GIT_CONFIG_NOSYSTEM"] == "1" and plain["HOME"] == "/private"
        pushing = ci_gate._isolated_git_env("/private", TOKEN)
        assert pushing["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
        assert TOKEN not in pushing["GIT_CONFIG_VALUE_0"]  # base64-encoded basic credentials
        assert pushing["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")

    @pytest.mark.asyncio
    async def test_isolated_git_disables_hooks_and_names_the_private_git_dir(self) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        with patch.object(ci_gate.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)) as exec_mock:
            await ci_gate._isolated_git("/private/git", "/ws", "/private", "status", "--porcelain")
        argv = exec_mock.await_args.args
        assert argv[0] == "git"
        assert "core.hooksPath=/dev/null" in argv and "core.fsmonitor=false" in argv
        assert "--git-dir=/private/git" in argv and "--work-tree=/ws" in argv
        assert argv[-2:] == ("status", "--porcelain")

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_GIT or os.name == "nt", reason="needs git and POSIX hooks")
    async def test_repo_planted_hooks_never_run_during_the_root_commit(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        private = tmp_path / "private"
        workspace.mkdir()
        private.mkdir()
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(private)}
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True, env=env)
        _write(workspace, "a.py", "x=1\n")
        subprocess.run(["git", "add", "a.py"], cwd=workspace, check=True, env=env)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "init"],
            cwd=workspace,
            check=True,
            env=env,
        )
        git_dir = ci_gate._isolate_git_dir(str(workspace), str(private))
        assert isinstance(git_dir, str)
        assert not (workspace / ".git").exists()
        marker = tmp_path / "hook-ran"
        hook = Path(git_dir) / "hooks" / "pre-commit"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        hook.chmod(0o755)
        _write(workspace, "a.py", "x = 1\n")

        git = ci_gate._isolated_git
        assert (await git(git_dir, str(workspace), str(private), "add", "--", "a.py"))[0] == 0
        rc, _, err = await git(
            git_dir, str(workspace), str(private), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-m", "fix"
        )
        assert rc == 0, err
        assert not marker.exists()


# ---------------------------------------------------------------------------
# `ci_gate forge`: Forge CI's lint and tests over one clone and one install
# ---------------------------------------------------------------------------


class _ForgeHarness:
    """Records every unprivileged script and clone a `run_forge` call makes."""

    def __init__(self, returncodes: dict[str, int] | None = None) -> None:
        self.scripts: list[str] = []
        self._returncodes = returncodes or {}

    async def run_script(self, workspace: str, script: str, *, sandbox: Sandbox) -> tuple[int, str]:
        self.scripts.append(script)
        for fragment, returncode in self._returncodes.items():
            if fragment in script:
                return returncode, f"{fragment} output {TOKEN}"
        return 0, "ok"


def _node_workspace(root: Path) -> None:
    _write(root, "package.json", '{"devDependencies": {"eslint": "9"}, "scripts": {"test": "vitest"}}')
    _write(root, "package-lock.json", "{}")
    _write(root, "src/app.ts", "export const x = 1\n")


async def _forge(workspace: Path, **kwargs: Any) -> GateResult:
    return await ci_gate.run_forge(
        repo="acme/widgets", branch="feature", base_branch="main", token=TOKEN, workspace=str(workspace), **kwargs
    )


@pytest.mark.usefixtures("sandboxed")
class TestRunForge:
    @pytest.mark.asyncio
    async def test_one_clone_one_install_then_lint_and_tests(self, tmp_path: Path) -> None:
        _node_workspace(tmp_path)
        harness = _ForgeHarness()
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()) as clone,
            patch.object(ci_gate, "changed_files", AsyncMock(return_value=["src/app.ts"])),
            patch.object(ci_gate, "_strip_remote_token", AsyncMock(return_value=None)),
            patch.object(ci_gate, "_run_script", AsyncMock(side_effect=harness.run_script)),
        ):
            result = await _forge(tmp_path)

        clone.assert_awaited_once()
        install, lint, tests = harness.scripts
        assert install == "npm ci || npm install --no-audit"
        assert "npm ci" not in lint and "npm ci" not in tests, "dependencies are installed exactly once"
        assert "npx --no-install eslint ./src/app.ts" in lint
        assert "npm run --if-present test" in tests
        assert result.condition == "pass"
        assert [(check.name, check.condition) for check in result.checks] == [("lint", "pass"), ("tests", "pass")]

    @pytest.mark.asyncio
    async def test_a_node_prs_desktop_forge_lint_goes_through_plan_lint(self, tmp_path: Path) -> None:
        """Accepted strictness: Forge lint on desktop is the Mastermind lint gate's lint_scope, not ruff-only."""
        _node_workspace(tmp_path)
        harness = _ForgeHarness()
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(return_value=["src/app.ts"])),
            patch.object(ci_gate, "_strip_remote_token", AsyncMock(return_value=None)),
            patch.object(ci_gate, "plan_lint", wraps=ci_gate.plan_lint) as plan_lint,
            patch.object(ci_gate, "_run_script", AsyncMock(side_effect=harness.run_script)),
        ):
            result = await _forge(tmp_path, run_tests=False)
        plan_lint.assert_called_once()
        stack, _, changed = plan_lint.call_args.args
        assert stack.name == "node-npm" and changed == ["src/app.ts"]
        assert any("eslint ./src/app.ts" in script for script in harness.scripts)
        assert not any("ruff" in script for script in harness.scripts)
        assert [check.name for check in result.checks] == ["lint"]

    @pytest.mark.asyncio
    async def test_skip_tests_runs_lint_only(self, tmp_path: Path) -> None:
        _node_workspace(tmp_path)
        harness = _ForgeHarness()
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(return_value=["src/app.ts"])),
            patch.object(ci_gate, "_strip_remote_token", AsyncMock(return_value=None)),
            patch.object(ci_gate, "_run_script", AsyncMock(side_effect=harness.run_script)),
        ):
            result = await _forge(tmp_path, run_tests=False)
        assert not any("npm run --if-present test" in script for script in harness.scripts)
        assert [check.name for check in result.checks] == ["lint"]

    @pytest.mark.asyncio
    async def test_a_failed_install_fails_every_check_that_needs_it_without_running_it(self, tmp_path: Path) -> None:
        _node_workspace(tmp_path)
        harness = _ForgeHarness(returncodes={"npm ci": 1})
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(return_value=["src/app.ts"])),
            patch.object(ci_gate, "_strip_remote_token", AsyncMock(return_value=None)),
            patch.object(ci_gate, "_run_script", AsyncMock(side_effect=harness.run_script)),
        ):
            result = await _forge(tmp_path)
        assert len(harness.scripts) == 1
        assert result.condition == "fail"
        assert all(
            check.condition == "fail" and "dependency install failed" in check.message for check in result.checks
        )
        assert all(TOKEN not in check.output for check in result.checks)

    @pytest.mark.asyncio
    async def test_lint_and_tests_are_reported_separately(self, tmp_path: Path) -> None:
        _node_workspace(tmp_path)
        harness = _ForgeHarness(returncodes={"npm run --if-present test": 1})
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(return_value=["src/app.ts"])),
            patch.object(ci_gate, "_strip_remote_token", AsyncMock(return_value=None)),
            patch.object(ci_gate, "_run_script", AsyncMock(side_effect=harness.run_script)),
        ):
            result = await _forge(tmp_path)
        assert result.condition == "fail"
        lint, tests = result.checks
        assert (lint.name, lint.condition) == ("lint", "pass")
        assert (tests.name, tests.condition, tests.message) == ("tests", "fail", "tests failed")
        assert TOKEN not in tests.output

    @pytest.mark.asyncio
    async def test_a_clone_failure_fails_every_check(self, tmp_path: Path) -> None:
        with patch.object(ci_gate, "clone_repo", AsyncMock(side_effect=RuntimeError(f"denied {TOKEN}"))):
            result = await _forge(tmp_path)
        assert result.condition == "fail"
        assert [check.name for check in result.checks] == ["lint", "tests"]
        assert all("clone failed" in check.message and TOKEN not in check.message for check in result.checks)

    @pytest.mark.asyncio
    async def test_nothing_to_lint_and_no_sandbox_fails_only_what_had_to_run(self, tmp_path: Path) -> None:
        _node_workspace(tmp_path)
        refused = GateResult(condition="fail", message="forge failed (could not hand the workspace over)")
        with (
            patch.object(ci_gate, "clone_repo", AsyncMock()),
            patch.object(ci_gate, "changed_files", AsyncMock(return_value=["README.md"])),
            patch.object(ci_gate, "_strip_remote_token", AsyncMock(return_value=None)),
            patch.object(ci_gate, "_prepare_sandbox", return_value=refused),
            patch.object(
                ci_gate, "_run_script", AsyncMock(side_effect=AssertionError("no repo code without a sandbox"))
            ),
        ):
            result = await _forge(tmp_path)
        lint, tests = result.checks
        assert lint.condition == "pass" and "no changed JavaScript/TypeScript files" in lint.message
        assert tests.condition == "fail" and tests.message == refused.message

    def test_main_passes_skip_tests(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(ci_gate.sys, "stdin", io.StringIO(f"{TOKEN}\n"))
        done = GateResult(condition="pass", message="forge: lint passed")
        with patch.object(ci_gate, "run_forge", AsyncMock(return_value=done)) as forge:
            assert ci_gate.main(["forge", "--repo=a/b", "--branch=f", "--base=main", "--skip-tests"]) == 0
        assert forge.await_args.kwargs["run_tests"] is False and forge.await_args.kwargs["token"] == TOKEN

    def test_one_parser_reads_the_per_check_results(self) -> None:
        result = GateResult(
            condition="fail",
            message="forge: lint passed; tests failed",
            checks=[
                ci_gate.GateCheckResult(name="lint", condition="pass", message="lint passed"),
                ci_gate.GateCheckResult(name="tests", condition="fail", message="tests failed", output="boom"),
            ],
        )
        assert parse_gate_result(f"noise\n{GATE_RESULT_MARKER}{result.model_dump_json()}\n") == result


class TestGateRunnerForForge:
    @pytest.mark.asyncio
    async def test_per_check_results_are_passed_through_scrubbed(self) -> None:
        from henchmen.mastermind.scheme_executor import handlers

        result = GateResult(
            condition="fail",
            message="forge: lint passed; tests failed",
            checks=[
                ci_gate.GateCheckResult(name="lint", condition="pass", message="lint passed"),
                ci_gate.GateCheckResult(name="tests", condition="fail", message="tests failed", output=f"x {TOKEN}"),
            ],
        )
        proc = _gate_proc(1, stdout=f"{GATE_RESULT_MARKER}{result.model_dump_json()}\n".encode())
        with patch.object(handlers.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)) as exec_mock:
            gate = await handlers.run_gate_in_container(
                _local_settings(), "forge", repo="acme/widgets", branch="f", base_branch="main"
            )
        argv = list(exec_mock.await_args.args)
        assert argv[argv.index("ghcr.io/acme/henchmen/operative:1.0.0") + 3] == "forge"
        assert gate["condition"] == "fail"
        assert [(c["name"], c["condition"]) for c in gate["checks"]] == [("lint", "pass"), ("tests", "fail")]
        assert TOKEN not in gate["checks"][1]["output"]

    @pytest.mark.asyncio
    async def test_every_gate_container_runs_under_an_init(self) -> None:
        _, exec_mock = await _check(_local_settings(), _gate_proc(0, stdout=_marker("pass", "ok")))
        argv = list(exec_mock.await_args.args)
        assert "--init" in argv[: argv.index("--entrypoint")]


def test_a_failed_handover_names_the_user_namespace_as_the_likely_cause(tmp_path: Path) -> None:
    with (
        patch.object(ci_gate, "_running_as_root", return_value=True),
        patch.object(ci_gate, "_chown_tree", side_effect=OSError(22, "Invalid argument")),
    ):
        result = ci_gate._prepare_sandbox(str(tmp_path), "lint")
    assert isinstance(result, GateResult) and result.condition == "fail"
    assert "uid 65534 is not mapped in this Docker user namespace" in result.message
