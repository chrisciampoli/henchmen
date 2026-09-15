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
            patch.object(handlers, "get_github_token_async", return_value=""),
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

        async def _never_exits() -> int:
            await asyncio.sleep(10)
            return 0

        proc.wait = _never_exits
        cleanup = AsyncMock()

        with (
            patch.object(handlers.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch.object(handlers, "_gate_timeout_seconds", return_value=0.01),
            patch.object(handlers, "_cleanup_gate_container", cleanup),
        ):
            task = asyncio.ensure_future(
                handlers.run_gate_in_container(
                    _settings(), "tests", repo="a/b", branch="b", base_branch="main", token=TOKEN
                )
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


def _gate_checks(**conditions: str) -> dict[str, Any]:
    """A `run_gate_in_container` result for `ci_gate forge` with one entry per named check."""
    checks = [
        {
            "name": name,
            "condition": condition,
            "message": f"{name} {'passed' if condition == 'pass' else 'failed'}",
            "output": "1 failed, 2 passed" if condition != "pass" else "",
        }
        for name, condition in conditions.items()
    ]
    overall = "pass" if all(c == "pass" for c in conditions.values()) else "fail"
    return {"condition": overall, "message": "forge: ...", "output": "", "checks": checks}


class TestForgeRouting:
    @staticmethod
    def _patches(settings: Settings, pr: MagicMock, gate: AsyncMock, decision: Any = (True, None)) -> Any:
        from contextlib import ExitStack

        from henchmen.forge import server
        from henchmen.forge.ci_runner import CIRunner

        stack = ExitStack()
        stack.enter_context(patch.object(server, "get_settings", return_value=settings))
        stack.enter_context(patch("github.Github", return_value=_github_client(pr)))
        stack.enter_context(patch.object(server, "clone_repo", AsyncMock()))
        stack.enter_context(patch.object(handlers, "run_gate_in_container", gate))
        stack.enter_context(
            patch.object(CIRunner, "run", AsyncMock(side_effect=AssertionError("no host CI on desktop")))
        )
        stack.enter_context(patch.object(CIRunner, "committed_tests_decision", AsyncMock(return_value=decision)))
        stack.enter_context(patch.object(CIRunner, "run_silent_failure_scan", AsyncMock(return_value=_scan())))
        return stack

    @pytest.mark.asyncio
    async def test_desktop_forge_runs_lint_and_tests_in_one_gate_container(self, forge_state: AsyncMock) -> None:
        from henchmen.forge import server

        settings = _settings(lair_default_timeout=1800, forge_ci_timeout_seconds=540)
        pr = MagicMock()
        gate = AsyncMock(return_value=_gate_checks(lint="pass", tests="fail"))
        with self._patches(settings, pr, gate), patch.object(server, "clone_repo", AsyncMock()) as clone:
            await server._run_ci_for_pr("https://github.com/acme/widgets/pull/7", "task-1", "req-1")

        # Exactly one container run per Forge CI, for both checks.
        gate.assert_awaited_once()
        args, kwargs = gate.await_args.args, gate.await_args.kwargs
        assert args == (settings, "forge")
        assert (kwargs["repo"], kwargs["branch"], kwargs["base_branch"]) == ("acme/widgets", "feature-branch", "main")
        assert kwargs.get("extra_args", ()) == ()
        # The desktop budget (gate timeout capped below the broker re-send), never the Pub/Sub budget.
        assert 0 < kwargs["timeout_seconds"] <= server.desktop_ci_budget_seconds(settings)
        assert kwargs["timeout_seconds"] > settings.forge_ci_timeout_seconds
        # Only a no-checkout clone reaches the host, for the text-only silent-failure scan.
        assert clone.await_args.kwargs["no_checkout"] is True
        payload = _published(forge_state)[0]
        assert payload["status"] == "failed"
        assert payload["failed"] == ["tests"]
        comment = pr.create_issue_comment.call_args.args[0]
        assert "tests — failed" in comment and "1 failed, 2 passed" in comment
        assert "lint — passed" in comment

    @pytest.mark.asyncio
    async def test_the_desktop_budget_is_capped_below_the_broker_forward_timeout(self, forge_state: AsyncMock) -> None:
        from henchmen.forge import server
        from henchmen.forge.ci_runner import CIRunner

        settings = _settings(lair_default_timeout=1800, forge_ci_timeout_seconds=540)
        gate = AsyncMock(return_value=_gate_checks(lint="pass", tests="pass"))
        with self._patches(settings, MagicMock(), gate):
            real_init = CIRunner.__init__
            budgets: list[int] = []

            def _recording_init(self: CIRunner, **kwargs: Any) -> None:
                budgets.append(kwargs["total_budget_seconds"])
                real_init(self, **kwargs)

            with patch.object(CIRunner, "__init__", _recording_init):
                await server._run_ci_for_pr("https://github.com/acme/widgets/pull/7", "task-1", "req-1")
        assert budgets == [server.desktop_ci_budget_seconds(settings)]
        assert budgets[0] < 1800

    @pytest.mark.asyncio
    async def test_desktop_forge_passes_only_when_every_check_passed(self, forge_state: AsyncMock) -> None:
        from henchmen.forge import server

        gate = AsyncMock(return_value=_gate_checks(lint="pass", tests="pass"))
        with self._patches(_settings(), MagicMock(), gate):
            await server._run_ci_for_pr("https://github.com/acme/widgets/pull/7", "task-1", "req-1")
        assert _published(forge_state)[0]["status"] == "passed"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "gate_result",
        [
            {"condition": "fail", "message": "forge failed (the gate did not finish within 1680s)", "output": ""},
            {
                "condition": "fail",
                "message": "forge failed (the gate container exited 137 without a result)",
                "output": "",
            },
            # A pass the exit code contradicts: every per-check pass is overruled.
            {**_gate_checks(lint="pass", tests="pass"), "condition": "fail", "message": "forge failed (exited 1)"},
        ],
        ids=["timeout", "no-result", "pass-but-nonzero-exit"],
    )
    async def test_a_gate_without_usable_results_fails_every_check(
        self, forge_state: AsyncMock, gate_result: dict[str, Any]
    ) -> None:
        from henchmen.forge import server

        pr = MagicMock()
        with self._patches(_settings(), pr, AsyncMock(return_value=gate_result)):
            await server._run_ci_for_pr("https://github.com/acme/widgets/pull/7", "task-1", "req-1")
        payload = _published(forge_state)[0]
        assert payload["status"] == "failed"
        assert payload["failed"] == ["lint", "tests"]
        comment = pr.create_issue_comment.call_args.args[0]
        # Each row says which check it is, instead of repeating the same gate message.
        assert f"lint: {gate_result['message']}" in comment
        assert f"tests: {gate_result['message']}" in comment

    @pytest.mark.asyncio
    async def test_a_node_repo_without_a_test_script_stays_incomplete_on_desktop(self, forge_state: AsyncMock) -> None:
        """The host path's "skipped, never a pass" rule survives the move into the gate container."""
        from henchmen.forge import server
        from henchmen.forge.ci_runner import STATUS_SKIPPED, CIRunner

        skipped = CIRunner.check_result("tests", STATUS_SKIPPED, "", "package.json declares no `test` script.")
        pr = MagicMock()
        gate = AsyncMock(return_value=_gate_checks(lint="pass"))
        with self._patches(_settings(), pr, gate, decision=(False, skipped)):
            await server._run_ci_for_pr("https://github.com/acme/widgets/pull/7", "task-1", "req-1")
        gate.assert_awaited_once()
        assert gate.await_args.kwargs["extra_args"] == ("--skip-tests",)
        payload = _published(forge_state)[0]
        assert payload["status"] == "incomplete" and payload["skipped"] == ["tests"]
        assert "INCOMPLETE" in pr.create_issue_comment.call_args.args[0]

    @pytest.mark.asyncio
    async def test_a_repo_with_no_tests_has_no_tests_check_on_desktop(self, forge_state: AsyncMock) -> None:
        from henchmen.forge import server

        gate = AsyncMock(return_value=_gate_checks(lint="pass"))
        with self._patches(_settings(), MagicMock(), gate, decision=(False, None)):
            await server._run_ci_for_pr("https://github.com/acme/widgets/pull/7", "task-1", "req-1")
        assert gate.await_args.kwargs["extra_args"] == ("--skip-tests",)
        assert _published(forge_state)[0]["status"] == "passed"

    @pytest.mark.asyncio
    async def test_a_desktop_gate_runner_error_is_a_ci_error(self, forge_state: AsyncMock) -> None:
        from henchmen.forge import server

        with (
            self._patches(_settings(), MagicMock(), AsyncMock(side_effect=FileNotFoundError("docker"))),
            pytest.raises(server.ForgeCIError),
        ):
            await server._run_ci_for_pr("https://github.com/acme/widgets/pull/7", "task-1", "req-1")
        assert _published(forge_state)[0]["reason"] == "ci-error"

    @pytest.mark.asyncio
    async def test_cloud_forge_keeps_the_host_runner(self, forge_state: AsyncMock) -> None:
        from henchmen.forge import server
        from henchmen.forge.ci_runner import CIRunner

        result = {"passed": True, "failed": [], "skipped": [], "checks": [], "summary": ""}
        settings = _settings(provider="gcp", forge_ci_timeout_seconds=123)
        with (
            patch.object(server, "get_settings", return_value=settings),
            patch("github.Github", return_value=_github_client(MagicMock())),
            patch.object(server, "clone_repo", AsyncMock()) as clone,
            patch.object(handlers, "run_gate_in_container", AsyncMock(side_effect=AssertionError("cloud path"))),
            patch.object(CIRunner, "run", AsyncMock(return_value=result)) as run,
            patch.object(CIRunner, "committed_tests_decision", AsyncMock(side_effect=AssertionError("cloud path"))),
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


_HAS_GIT = __import__("shutil").which("git") is not None


def _commit_tree(root: Any, files: dict[str, str]) -> None:
    import os
    import subprocess

    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(root)}
    subprocess.run(["git", "init", "-q", str(root / "repo")], check=True, env=env)
    for rel, text in files.items():
        path = root / "repo" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(root / "repo"), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root / "repo"), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "c"],
        check=True,
        env=env,
    )


