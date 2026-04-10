"""Tests for scoped lint/test in monorepos and dev mode fix node behavior."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# test_runner: _get_affected_packages
# ---------------------------------------------------------------------------


class TestGetAffectedPackages:
    """Test _get_affected_packages correctly maps git diffs to package filters."""

    @pytest.fixture
    def workspace(self, tmp_path: Any) -> str:
        """Create a fake monorepo workspace."""
        (tmp_path / "pnpm-lock.yaml").touch()
        (tmp_path / "turbo.json").touch()
        (tmp_path / "package.json").write_text("{}")
        return str(tmp_path)

    @pytest.mark.asyncio
    async def test_maps_changed_files_to_packages(self, workspace: str) -> None:
        from henchmen.arsenal.tools.test_runner import _get_affected_packages

        diff_output = b"apps/api/src/modules/foo.ts\napps/api/src/modules/bar.ts\n"

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (diff_output, b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await _get_affected_packages(workspace)

        assert result == ["./apps/api"]

    @pytest.mark.asyncio
    async def test_multiple_packages(self, workspace: str) -> None:
        from henchmen.arsenal.tools.test_runner import _get_affected_packages

        diff_output = b"apps/api/src/foo.ts\npackages/shared/src/bar.ts\n"

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (diff_output, b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await _get_affected_packages(workspace)

        assert result == ["./apps/api", "./packages/shared"]

    @pytest.mark.asyncio
    async def test_root_file_returns_empty(self, workspace: str) -> None:
        from henchmen.arsenal.tools.test_runner import _get_affected_packages

        diff_output = b"apps/api/src/foo.ts\n.eslintrc.js\n"

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (diff_output, b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await _get_affected_packages(workspace)

        # Root file means we can't scope — return empty
        assert result == []

    @pytest.mark.asyncio
    async def test_git_diff_fails_returns_empty(self, workspace: str) -> None:
        from henchmen.arsenal.tools.test_runner import _get_affected_packages

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"fatal: not a git repo")
            mock_proc.returncode = 128
            mock_exec.return_value = mock_proc

            result = await _get_affected_packages(workspace)

        assert result == []

    @pytest.mark.asyncio
    async def test_no_changed_files_returns_empty(self, workspace: str) -> None:
        from henchmen.arsenal.tools.test_runner import _get_affected_packages

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await _get_affected_packages(workspace)

        assert result == []


# ---------------------------------------------------------------------------
# test_runner: _is_monorepo
# ---------------------------------------------------------------------------


class TestIsMonorepo:
    def test_true_with_both_files(self, tmp_path: Any) -> None:
        from henchmen.arsenal.tools.test_runner import _is_monorepo

        (tmp_path / "pnpm-lock.yaml").touch()
        (tmp_path / "turbo.json").touch()
        assert _is_monorepo(str(tmp_path)) is True

    def test_false_without_turbo(self, tmp_path: Any) -> None:
        from henchmen.arsenal.tools.test_runner import _is_monorepo

        (tmp_path / "pnpm-lock.yaml").touch()
        assert _is_monorepo(str(tmp_path)) is False

    def test_false_without_pnpm(self, tmp_path: Any) -> None:
        from henchmen.arsenal.tools.test_runner import _is_monorepo

        (tmp_path / "turbo.json").touch()
        assert _is_monorepo(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# test_runner: run_lint scoping
# ---------------------------------------------------------------------------


class TestRunLintScoping:
    """Verify run_lint uses --filter in monorepos."""

    @pytest.mark.asyncio
    async def test_monorepo_lint_uses_filter(self, tmp_path: Any) -> None:
        """In a monorepo with affected packages, lint should use pnpm turbo --filter."""
        wd = str(tmp_path)
        (tmp_path / "pnpm-lock.yaml").touch()
        (tmp_path / "turbo.json").touch()
        (tmp_path / "package.json").write_text("{}")

        from henchmen.arsenal.tools.test_runner import run_lint

        lint_result = {"stdout": "", "stderr": "", "return_code": 0, "success": True}

        with (
            patch(
                "henchmen.arsenal.tools.test_runner._get_affected_packages",
                return_value=["./apps/api"],
            ),
            patch(
                "henchmen.arsenal.tools.test_runner._run_subprocess",
                return_value=lint_result,
            ) as mock_run,
        ):
            result = await run_lint(working_dir=wd)

        # Verify the command used --filter
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0]
        assert "turbo" in cmd_args
        assert "--filter" in cmd_args
        assert "./apps/api" in cmd_args
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_monorepo_no_affected_falls_back(self, tmp_path: Any) -> None:
        """If can't determine affected packages, falls back to full lint."""
        wd = str(tmp_path)
        (tmp_path / "pnpm-lock.yaml").touch()
        (tmp_path / "turbo.json").touch()
        (tmp_path / "package.json").write_text("{}")

        from henchmen.arsenal.tools.test_runner import run_lint

        lint_result = {"stdout": "", "stderr": "", "return_code": 0, "success": True}

        with (
            patch(
                "henchmen.arsenal.tools.test_runner._get_affected_packages",
                return_value=[],
            ),
            patch(
                "henchmen.arsenal.tools.test_runner._run_subprocess",
                return_value=lint_result,
            ) as mock_run,
        ):
            await run_lint(working_dir=wd)

        # Should fall back to pnpm run lint (no --filter)
        cmd_args = mock_run.call_args[0]
        assert cmd_args == ("pnpm", "run", "lint")

    @pytest.mark.asyncio
    async def test_non_monorepo_pnpm_unchanged(self, tmp_path: Any) -> None:
        """Non-monorepo pnpm project should use pnpm run lint (no turbo)."""
        wd = str(tmp_path)
        (tmp_path / "pnpm-lock.yaml").touch()
        (tmp_path / "package.json").write_text("{}")
        # No turbo.json — not a monorepo

        from henchmen.arsenal.tools.test_runner import run_lint

        lint_result = {"stdout": "", "stderr": "", "return_code": 0, "success": True}

        with patch(
            "henchmen.arsenal.tools.test_runner._run_subprocess",
            return_value=lint_result,
        ) as mock_run:
            await run_lint(working_dir=wd)

        cmd_args = mock_run.call_args[0]
        assert cmd_args == ("pnpm", "run", "lint")


