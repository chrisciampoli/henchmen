"""Unit tests for :class:`henchmen.mastermind.lair_manager.LairManager`.

Covers the container-job contract: what environment the operative receives,
how job ids are derived, which service account is used, and the fail-closed
behaviour of ``wait_for_completion`` when no report ever arrives.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from henchmen.config.settings import Environment, Settings, get_settings
from henchmen.mastermind.lair_manager import LairManager
from henchmen.models.dossier import Dossier
from henchmen.models.llm import ModelTier
from henchmen.models.operative import OperativeReport, OperativeStatus
from henchmen.models.scheme import NodeType, SchemeNode
from henchmen.models.task import HenchmenTask, TaskContext, TaskSource
from henchmen.providers.interfaces.container_orchestrator import JobResult, JobStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    import os

    os.environ.setdefault("HENCHMEN_GCP_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    update: dict = {"provider": "gcp", "environment": Environment.DEV}
    update.update(overrides)
    return get_settings().model_copy(update=update)


def _task(**overrides) -> HenchmenTask:
    defaults = {
        "id": "task-abcdef01-2345",
        "source": TaskSource.SLACK,
        "source_id": "C123/1700000000.1",
        "title": "Fix login crash",
        "description": "Users report a crash when logging in",
        "context": TaskContext(repo="acme/webapp", branch="main"),
        "created_by": "user@test.com",
    }
    defaults.update(overrides)
    return HenchmenTask(**defaults)


def _node(node_id: str = "implement_fix", **kwargs) -> SchemeNode:
    kwargs.setdefault("timeout_seconds", 600)
    return SchemeNode(id=node_id, name=node_id, node_type=NodeType.AGENTIC, **kwargs)


def _store() -> MagicMock:
    store = MagicMock()
    store.get = AsyncMock(return_value=None)
    store.set = AsyncMock()
    store.update = AsyncMock()
    store.delete = AsyncMock()
    return store


def _orchestrator(status: JobStatus = JobStatus.RUNNING) -> MagicMock:
    orch = MagicMock()
    orch.run_job = AsyncMock(return_value="exec-123")
    orch.get_status = AsyncMock(return_value=JobResult(job_id="job", status=status))
    orch.cancel = AsyncMock()
    return orch


def _report(node_id: str = "implement_fix", **overrides) -> OperativeReport:
    now = datetime.now(UTC)
    defaults = {
        "task_id": "task-abcdef01-2345",
        "scheme_id": "bugfix_standard",
        "node_id": node_id,
        "operative_id": "lair-x",
        "status": OperativeStatus.COMPLETED,
        "summary": "done",
        "confidence_score": 0.9,
        "started_at": now,
        "completed_at": now,
    }
    defaults.update(overrides)
    return OperativeReport(**defaults)


# ---------------------------------------------------------------------------
# _build_env_vars
# ---------------------------------------------------------------------------


class TestBuildEnvVars:
    def test_forwards_operator_settings_to_the_container(self):
        """Operator overrides must reach the operative, not silently default inside Docker."""
        settings = _settings(
            operative_max_output_tokens=4321,
            operative_task_cost_ceiling_usd=9.5,
            anthropic_model_complex="claude-opus-5",
        )
        env = LairManager(settings)._build_env_vars(_task(), _node(), "lair-1")

        assert env["HENCHMEN_OPERATIVE_MAX_OUTPUT_TOKENS"] == "4321"
        assert env["HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD"] == "9.5"
        assert env["HENCHMEN_ANTHROPIC_MODEL_COMPLEX"] == "claude-opus-5"
        assert env["HENCHMEN_GCP_PROJECT_ID"] == "test-project"

    def test_secrets_are_withheld_in_cloud_mode(self):
        settings = _settings(provider="gcp", github_token="ghp_" + "s" * 36)
        env = LairManager(settings)._build_env_vars(_task(), _node(), "lair-1")
        assert "HENCHMEN_GITHUB_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env

    def test_local_mode_passes_token_and_forward_url(self):
        settings = _settings(
            provider="local",
            gcp_project_id="",
            github_token="ghp_" + "s" * 36,
            llm_provider="local",
            llm_ollama_base_url="http://localhost:11434",
            local_serve_port=8123,
        )
        env = LairManager(settings)._build_env_vars(_task(), _node(), "lair-1")

        assert env["GITHUB_TOKEN"] == "ghp_" + "s" * 36
        assert env["HENCHMEN_GITHUB_TOKEN"] == "ghp_" + "s" * 36
        assert env["HENCHMEN_LOCAL_FORWARD_BASE_URL"] == "http://host.docker.internal:8123"
        # localhost inside the container is the container itself.
        assert env["HENCHMEN_LLM_OLLAMA_BASE_URL"] == "http://host.docker.internal:11434"

    def test_model_name_defaults_to_the_complex_tier(self):
        env = LairManager(_settings())._build_env_vars(_task(), _node(), "lair-1")
        assert env["MODEL_NAME"] == ModelTier.COMPLEX.value

    def test_task_description_is_not_truncated_to_500_chars(self):
        """The fix nodes depend on the lint/test output appended to the description."""
        description = "x" * 400 + "\n--- RUN_TESTS OUTPUT (FIX THESE ERRORS) ---\nAssertionError: boom"
        env = LairManager(_settings())._build_env_vars(_task(description=description), _node("fix_tests"), "lair-1")
        assert "FIX THESE ERRORS" in env["TASK_DESCRIPTION"]
        assert "AssertionError: boom" in env["TASK_DESCRIPTION"]

    def test_task_description_is_capped(self):
        env = LairManager(_settings())._build_env_vars(_task(description="y" * 40_000), _node(), "lair-1")
        assert len(env["TASK_DESCRIPTION"]) == 16_000

    def test_fix_nodes_clone_the_feature_branch(self):
        task = _task()
        lm = LairManager(_settings())
        assert lm._build_env_vars(task, _node("fix_tests"), "l")["BRANCH"] == task.branch_name
        assert lm._build_env_vars(task, _node("implement_fix"), "l")["BRANCH"] == "main"

    def test_dossier_uri_is_set_only_when_serialized(self):
        lm = LairManager(_settings())
        task = _task()
        assert "DOSSIER_URI" not in lm._build_env_vars(task, _node(), "l", dossier=Dossier(task_id=task.id))

        dossier = Dossier(task_id=task.id, artifact_uri="gs://bucket/dossiers/x.json")
        env = lm._build_env_vars(task, _node(), "l", dossier=dossier)
        assert env["DOSSIER_URI"] == "gs://bucket/dossiers/x.json"


# ---------------------------------------------------------------------------
# create_lair
# ---------------------------------------------------------------------------


class TestCreateLair:
    @pytest.mark.asyncio
    async def test_job_id_is_unique_per_execution(self):
        """Re-running the same task/node must not collide with the previous job resource."""
        lm = LairManager(_settings(), container_orchestrator=_orchestrator(), document_store=_store())
        task, node = _task(), _node()

        first = await lm.create_lair(task, node)
        second = await lm.create_lair(task, node)

        assert first != second
        assert first.startswith("lair-task-abc-implement-fix-")
        assert len(second) <= 63

    @pytest.mark.asyncio
    async def test_service_account_follows_the_environment(self):
        orch = _orchestrator()
        lm = LairManager(
            _settings(environment=Environment.STAGING), container_orchestrator=orch, document_store=_store()
        )

        await lm.create_lair(_task(), _node())

        assert orch.run_job.call_args.kwargs["service_account"] == (
            "sa-staging-operative@test-project.iam.gserviceaccount.com"
        )

    @pytest.mark.asyncio
    async def test_service_account_override_is_honoured(self):
        orch = _orchestrator()
        settings = _settings(lair_service_account="custom@example.iam.gserviceaccount.com")
        lm = LairManager(settings, container_orchestrator=orch, document_store=_store())

        await lm.create_lair(_task(), _node())

        assert orch.run_job.call_args.kwargs["service_account"] == "custom@example.iam.gserviceaccount.com"

    @pytest.mark.asyncio
    async def test_local_mode_uses_no_service_account_or_secrets(self):
        orch = _orchestrator()
        lm = LairManager(
            _settings(provider="local", gcp_project_id=""), container_orchestrator=orch, document_store=_store()
        )

        await lm.create_lair(_task(), _node())

        assert orch.run_job.call_args.kwargs["service_account"] is None
        assert orch.run_job.call_args.kwargs["secrets"] is None

    @pytest.mark.asyncio
    async def test_resources_come_from_settings(self):
        orch = _orchestrator()
        settings = _settings(lair_default_cpu="2", lair_default_memory="4Gi")
        lm = LairManager(settings, container_orchestrator=orch, document_store=_store())

        await lm.create_lair(_task(), _node(timeout_seconds=1800))

        assert orch.run_job.call_args.kwargs["cpu"] == "2"
        assert orch.run_job.call_args.kwargs["memory"] == "4Gi"

    @pytest.mark.asyncio
    async def test_stale_report_is_deleted_before_launch(self):
        store = _store()
        lm = LairManager(_settings(), container_orchestrator=_orchestrator(), document_store=store)
        task, node = _task(), _node()

        await lm.create_lair(task, node)

        store.delete.assert_awaited_with("operative_reports", f"{task.id}:{node.id}")


# ---------------------------------------------------------------------------
# wait_for_completion
# ---------------------------------------------------------------------------


class TestWaitForCompletion:
    @pytest.fixture(autouse=True)
    def _no_report_grace(self, monkeypatch):
        """Skip the post-finish Pub/Sub grace period so fallback paths run instantly."""
        monkeypatch.setattr("henchmen.mastermind.lair_manager._REPORT_GRACE_SECONDS", 0)

    @pytest.mark.asyncio
    async def test_stale_stored_report_is_not_consumed(self):
        """A report from a previous execution must not satisfy this wait."""
        store = _store()
        lm = LairManager(_settings(), container_orchestrator=_orchestrator(), document_store=store)
        task, node = _task(), _node()
        lair_id = await lm.create_lair(task, node)

        stale = _report(completed_at=datetime.now(UTC) - timedelta(hours=1))
        stale.started_at = datetime.now(UTC) - timedelta(hours=2)
        store.get = AsyncMock(return_value=stale.model_dump(mode="json"))

        report = await lm.wait_for_completion(lair_id, poll_interval=0, timeout_seconds=0)

        assert report.status is not OperativeStatus.COMPLETED
        assert report.confidence_score == 0.0

    @pytest.mark.asyncio
    async def test_fresh_stored_report_is_returned_and_consumed(self):
        store = _store()
        lm = LairManager(_settings(), container_orchestrator=_orchestrator(), document_store=store)
        task, node = _task(), _node()
        lair_id = await lm.create_lair(task, node)

        store.get = AsyncMock(return_value=_report().model_dump(mode="json"))
        report = await lm.wait_for_completion(lair_id, poll_interval=0)

        assert report.status is OperativeStatus.COMPLETED
        # Consumed so a later execution of the same node cannot pick it up.
        assert store.delete.await_count >= 2

    @pytest.mark.asyncio
    async def test_missing_report_never_fabricates_success(self):
        """A finished job with no report is not evidence the work was done."""
        orch = _orchestrator(JobStatus.COMPLETED)
        lm = LairManager(_settings(), container_orchestrator=orch, document_store=_store())
        lair_id = await lm.create_lair(_task(), _node())

        report = await lm.wait_for_completion(lair_id, poll_interval=0)

        assert report.status is OperativeStatus.FAILED
        assert report.confidence_score == 0.0

    @pytest.mark.asyncio
    async def test_interrupted_report_persisted_by_operative_is_used(self):
        """A SIGTERM'd operative's partial report beats a fabricated FAILED one."""
        orch = _orchestrator(JobStatus.FAILED)
        store = _store()
        lm = LairManager(_settings(), container_orchestrator=orch, document_store=store)
        lair_id = await lm.create_lair(_task(), _node())
        interrupted = _report(status=OperativeStatus.INTERRUPTED, total_input_tokens=1234)

        async def _get(collection: str, doc_id: str):
            if collection == "task_executions":
                return {
                    "interrupted_node_id": "implement_fix",
                    "interrupted_report": interrupted.model_dump(mode="json"),
                }
            return None

        store.get = AsyncMock(side_effect=_get)

        report = await lm.wait_for_completion(lair_id, poll_interval=0)

        assert report.status is OperativeStatus.INTERRUPTED
        assert report.total_input_tokens == 1234
        store.update.assert_awaited_with(
            "task_executions", _task().id, {"interrupted_node_id": None, "interrupted_report": None}
        )

    @pytest.mark.asyncio
    async def test_interrupted_report_for_another_node_is_ignored(self):
        orch = _orchestrator(JobStatus.FAILED)
        store = _store()
        lm = LairManager(_settings(), container_orchestrator=orch, document_store=store)
        lair_id = await lm.create_lair(_task(), _node())
        other = _report(node_id="fix_tests", status=OperativeStatus.INTERRUPTED)

        async def _get(collection: str, doc_id: str):
            if collection == "task_executions":
                return {"interrupted_node_id": "fix_tests", "interrupted_report": other.model_dump(mode="json")}
            return None

        store.get = AsyncMock(side_effect=_get)

        report = await lm.wait_for_completion(lair_id, poll_interval=0)

        assert report.status is OperativeStatus.FAILED

    @pytest.mark.asyncio
    async def test_timed_out_job_maps_to_timed_out(self):
        orch = _orchestrator(JobStatus.TIMED_OUT)
        lm = LairManager(_settings(), container_orchestrator=orch, document_store=_store())
        lair_id = await lm.create_lair(_task(), _node())

        report = await lm.wait_for_completion(lair_id, poll_interval=0)

        assert report.status is OperativeStatus.TIMED_OUT

    @pytest.mark.asyncio
    async def test_wait_is_bounded_and_cancels_the_lair(self):
        orch = _orchestrator(JobStatus.RUNNING)
        lm = LairManager(_settings(), container_orchestrator=orch, document_store=_store())
        lair_id = await lm.create_lair(_task(), _node())

        report = await lm.wait_for_completion(lair_id, poll_interval=0, timeout_seconds=0)

        assert report.status is OperativeStatus.TIMED_OUT
        orch.cancel.assert_awaited_once_with("exec-123")

    @pytest.mark.asyncio
    async def test_unknown_lair_fails_fast(self):
        lm = LairManager(_settings(), container_orchestrator=_orchestrator(), document_store=_store())

        report = await lm.wait_for_completion("lair-does-not-exist", poll_interval=0)

        assert report.status is OperativeStatus.FAILED