@pytest.mark.skipif(not _HAS_GIT, reason="git is not installed")
class TestCommittedTestsDecision:
    """The desktop tests decision reads only committed objects and matches the host path's rules."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("files", "run", "status"),
        [
            ({"package.json": '{"scripts": {"test": "jest"}}'}, True, None),
            ({"package.json": '{"scripts": {"build": "tsc"}}'}, False, "skipped"),
            ({"pyproject.toml": "", "tests/test_x.py": "def test_x(): pass\n"}, True, None),
            ({"pyproject.toml": "", "src/x.py": "x = 1\n"}, False, None),
        ],
        ids=["node-with-script", "node-without-script", "python-with-tests", "no-tests"],
    )
    async def test_decision(self, tmp_path: Any, files: dict[str, str], run: bool, status: str | None) -> None:
        from henchmen.forge.ci_runner import CIRunner

        _commit_tree(tmp_path, files)
        decided_run, check = await CIRunner(timeout_seconds=30).committed_tests_decision(str(tmp_path / "repo"))
        assert decided_run is run
        assert (check["status"] if check else None) == status

    @pytest.mark.asyncio
    async def test_an_unreadable_tree_fails_closed(self, tmp_path: Any) -> None:
        from henchmen.forge.ci_runner import CIRunner

        (tmp_path / "not-a-repo").mkdir()
        run, check = await CIRunner(timeout_seconds=30).committed_tests_decision(str(tmp_path / "not-a-repo"))
        assert run is False and check is not None and check["status"] == "failed"


class TestDesktopForgeDeadline:
    """A desktop Forge run always ends before the in-memory broker re-sends the request."""

    @pytest.mark.parametrize("lair_timeout", [600, 1700, 1800, 7200])
    def test_the_desktop_budget_is_strictly_below_the_forward_timeout(self, lair_timeout: int) -> None:
        from henchmen.forge import server
        from henchmen.providers.local.memory import FORWARD_TIMEOUT_SECONDS

        budget = server.desktop_ci_budget_seconds(_settings(lair_default_timeout=lair_timeout))
        assert budget < FORWARD_TIMEOUT_SECONDS
        assert budget <= lair_timeout
        assert FORWARD_TIMEOUT_SECONDS - budget >= 120 or budget == lair_timeout

    @pytest.mark.asyncio
    async def test_time_spent_in_the_gate_reduces_the_scan_budget(self) -> None:
        from henchmen.forge import server
        from henchmen.forge.ci_runner import CIRunner

        loop = asyncio.get_running_loop()
        runner = CIRunner(timeout_seconds=1000, total_budget_seconds=1000)
        budgets: list[int] = []

        async def _scan(self: CIRunner, workspace: str, base_ref: str) -> dict[str, Any]:
            budgets.append(self.total_budget_seconds)
            return _scan_check()

        gate = AsyncMock(return_value=_gate_checks(lint="pass", tests="pass"))
        with (
            patch.object(handlers, "run_gate_in_container", gate),
            patch.object(CIRunner, "committed_tests_decision", AsyncMock(return_value=(True, None))),
            patch.object(CIRunner, "run_silent_failure_scan", _scan),
        ):
            # 300s of the budget are already gone: the gate gets what is left, the scan less still.
            result = await server._run_local_ci(
                _settings(), runner, "acme/widgets", "f", "main", "/ws", token=TOKEN, deadline=loop.time() + 700
            )
        assert gate.await_args.kwargs["timeout_seconds"] <= 700
        assert budgets and budgets[0] <= 700
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_no_time_left_for_the_scan_is_incomplete_not_passed(self) -> None:
        from henchmen.forge import server
        from henchmen.forge.ci_runner import CIRunner

        loop = asyncio.get_running_loop()
        runner = CIRunner(timeout_seconds=1000, total_budget_seconds=1000)

        async def _gate_uses_up_the_budget(*args: Any, **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0.3)
            return _gate_checks(lint="pass", tests="pass")

        with (
            patch.object(handlers, "run_gate_in_container", AsyncMock(side_effect=_gate_uses_up_the_budget)),
            patch.object(CIRunner, "committed_tests_decision", AsyncMock(return_value=(True, None))),
            patch.object(CIRunner, "run_silent_failure_scan", AsyncMock(side_effect=AssertionError("no time left"))),
        ):
            result = await server._run_local_ci(
                _settings(), runner, "a/b", "f", "main", "/ws", token=TOKEN, deadline=loop.time() + 0.2
            )
        assert result["passed"] is False and result["incomplete"] is True
        assert result["skipped"] == ["silent_failure_scan"]

    @pytest.mark.asyncio
    async def test_no_time_left_for_the_gate_fails_every_check_without_a_container(self) -> None:
        from henchmen.forge import server
        from henchmen.forge.ci_runner import CIRunner

        loop = asyncio.get_running_loop()
        runner = CIRunner(timeout_seconds=10, total_budget_seconds=10)
        with (
            patch.object(handlers, "run_gate_in_container", AsyncMock(side_effect=AssertionError("no container"))),
            patch.object(CIRunner, "committed_tests_decision", AsyncMock(return_value=(True, None))),
        ):
            result = await server._run_local_ci(
                _settings(), runner, "a/b", "f", "main", "/ws", token=TOKEN, deadline=loop.time() - 1
            )
        assert result["failed"] == ["lint", "tests"]


def _scan_check() -> dict[str, Any]:
    return {"name": "silent_failure_scan", "status": "passed", "passed": True, "output": "", "error": ""}


class TestForgeRequestDedup:
    """A re-sent forge-request (same request id) never runs CI or comments twice on a desktop install."""

    @staticmethod
    def _envelope(request_id: str = "req-42") -> dict[str, Any]:
        import base64

        data = {"pr_url": "https://github.com/acme/widgets/pull/7", "task_id": "t1", "request_id": request_id}
        return {"message": {"data": base64.b64encode(json.dumps(data).encode()).decode(), "messageId": "m-1"}}

    @pytest.fixture
    def store(self) -> Any:
        from henchmen.forge.server import app

        class _Store:
            def __init__(self) -> None:
                self.docs: dict[tuple[str, str], dict[str, Any]] = {}

            async def get(self, collection: str, key: str) -> dict[str, Any] | None:
                return self.docs.get((collection, key))

            async def set(self, collection: str, key: str, data: dict[str, Any]) -> None:
                self.docs[(collection, key)] = data

            async def delete(self, collection: str, key: str) -> None:
                self.docs.pop((collection, key), None)

        fake = _Store()
        app.state.document_store = fake
        app.state.message_broker = AsyncMock()
        yield fake
        for attr in ("document_store", "message_broker"):
            if hasattr(app.state, attr):
                delattr(app.state, attr)

    def _post(self, request_id: str = "req-42") -> Any:
        from fastapi.testclient import TestClient

        from henchmen.forge.server import app

        return TestClient(app, raise_server_exceptions=False).post(
            "/pubsub/forge-request", json=self._envelope(request_id)
        )

    def test_a_duplicate_request_id_does_not_run_ci_twice(self, store: Any) -> None:
        from henchmen.forge import server

        run = AsyncMock()
        with (
            patch.object(server, "get_settings", return_value=_settings()),
            patch.object(server, "verify_pubsub_oidc", AsyncMock()),
            patch.object(server, "_run_ci_for_pr", run),
        ):
            first = self._post()
            second = self._post()
        assert first.status_code == 200 and first.json()["status"] == "accepted"
        assert second.status_code == 200 and second.json()["status"] == "duplicate"
        run.assert_awaited_once()
        assert store.docs[("processed_messages", "forge-request:req-42")]["status"] == "done"

    def test_a_re_send_while_the_first_run_is_still_going_is_a_duplicate(self, store: Any) -> None:
        from datetime import UTC, datetime

        from henchmen.forge import server

        store.docs[("processed_messages", "forge-request:req-42")] = {
            "status": "in_flight",
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        run = AsyncMock()
        with (
            patch.object(server, "get_settings", return_value=_settings()),
            patch.object(server, "verify_pubsub_oidc", AsyncMock()),
            patch.object(server, "_run_ci_for_pr", run),
        ):
            assert self._post().json()["status"] == "duplicate"
        run.assert_not_awaited()

    def test_a_retriable_failure_releases_the_claim(self, store: Any) -> None:
        from henchmen.forge import server

        with (
            patch.object(server, "get_settings", return_value=_settings()),
            patch.object(server, "verify_pubsub_oidc", AsyncMock()),
            patch.object(server, "_run_ci_for_pr", AsyncMock(side_effect=[RuntimeError("boom"), None])) as run,
        ):
            assert self._post().status_code == 500
            assert self._post().json()["status"] == "accepted"
        assert run.await_count == 2

    def test_the_cloud_path_does_not_dedup(self, store: Any) -> None:
        from henchmen.forge import server

        run = AsyncMock()
        with (
            patch.object(server, "get_settings", return_value=_settings(provider="gcp")),
            patch.object(server, "verify_pubsub_oidc", AsyncMock()),
            patch.object(server, "_run_ci_for_pr", run),
        ):
            assert self._post().json()["status"] == "accepted"
            assert self._post().json()["status"] == "accepted"
        assert run.await_count == 2
        assert store.docs == {}


# ---------------------------------------------------------------------------
# GitHub credentials in gate containers (Task 7, ruling PB-2)
# ---------------------------------------------------------------------------

INSTALLATION_TOKEN = "ghs_" + "i" * 36


class _Stream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        data, self._data = self._data, b""
        return data


def _passing_gate_proc(message: str) -> MagicMock:
    from henchmen.mastermind.scheme_executor.ci_gate import GATE_RESULT_MARKER, GateResult

    result = GateResult(condition="pass", message=message)
    proc = MagicMock()
    proc.returncode = 0
    proc.stdin = MagicMock(drain=AsyncMock())
    proc.stdout = _Stream(f"{GATE_RESULT_MARKER}{result.model_dump_json()}\n".encode())
    proc.stderr = _Stream(b"")
    proc.wait = AsyncMock(return_value=0)
    return proc


class TestGateCredentials:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("node", ["lint", "tests", "fix_lint"])
    async def test_desktop_gates_receive_the_installation_token_not_the_pat(self, node: str) -> None:
        settings = _settings(lair_default_timeout=1800)  # github_token is the PAT (ghp_...)
        proc = _passing_gate_proc(f"{node} done with {INSTALLATION_TOKEN}")
        provider = AsyncMock(return_value=INSTALLATION_TOKEN)
        with (
            patch("henchmen.config.settings.get_settings", return_value=settings),
            patch.object(handlers, "get_github_token_async", provider),
            patch.object(handlers.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)) as exec_mock,
        ):
            if node == "fix_lint":
                result = await handlers.handle_fix_lint(_executor(settings), MagicMock(), _task(), Dossier(task_id="t"))
            else:
                result = await handlers._run_ci_check(MagicMock(), _task(), node)  # type: ignore[arg-type]

        stdin = b"".join(call.args[0] for call in proc.stdin.write.call_args_list)
        argv = [str(arg) for arg in exec_mock.await_args.args]
        env = exec_mock.await_args.kwargs["env"]
        assert stdin == f"{INSTALLATION_TOKEN}\n".encode()
        assert b"ghp_" not in stdin
        assert all("ghp_" not in arg and INSTALLATION_TOKEN not in arg for arg in argv)
        assert all(TOKEN not in value and INSTALLATION_TOKEN not in value for value in env.values())
        # Fetched once, repo-scoped, and asked to outlive the gate timeout.
        provider.assert_awaited_once_with("acme/widgets", settings=settings, min_ttl_seconds=1800 + 300)
        assert INSTALLATION_TOKEN not in result["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("node", ["lint", "fix_lint"])
    async def test_desktop_gates_fail_closed_when_github_credentials_fail(self, node: str) -> None:
        from henchmen.utils.github_auth import GitHubAuthError

        settings = _settings()
        with (
            patch("henchmen.config.settings.get_settings", return_value=settings),
            patch.object(handlers, "get_github_token_async", AsyncMock(side_effect=GitHubAuthError("no key"))),
            patch.object(handlers, "run_gate_in_container", AsyncMock(side_effect=AssertionError("no gate"))),
        ):
            if node == "fix_lint":
                result = await handlers.handle_fix_lint(_executor(settings), MagicMock(), _task(), Dossier(task_id="t"))
            else:
                result = await handlers._run_ci_check(MagicMock(), _task(), "lint")
        assert result["condition"] == "fail"
        assert "(GitHub credentials): no key" in result["message"]

    @pytest.mark.asyncio
    async def test_a_partly_configured_app_never_falls_back_to_the_pat(self) -> None:
        settings = _settings(github_app_id="4242")  # installation id and key path missing; github_token is a PAT
        with (
            patch("henchmen.config.settings.get_settings", return_value=settings),
            patch.object(handlers, "run_gate_in_container", AsyncMock(side_effect=AssertionError("no gate"))),
        ):
            result = await handlers._run_ci_check(MagicMock(), _task(), "tests")
        assert result["condition"] == "fail"
        assert "partly configured" in result["message"]
        assert TOKEN not in result["message"]

    def test_the_gate_token_lifetime_covers_the_gate_timeout(self) -> None:
        assert handlers._gate_min_ttl(_settings(lair_default_timeout=1800)) == 2100

    @pytest.mark.asyncio
    async def test_a_gate_timeout_beyond_the_token_cap_is_warned_about(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _settings(lair_default_timeout=3600)
        provider = AsyncMock(return_value=INSTALLATION_TOKEN)
        with (
            patch.object(handlers, "get_github_token_async", provider),
            patch.object(handlers, "get_credentials_provider", return_value=MagicMock(uses_app=True)),
            caplog.at_level("WARNING"),
        ):
            assert await handlers._gate_github_token(settings, "acme/widgets") == INSTALLATION_TOKEN
        assert "may expire before a long gate finishes" in caplog.text
        assert INSTALLATION_TOKEN not in caplog.text
        assert provider.await_args.kwargs["min_ttl_seconds"] == 3900

    @pytest.mark.asyncio
    async def test_a_pat_only_config_gets_no_token_expiry_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = _settings(lair_default_timeout=3600)  # PAT, no GitHub App: the real provider returns it
        with caplog.at_level("WARNING"):
            assert await handlers._gate_github_token(settings, "acme/widgets") == TOKEN
        assert "may expire" not in caplog.text
        assert "installation tokens last an hour" not in caplog.text
        assert TOKEN not in caplog.text

    @pytest.mark.asyncio
    async def test_forge_desktop_token_outlives_the_ci_budget(self, forge_state: AsyncMock) -> None:
        from henchmen.forge import server

        settings = _settings(lair_default_timeout=1800)
        provider = AsyncMock(return_value=INSTALLATION_TOKEN)
        gate = AsyncMock(return_value=_gate_checks(lint="pass", tests="pass"))
        with (
            TestForgeRouting._patches(settings, MagicMock(), gate),
            patch.object(server, "clone_repo", AsyncMock()) as clone,
            patch.object(server, "get_github_token_async", provider),
        ):
            await server._run_ci_for_pr("https://github.com/acme/widgets/pull/7", "task-1", "req-1")

        budget = server.desktop_ci_budget_seconds(settings)
        first = provider.await_args_list[0]
        assert first.args == ("acme/widgets",)
        assert first.kwargs["settings"] is settings
        assert first.kwargs["min_ttl_seconds"] >= budget
        assert first.kwargs["min_ttl_seconds"] == budget + server._DESKTOP_FORWARD_HEADROOM_SECONDS
        # The same token clones on the host and reaches the gate (over stdin, by the runner).
        assert clone.await_args.kwargs["token"] == INSTALLATION_TOKEN
        assert gate.await_args.kwargs["token"] == INSTALLATION_TOKEN
        assert _published(forge_state)[0]["status"] == "passed"