# ---------------------------------------------------------------------------
# test_runner: run_tests scoping
# ---------------------------------------------------------------------------


class TestRunTestsScoping:
    """Verify run_tests uses --filter in monorepos."""

    @pytest.mark.asyncio
    async def test_monorepo_tests_uses_filter(self, tmp_path: Any) -> None:
        wd = str(tmp_path)
        (tmp_path / "pnpm-lock.yaml").touch()
        (tmp_path / "turbo.json").touch()
        (tmp_path / "package.json").write_text("{}")

        from henchmen.arsenal.tools.test_runner import run_tests

        test_result = {"stdout": "", "stderr": "", "return_code": 0, "success": True}

        with (
            patch(
                "henchmen.arsenal.tools.test_runner._get_affected_packages",
                return_value=["./apps/api"],
            ),
            patch(
                "henchmen.arsenal.tools.test_runner._run_subprocess",
                return_value=test_result,
            ) as mock_run,
        ):
            await run_tests(working_dir=wd)

        cmd_args = mock_run.call_args[0]
        assert "turbo" in cmd_args
        assert "--filter" in cmd_args
        assert "./apps/api" in cmd_args


# ---------------------------------------------------------------------------
# executor: dev mode simulated pass blocked for fix nodes
# ---------------------------------------------------------------------------


class TestDevModeFixNodeBehavior:
    """Verify fix_lint/fix_tests never get simulated pass in dev mode."""

    def _make_executor(self) -> Any:
        from henchmen.mastermind.scheme_executor.executor import SchemeExecutor

        mock_graph = MagicMock()
        mock_lair = MagicMock()
        mock_settings = MagicMock()
        mock_settings.environment = MagicMock()
        mock_settings.environment.value = "dev"
        return SchemeExecutor(mock_graph, mock_lair, mock_settings)

    def _make_node(self, node_id: str) -> Any:
        from henchmen.models.scheme import NodeType, SchemeNode

        return SchemeNode(
            id=node_id,
            name=node_id,
            node_type=NodeType.AGENTIC,
            instruction_template="fix it",
            max_steps=5,
            timeout_seconds=300,
        )

    def _make_task(self) -> Any:
        from henchmen.models.task import HenchmenTask, TaskContext, TaskSource

        return HenchmenTask(
            id="test-task-id",
            title="Test",
            description="Test task",
            source=TaskSource.CLI,
            source_id="test-source-id",
            created_by="test-user",
            context=TaskContext(repo="test/repo"),
        )

    def _make_dossier(self) -> Any:
        from henchmen.models.dossier import Dossier

        return Dossier(task_id="test-task-id")

    @pytest.mark.asyncio
    async def test_fix_lint_fails_in_dev_mode(self) -> None:
        """fix_lint should fail-closed even in dev mode when lair fails."""
        executor = self._make_executor()
        node = self._make_node("fix_lint")
        task = self._make_task()
        dossier = self._make_dossier()

        # Make lair provisioning fail
        executor.lair_manager.create_lair = AsyncMock(side_effect=Exception("503 Connection reset"))

        result = await executor._execute_agentic(node, task, dossier)

        assert result["condition"] == "fail"
        assert "dev_mode" not in result

    @pytest.mark.asyncio
    async def test_fix_tests_fails_in_dev_mode(self) -> None:
        """fix_tests should fail-closed even in dev mode when lair fails."""
        executor = self._make_executor()
        node = self._make_node("fix_tests")
        task = self._make_task()
        dossier = self._make_dossier()

        executor.lair_manager.create_lair = AsyncMock(side_effect=Exception("503 Connection reset"))

        result = await executor._execute_agentic(node, task, dossier)

        assert result["condition"] == "fail"
        assert "dev_mode" not in result

    @pytest.mark.asyncio
    async def test_implement_feature_simulates_in_dev_mode(self) -> None:
        """Non-fix nodes should still get simulated pass in dev mode."""
        executor = self._make_executor()
        node = self._make_node("implement_feature")
        task = self._make_task()
        dossier = self._make_dossier()

        executor.lair_manager.create_lair = AsyncMock(side_effect=Exception("503 Connection reset"))

        result = await executor._execute_agentic(node, task, dossier)

        assert result["condition"] == "pass"
        assert result.get("dev_mode") is True
