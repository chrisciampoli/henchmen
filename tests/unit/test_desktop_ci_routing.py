"""Operative-written code never runs natively in the desktop server process (decision C18).

When the effective container orchestrator is local, Forge CI's lint and tests
and the ``fix_lint`` node run through the single gate-container runner
(``handlers.run_gate_in_container``); the cloud path is untouched.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.config.settings import Settings
from henchmen.mastermind.scheme_executor import handlers
from henchmen.models.dossier import Dossier
from henchmen.providers.registry import orchestrator_is_local

TOKEN = "ghp_" + "r" * 36


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"provider": "local", "github_token": TOKEN, "gcp_project_id": "test-project"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _task() -> MagicMock:
    task = MagicMock()
    task.id = "task-1"
    task.context.repo = "acme/widgets"
    task.context.branch = "develop"
    task.branch_name = "henchmen/task-1"
    return task


class TestOrchestratorIsLocal:
    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"provider": "local"}, True),
            ({"provider": "gcp"}, False),
            ({"provider": "gcp", "container_orchestrator_provider": "local"}, True),
            ({"provider": "local", "container_orchestrator_provider": "gcp"}, False),
        ],
    )
    def test_follows_the_effective_container_orchestrator(self, overrides: dict[str, Any], expected: bool) -> None:
        assert orchestrator_is_local(_settings(**overrides)) is expected

    def test_lair_image_follows_the_same_predicate(self) -> None:
        from henchmen.mastermind.lair_manager import LairManager

        docker_on_gcp = _settings(provider="gcp", container_orchestrator_provider="local")
        assert LairManager(docker_on_gcp)._build_image() == "henchmen-operative:local"
        cloud_run = _settings(provider="gcp", gcp_region="us-central1")
        assert LairManager(cloud_run)._build_image().startswith("us-central1-docker.pkg.dev/test-project/")


# ---------------------------------------------------------------------------
# fix_lint
# ---------------------------------------------------------------------------


def _executor(settings: Settings) -> MagicMock:
    executor = MagicMock()
    executor.settings = settings
    return executor


class TestFixLintRouting:
    @pytest.mark.asyncio
    async def test_desktop_fix_lint_runs_in_the_gate_container(self) -> None:
        settings = _settings(git_author_name="Ann Dev", git_author_email="ann@example.com")
        gate = AsyncMock(return_value={"condition": "pass", "message": "fix_lint: auto-fixed", "output": "1 fixed"})
        with (
            patch.object(handlers, "run_gate_in_container", gate),
            patch.object(handlers, "clone_repo", AsyncMock(side_effect=AssertionError("no host clone on desktop"))),
            patch.object(handlers.tempfile, "mkdtemp", side_effect=AssertionError("no host workspace on desktop")),
        ):
            result = await handlers.handle_fix_lint(_executor(settings), MagicMock(), _task(), Dossier(task_id="t"))

        assert result == {"condition": None, "message": "fix_lint: auto-fixed", "output": "1 fixed"}
        args, kwargs = gate.await_args.args, gate.await_args.kwargs
        assert args == (settings, "fix")
        assert kwargs["repo"] == "acme/widgets"
        assert (kwargs["branch"], kwargs["base_branch"]) == ("henchmen/task-1", "develop")
        assert kwargs["extra_args"] == ("--author-name=Ann Dev", "--author-email=ann@example.com")

    @pytest.mark.asyncio
    async def test_a_failed_desktop_fix_fails_the_node(self) -> None:
        failed = {"condition": "fail", "message": "fix_lint failed (git push failed): denied", "output": ""}
        with patch.object(handlers, "run_gate_in_container", AsyncMock(return_value=failed)):
            result = await handlers.handle_fix_lint(_executor(_settings()), MagicMock(), _task(), Dossier(task_id="t"))
        assert result == failed

    @pytest.mark.asyncio
    async def test_a_desktop_runner_error_fails_closed_without_the_token(self) -> None:
        boom = AsyncMock(side_effect=FileNotFoundError(f"docker not found {TOKEN}"))
        with patch.object(handlers, "run_gate_in_container", boom):
            result = await handlers.handle_fix_lint(_executor(_settings()), MagicMock(), _task(), Dossier(task_id="t"))
        assert result["condition"] == "fail"
        assert "fix_lint failed (error:" in result["message"] and TOKEN not in result["message"]

    @pytest.mark.asyncio
    async def test_cloud_fix_lint_never_uses_the_gate_container(self) -> None:
        settings = _settings(provider="gcp")
        with (
            patch.object(handlers, "run_gate_in_container", AsyncMock(side_effect=AssertionError("cloud path"))),
            patch.object(handlers, "get_github_token", return_value=""),
            patch.object(handlers, "clone_repo", AsyncMock(side_effect=RuntimeError("git clone failed: x"))) as clone,
        ):
            result = await handlers.handle_fix_lint(_executor(settings), MagicMock(), _task(), Dossier(task_id="t"))
        assert result == {"condition": "fail", "message": "fix_lint failed (clone failed): git clone failed: x"}
        clone.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cloud_ci_checks_never_use_the_gate_container(self) -> None:
        settings = _settings(provider="gcp")
        with (
            patch("henchmen.config.settings.get_settings", return_value=settings),
            patch.object(handlers, "run_gate_in_container", AsyncMock(side_effect=AssertionError("cloud path"))),
            patch.object(
                handlers,
                "plan_gate",
                AsyncMock(return_value=handlers.GateResult(condition="fail", message="lint failed (clone failed)")),
            ) as plan,
        ):
            result = await handlers._run_ci_check(MagicMock(), _task(), "lint")
        assert result["condition"] == "fail"
        plan.assert_awaited_once()


class TestCleanupCancellation:
    @pytest.mark.asyncio
    async def test_a_cancel_arriving_while_readers_wind_down_still_cleans_up_then_propagates(self) -> None:
        """D9: the cancelling() count, not task.cancelled(), tells a new cancellation from our own."""
        reader_cancelled = asyncio.Event()

        class _StubbornStream:
            async def read(self, n: int = -1) -> bytes:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    reader_cancelled.set()
                    await asyncio.sleep(10)  # winds down slowly after being cancelled
                return b""

        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock(drain=AsyncMock())
        proc.stdout = _StubbornStream()
        proc.stderr = _StubbornStream()
        proc.wait = AsyncMock(side_effect=lambda: asyncio.sleep(10))
        cleanup = AsyncMock()

        with (
            patch.object(handlers.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch.object(handlers, "_gate_timeout_seconds", return_value=0.01),
            patch.object(handlers, "_cleanup_gate_container", cleanup),
        ):
            task = asyncio.ensure_future(
                handlers.run_gate_in_container(_settings(), "tests", repo="a/b", branch="b", base_branch="main")
            )
            await asyncio.wait_for(reader_cancelled.wait(), timeout=2.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2.0)
        cleanup.assert_awaited_once()


# ---------------------------------------------------------------------------
# Forge CI
# ---------------------------------------------------------------------------


def _github_client(pr: MagicMock) -> MagicMock:
    pr.head.ref = "feature-branch"
    pr.base.ref = "main"
    repo = MagicMock()
    repo.get_pull.return_value = pr
    client = MagicMock()
    client.get_repo.return_value = repo
    return client


@pytest.fixture
def forge_state() -> Any:
    from henchmen.forge.server import app

    broker = AsyncMock()
    broker.publish = AsyncMock(return_value="id")
    app.state.message_broker = broker
    yield broker
    if hasattr(app.state, "message_broker"):
        delattr(app.state, "message_broker")


def _published(broker: AsyncMock) -> list[dict[str, Any]]:
    return [json.loads(call.args[1].decode("utf-8")) for call in broker.publish.call_args_list]


def _scan(status: str = "passed") -> dict[str, Any]:
    return {"name": "silent_failure_scan", "status": status, "passed": status == "passed", "output": "", "error": ""}


class TestForgeRouting:
    @pytest.mark.asyncio
    async def test_desktop_forge_runs_lint_and_tests_in_the_gate_container(self, forge_state: AsyncMock) -> None:
        from henchmen.forge import server
        from henchmen.forge.ci_runner import CIRunner

        settings = _settings()
        pr = MagicMock()
        gates = {
            "lint": {"condition": "pass", "message": "lint passed", "output": ""},
            "tests": {"condition": "fail", "message": "tests failed", "output": "1 failed, 2 passed"},
        }
        gate = AsyncMock(side_effect=lambda settings, check, **kwargs: gates[check])
        with (
            patch.object(server, "get_settings", return_value=settings),
            patch("github.Github", return_value=_github_client(pr)),
            patch.object(server, "clone_repo", AsyncMock()) as clone,
            patch.object(handlers, "run_gate_in_container", gate),
            patch.object(CIRunner, "run", AsyncMock(side_effect=AssertionError("no host CI on desktop"))),
            patch.object(CIRunner, "run_silent_failure_scan", AsyncMock(return_value=_scan())) as scan,
        ):
            await server._run_ci_for_pr("https://github.com/acme/widgets/pull/7", "task-1", "req-1")

        # Only a no-checkout clone reaches the host, for the text-only silent-failure scan.
        assert clone.await_args.kwargs["no_checkout"] is True
        assert scan.await_args.args[1] == "main"
        assert [call.args[1] for call in gate.await_args_list] == ["lint", "tests"]
        for call in gate.await_args_list:
            assert (call.kwargs["repo"], call.kwargs["branch"], call.kwargs["base_branch"]) == (
                "acme/widgets",
                "feature-branch",
                "main",
            )
            assert 0 < call.kwargs["timeout_seconds"] <= settings.forge_ci_timeout_seconds
        payload = _published(forge_state)[0]
        assert payload["status"] == "failed"
        assert payload["failed"] == ["tests"]
        comment = pr.create_issue_comment.call_args.args[0]
        assert "tests — failed" in comment and "1 failed, 2 passed" in comment

    @pytest.mark.asyncio
    async def test_desktop_forge_passes_only_when_every_check_passed(self, forge_state: AsyncMock) -> None:
        from henchmen.forge import server
        from henchmen.forge.ci_runner import CIRunner

        ok = {"condition": "pass", "message": "ok", "output": ""}
        with (
            patch.object(server, "get_settings", return_value=_settings()),
            patch("github.Github", return_value=_github_client(MagicMock())),
            patch.object(server, "clone_repo", AsyncMock()),
            patch.object(handlers, "run_gate_in_container", AsyncMock(return_value=ok)),
            patch.object(CIRunner, "run_silent_failure_scan", AsyncMock(return_value=_scan())),
        ):
            await server._run_ci_for_pr("https://github.com/acme/widgets/pull/7", "task-1", "req-1")
        assert _published(forge_state)[0]["status"] == "passed"

    @pytest.mark.asyncio
    async def test_a_desktop_gate_runner_error_is_a_ci_error(self, forge_state: AsyncMock) -> None:
        from henchmen.forge import server

        with (
            patch.object(server, "get_settings", return_value=_settings()),
            patch("github.Github", return_value=_github_client(MagicMock())),
            patch.object(server, "clone_repo", AsyncMock()),
            patch.object(handlers, "run_gate_in_container", AsyncMock(side_effect=FileNotFoundError("docker"))),
            pytest.raises(server.ForgeCIError),
        ):
            await server._run_ci_for_pr("https://github.com/acme/widgets/pull/7", "task-1", "req-1")
        assert _published(forge_state)[0]["reason"] == "ci-error"

    @pytest.mark.asyncio
    async def test_cloud_forge_keeps_the_host_runner(self, forge_state: AsyncMock) -> None:
        from henchmen.forge import server
        from henchmen.forge.ci_runner import CIRunner

        result = {"passed": True, "failed": [], "skipped": [], "checks": [], "summary": ""}
        with (
            patch.object(server, "get_settings", return_value=_settings(provider="gcp")),
            patch("github.Github", return_value=_github_client(MagicMock())),
            patch.object(server, "clone_repo", AsyncMock()) as clone,
            patch.object(handlers, "run_gate_in_container", AsyncMock(side_effect=AssertionError("cloud path"))),
            patch.object(CIRunner, "run", AsyncMock(return_value=result)) as run,
        ):
            await server._run_ci_for_pr("https://github.com/acme/widgets/pull/7", "task-1", "req-1")
        assert clone.await_args.kwargs["no_checkout"] is False
        run.assert_awaited_once()
        assert _published(forge_state)[0]["status"] == "passed"


class TestSilentFailureScanOnly:
    @pytest.mark.asyncio
    async def test_scan_only_uses_git_plumbing_and_never_runs_repo_code(self, tmp_path: Any) -> None:
        from henchmen.forge.ci_runner import CIRunner

        diff = "diff --git a/x.py b/x.py\n+++ b/x.py\n+try:\n+    pass\n+except Exception:\n+    pass\n"
        commands: list[list[str]] = []

        async def _run(cmd: list[str], cwd: str, *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
            commands.append(cmd)
            if cmd[:2] == ["git", "merge-base"]:
                return 0, "abc123\n", ""
            if cmd[:2] == ["git", "diff"]:
                return 0, diff, ""
            return 0, "", ""

        runner = CIRunner(timeout_seconds=30, total_budget_seconds=30)
        with patch.object(runner, "_run_command", AsyncMock(side_effect=_run)):
            check = await runner.run_silent_failure_scan(str(tmp_path), "main")
        assert check["name"] == "silent_failure_scan"
        assert {cmd[0] for cmd in commands} == {"git"}
        assert [cmd[1] for cmd in commands] == ["fetch", "merge-base", "diff"]
