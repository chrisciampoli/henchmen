"""Unit tests for :class:`henchmen.mastermind.lair_manager.LairManager`.

Covers the container-job contract: what environment the operative receives,
how job ids are derived, which service account is used, and the fail-closed
behaviour of ``wait_for_completion`` when no report ever arrives.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from henchmen.config.settings import Environment, Settings
from henchmen.mastermind.lair_manager import LairManager
from henchmen.models.dossier import Dossier
from henchmen.models.llm import ModelTier
from henchmen.models.operative import OperativeReport, OperativeStatus
from henchmen.models.scheme import NodeType, SchemeNode
from henchmen.models.task import HenchmenTask, TaskContext, TaskSource
from henchmen.providers.interfaces.container_orchestrator import JobResult, JobStatus
from henchmen.utils.github_auth import (
    MAX_MIN_TTL_SECONDS,
    GitHubAppConfigurationError,
    GitHubAuthError,
    GitHubRepositoryReferenceError,
    InstallationToken,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    # _env_file=None: a developer's .env.local (a PAT, a GitHub App) must never reach these tests.
    base = Settings(**{"_env_file": None, "gcp_project_id": "test-project"})
    update: dict = {"provider": "gcp", "environment": Environment.DEV}
    update.update(overrides)
    return base.model_copy(update=update)


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


_PAT = "ghp_" + "p" * 36
_APP = {
    "github_app_id": "4242",
    "github_app_installation_id": "77",
    "github_app_private_key_path": "/data/secrets/github-app.pem",
}
_ISSUED = InstallationToken(token="ghs_installation", expires_at=1_900_000_000.0)


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
        env = LairManager(settings)._build_env_vars(_task(), _node(), "lair-1", github_token="ghp_" + "s" * 36)

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

    def test_desktop_install_env_never_carries_the_internal_push_token(self, monkeypatch, tmp_path):
        """Amendment B2: operatives never receive the internal push token."""
        from henchmen.config.internal_auth import clear_cache, load_internal_auth

        clear_cache()
        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
        internal = load_internal_auth(tmp_path / "secrets")
        settings = _settings(provider="local", gcp_project_id="")

        env = LairManager(settings)._build_env_vars(_task(), _node(), "lair-1")

        assert not any(internal.push_token in v for v in env.values())
        assert not any("PUSH_TOKEN" in key.upper() for key in env)

    def test_desktop_install_env_never_carries_a_token_for_another_task(self, monkeypatch, tmp_path):
        """Ruling 3: the same substring check as B2, but for a sibling task's token."""
        from henchmen.config.internal_auth import clear_cache, load_internal_auth

        clear_cache()
        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
        internal = load_internal_auth(tmp_path / "secrets")
        other_task_token = internal.task_token("some-other-task")
        settings = _settings(provider="local", gcp_project_id="")

        env = LairManager(settings)._build_env_vars(_task(), _node(), "lair-1")

        assert not any(other_task_token in v for v in env.values())

    def test_settings_token_is_never_forwarded_implicitly(self):
        settings = _settings(provider="local", gcp_project_id="", github_token="ghp_" + "s" * 36)
        env = LairManager(settings)._build_env_vars(_task(), _node(), "lair-1")
        assert "HENCHMEN_GITHUB_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env
        assert "HENCHMEN_GITHUB_TOKEN_EXPIRES_AT" not in env
        assert not any("ghp_" in value for value in env.values())

    def test_installation_token_expiry_is_forwarded(self):
        env = LairManager(_settings(provider="local", gcp_project_id=""))._build_env_vars(
            _task(), _node(), "lair-1", github_token="ghs_x", github_token_expires_at="2030-03-17T17:46:40Z"
        )
        assert env["HENCHMEN_GITHUB_TOKEN"] == env["GITHUB_TOKEN"] == "ghs_x"
        assert env["HENCHMEN_GITHUB_TOKEN_EXPIRES_AT"] == "2030-03-17T17:46:40Z"

    def test_an_expiry_without_a_token_is_dropped(self):
        env = LairManager(_settings(provider="local", gcp_project_id=""))._build_env_vars(
            _task(), _node(), "lair-1", github_token_expires_at="2030-03-17T17:46:40Z"
        )
        assert "HENCHMEN_GITHUB_TOKEN_EXPIRES_AT" not in env

    def test_lair_manager_module_never_references_the_push_token(self):
        """Belt-and-suspenders: even if env-building changes shape, the module must not name it."""
        import inspect

        from henchmen.mastermind import lair_manager

        source = inspect.getsource(lair_manager)
        assert "internal_push_token" not in source
        assert "INTERNAL_PUSH_TOKEN_FILE_NAME" not in source


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

    @pytest.mark.asyncio
    async def test_local_mode_without_an_app_forwards_the_pat(self):
        orch = _orchestrator()
        settings = _settings(provider="local", gcp_project_id="", github_token=_PAT)
        lm = LairManager(settings, container_orchestrator=orch, document_store=_store())

        await lm.create_lair(_task(), _node())

        env = orch.run_job.call_args.kwargs["env_vars"]
        assert env["GITHUB_TOKEN"] == env["HENCHMEN_GITHUB_TOKEN"] == _PAT
        assert "HENCHMEN_GITHUB_TOKEN_EXPIRES_AT" not in env

    @pytest.mark.asyncio
    async def test_github_app_operatives_get_a_repo_scoped_token_and_its_expiry(self, monkeypatch, tmp_path):
        from henchmen.config.internal_auth import clear_cache, load_internal_auth

        clear_cache()
        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
        internal = load_internal_auth(tmp_path / "secrets")
        minted = AsyncMock(return_value=_ISSUED)
        monkeypatch.setattr("henchmen.mastermind.lair_manager.get_installation_token_async", minted)
        orch = _orchestrator()
        settings = _settings(provider="local", gcp_project_id="", github_token=_PAT, **_APP)
        lm = LairManager(settings, container_orchestrator=orch, document_store=_store())

        await lm.create_lair(_task(), _node(timeout_seconds=600))

        kwargs = orch.run_job.call_args.kwargs
        env = kwargs["env_vars"]
        assert env["GITHUB_TOKEN"] == env["HENCHMEN_GITHUB_TOKEN"] == "ghs_installation"
        assert env["HENCHMEN_GITHUB_TOKEN_EXPIRES_AT"] == _ISSUED.expires_at_iso()
        assert kwargs["secrets"] is None
        # Neither the PAT, the App key path nor the internal push token reaches the operative.
        assert not any(_PAT in value for value in env.values())
        assert not any("github-app.pem" in value for value in env.values())
        assert not any(internal.push_token in value for value in env.values())
        minted.assert_awaited_once_with("acme/webapp", settings=settings, min_ttl_seconds=900)

    @pytest.mark.asyncio
    async def test_github_app_token_is_scoped_to_owner_name_for_a_clone_url(self, monkeypatch):
        minted = AsyncMock(return_value=_ISSUED)
        monkeypatch.setattr("henchmen.mastermind.lair_manager.get_installation_token_async", minted)
        settings = _settings(provider="local", gcp_project_id="", **_APP)
        lm = LairManager(settings, container_orchestrator=_orchestrator(), document_store=_store())
        task = _task(context=TaskContext(repo="https://github.com/acme/webapp.git", branch="main"))

        await lm.create_lair(task, _node())

        assert minted.await_args.args == ("acme/webapp",)

    @pytest.mark.asyncio
    async def test_a_node_longer_than_the_token_cap_warns_and_asks_for_the_cap(self, monkeypatch, caplog):
        minted = AsyncMock(return_value=_ISSUED)
        monkeypatch.setattr("henchmen.mastermind.lair_manager.get_installation_token_async", minted)
        lm = LairManager(_settings(**_APP), container_orchestrator=_orchestrator(), document_store=_store())

        with caplog.at_level("WARNING", logger="henchmen.mastermind.lair_manager"):
            await lm.create_lair(_task(), _node(timeout_seconds=3600))

        assert minted.await_args.kwargs["min_ttl_seconds"] == MAX_MIN_TTL_SECONDS
        assert any("refresh its token" in record.getMessage() for record in caplog.records)
        assert "ghs_installation" not in caplog.text

    @pytest.mark.asyncio
    async def test_github_app_on_cloud_run_does_not_mount_the_pat_secret(self, monkeypatch):
        monkeypatch.setattr(
            "henchmen.mastermind.lair_manager.get_installation_token_async", AsyncMock(return_value=_ISSUED)
        )
        orch = _orchestrator()
        lm = LairManager(_settings(github_token=_PAT, **_APP), container_orchestrator=orch, document_store=_store())

        await lm.create_lair(_task(), _node())

        kwargs = orch.run_job.call_args.kwargs
        assert kwargs["secrets"] is None
        assert kwargs["env_vars"]["GITHUB_TOKEN"] == "ghs_installation"
        assert kwargs["env_vars"]["HENCHMEN_GITHUB_TOKEN_EXPIRES_AT"] == _ISSUED.expires_at_iso()
        assert not any(_PAT in value for value in kwargs["env_vars"].values())

    @pytest.mark.asyncio
    async def test_cloud_run_without_an_app_keeps_the_secret_mount(self):
        orch = _orchestrator()
        lm = LairManager(_settings(github_token=_PAT), container_orchestrator=orch, document_store=_store())

        await lm.create_lair(_task(), _node())

        kwargs = orch.run_job.call_args.kwargs
        assert kwargs["secrets"] == {"GITHUB_TOKEN": "projects/test-project/secrets/henchmen-dev-github-token"}
        assert "GITHUB_TOKEN" not in kwargs["env_vars"]
        assert not any(_PAT in value for value in kwargs["env_vars"].values())

    @pytest.mark.asyncio
    async def test_local_provider_with_a_gcp_orchestrator_and_no_app_gets_no_env_token(self):
        """PI-6: the effective container orchestrator decides, not the coarse ``provider`` field."""
        orch = _orchestrator()
        settings = _settings(
            provider="local", gcp_project_id="", github_token=_PAT, container_orchestrator_provider="gcp"
        )
        lm = LairManager(settings, container_orchestrator=orch, document_store=_store())

        assert await lm._operative_github_credentials(_task(), 600) is None
        await lm.create_lair(_task(), _node())

        env = orch.run_job.call_args.kwargs["env_vars"]
        assert "GITHUB_TOKEN" not in env
        assert "HENCHMEN_GITHUB_TOKEN" not in env
        assert not any(_PAT in value for value in env.values())

    @pytest.mark.asyncio
    async def test_token_failure_starts_no_job(self, monkeypatch):
        monkeypatch.setattr(
            "henchmen.mastermind.lair_manager.get_installation_token_async",
            AsyncMock(side_effect=GitHubAuthError("GitHub refused to issue an installation token (HTTP 401)")),
        )
        orch = _orchestrator()
        store = _store()
        lm = LairManager(_settings(**_APP), container_orchestrator=orch, document_store=store)

        with pytest.raises(GitHubAuthError):
            await lm.create_lair(_task(), _node())

        orch.run_job.assert_not_awaited()
        store.delete.assert_not_awaited()
        assert lm._active_lairs == {}

    @pytest.mark.asyncio
    async def test_a_partly_configured_app_never_falls_back_to_the_pat(self, monkeypatch):
        minted = AsyncMock(return_value=_ISSUED)
        monkeypatch.setattr("henchmen.mastermind.lair_manager.get_installation_token_async", minted)
        orch = _orchestrator()
        settings = _settings(github_token=_PAT, github_app_id="4242")
        lm = LairManager(settings, container_orchestrator=orch, document_store=_store())

        with pytest.raises(GitHubAppConfigurationError):
            await lm.create_lair(_task(), _node())

        orch.run_job.assert_not_awaited()
        minted.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_github_app_with_no_task_repository_refuses_an_installation_wide_token(self, monkeypatch):
        minted = AsyncMock(return_value=_ISSUED)
        monkeypatch.setattr("henchmen.mastermind.lair_manager.get_installation_token_async", minted)
        orch = _orchestrator()
        lm = LairManager(_settings(**_APP), container_orchestrator=orch, document_store=_store())
        task = _task(context=TaskContext(repo="", branch="main"))

        with pytest.raises(GitHubRepositoryReferenceError):
            await lm.create_lair(task, _node())

        minted.assert_not_awaited()
        orch.run_job.assert_not_awaited()


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


def test_local_mode_uses_the_prebuilt_operative_image_when_configured():
    from henchmen.config.settings import Settings
    from henchmen.mastermind.lair_manager import LairManager

    settings = Settings(_env_file=None, provider="local", operative_image="ghcr.io/acme/henchmen/operative:0.3.0")
    assert LairManager(settings)._build_image() == "ghcr.io/acme/henchmen/operative:0.3.0"


def test_local_mode_falls_back_to_the_locally_built_image():
    from henchmen.config.settings import Settings
    from henchmen.mastermind.lair_manager import LairManager

    assert LairManager(Settings(_env_file=None, provider="local"))._build_image() == "henchmen-operative:local"


def test_desktop_lairs_receive_a_token_for_their_own_task_only(monkeypatch, tmp_path):
    from henchmen.config.internal_auth import load_internal_auth

    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    task = _task()
    env = LairManager(_settings(provider="local", gcp_project_id=""))._build_env_vars(task, _node(), "lair-1")

    internal = load_internal_auth(tmp_path / "secrets")
    token = env["HENCHMEN_OPERATIVE_TASK_TOKEN"]
    assert internal.verify_task_token(task.id, token)
    assert not internal.verify_task_token("another-task", token)
    assert internal.push_token not in env.values(), "operatives never receive the internal push token"


def test_no_task_token_outside_a_desktop_install(monkeypatch):
    monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
    env = LairManager(_settings(provider="local", gcp_project_id=""))._build_env_vars(_task(), _node(), "lair-1")
    assert "HENCHMEN_OPERATIVE_TASK_TOKEN" not in env


def test_cloud_lairs_never_receive_a_task_token(monkeypatch, tmp_path):
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    env = LairManager(_settings(provider="gcp"))._build_env_vars(_task(), _node(), "lair-1")
    assert "HENCHMEN_OPERATIVE_TASK_TOKEN" not in env


def test_desktop_with_a_gcp_container_orchestrator_override_gets_no_task_token(monkeypatch, tmp_path):
    """Ruling: the gate is the *effective* container orchestrator, not the coarse `provider` field."""
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    settings = _settings(provider="local", gcp_project_id="", container_orchestrator_provider="gcp")
    env = LairManager(settings)._build_env_vars(_task(), _node(), "lair-1")
    assert "HENCHMEN_OPERATIVE_TASK_TOKEN" not in env


class TestReportBinding:
    """B4: a report is accepted only for a lair this manager launched and still waits on."""

    @staticmethod
    def _manager():
        from datetime import UTC, datetime

        from henchmen.config.settings import Settings
        from henchmen.mastermind.lair_manager import LairManager

        manager = LairManager(Settings(_env_file=None, provider="local", lair_default_timeout=600))
        manager._active_lairs["lair-abc-implement-fix-1a2b3c"] = {
            "execution_id": "exec-1",
            "task_id": "task-1",
            "node_id": "implement_fix",
            "timeout_seconds": 600,
            "created_at": datetime.now(UTC).isoformat(),
        }
        return manager

    def test_the_launched_lair_is_accepted(self):
        manager = self._manager()
        assert manager.accepts_report_from("task-1", "implement_fix", "lair-abc-implement-fix-1a2b3c") is True
        assert manager.accepts_report_from("task-1", "implement_fix") is True

    @pytest.mark.parametrize(
        ("task_id", "node_id", "operative_id"),
        [("task-2", "implement_fix", None), ("task-1", "fix_tests", None), ("task-1", "implement_fix", "lair-other")],
    )
    def test_anything_else_is_refused(self, task_id, node_id, operative_id):
        assert self._manager().accepts_report_from(task_id, node_id, operative_id) is False

    def test_a_superseded_lair_cannot_report_over_its_replacement(self):
        from datetime import UTC, datetime, timedelta

        manager = self._manager()
        manager._active_lairs["lair-abc-implement-fix-1a2b3c"]["created_at"] = (
            datetime.now(UTC) - timedelta(seconds=30)
        ).isoformat()
        manager._active_lairs["lair-abc-implement-fix-9z8y7x"] = {
            "execution_id": "exec-2",
            "task_id": "task-1",
            "node_id": "implement_fix",
            "timeout_seconds": 600,
            "created_at": datetime.now(UTC).isoformat(),
        }
        assert manager.accepts_report_from("task-1", "implement_fix", "lair-abc-implement-fix-9z8y7x") is True
        assert manager.accepts_report_from("task-1", "implement_fix", "lair-abc-implement-fix-1a2b3c") is False

    def test_a_lair_past_its_wait_window_is_refused(self):
        from datetime import UTC, datetime, timedelta

        manager = self._manager()
        long_ago = datetime.now(UTC) - timedelta(seconds=600 + 300 + 15 + 60)
        manager._active_lairs["lair-abc-implement-fix-1a2b3c"]["created_at"] = long_ago.isoformat()
        assert manager.accepts_report_from("task-1", "implement_fix") is False

    @pytest.mark.asyncio
    async def test_create_lair_registers_what_the_binding_checks(self):
        from henchmen.config.settings import Settings
        from henchmen.mastermind.lair_manager import LairManager
        from henchmen.models.scheme import SchemeNode

        orchestrator = MagicMock()
        orchestrator.run_job = AsyncMock(return_value="exec-9")
        store = MagicMock()
        store.delete = AsyncMock()
        manager = LairManager(Settings(_env_file=None, provider="gcp", gcp_project_id="p"), orchestrator, store)
        task = MagicMock()
        task.id = "task-9"
        task.context.repo = "acme/api"
        task.context.branch = "main"
        task.title = "t"
        task.description = "d"
        task.branch_name = "henchmen/task-9"
        node = MagicMock(spec=SchemeNode)
        node.id = "implement_fix"
        node.timeout_seconds = 120
        node.model_name = None
        lair_id = await manager.create_lair(task, node)
        assert manager.accepts_report_from("task-9", "implement_fix", lair_id) is True
        assert manager.accepts_report_from("task-9", "implement_fix", "lair-forged") is False


class TestProvisionalLairRegistration:
    """M5: a lair is reportable from the moment it is launched, and forgotten if the launch fails."""

    @staticmethod
    def _task_and_node():
        from henchmen.models.scheme import SchemeNode

        task = MagicMock()
        task.id = "task-7"
        task.context.repo = "acme/api"
        task.context.branch = "main"
        task.title = "t"
        task.description = "d"
        task.branch_name = "henchmen/task-7"
        node = MagicMock(spec=SchemeNode)
        node.id = "implement_fix"
        node.timeout_seconds = 120
        node.model_name = None
        return task, node

    @pytest.mark.asyncio
    async def test_a_report_arriving_before_run_job_returns_is_accepted(self):
        from henchmen.config.settings import Settings
        from henchmen.mastermind.lair_manager import LairManager

        seen: list[bool] = []
        manager: LairManager

        async def _run_job(**kwargs):
            seen.append(manager.accepts_report_from("task-7", "implement_fix", kwargs["job_id"]))
            return "exec-1"

        orchestrator = MagicMock()
        orchestrator.run_job = AsyncMock(side_effect=_run_job)
        store = MagicMock()
        store.delete = AsyncMock()
        manager = LairManager(Settings(_env_file=None, provider="gcp", gcp_project_id="p"), orchestrator, store)
        lair_id = await manager.create_lair(*self._task_and_node())
        assert seen == [True]
        assert manager._active_lairs[lair_id]["execution_id"] == "exec-1"

    @pytest.mark.asyncio
    async def test_a_failed_launch_leaves_nothing_reportable(self):
        from henchmen.config.settings import Settings
        from henchmen.mastermind.lair_manager import LairManager

        orchestrator = MagicMock()
        orchestrator.run_job = AsyncMock(side_effect=RuntimeError("docker not running"))
        store = MagicMock()
        store.delete = AsyncMock()
        manager = LairManager(Settings(_env_file=None, provider="gcp", gcp_project_id="p"), orchestrator, store)
        with pytest.raises(RuntimeError, match="docker not running"):
            await manager.create_lair(*self._task_and_node())
        assert manager._active_lairs == {}
        assert manager.accepts_report_from("task-7", "implement_fix") is False


@pytest.mark.asyncio
async def test_a_lair_evicted_during_run_job_does_not_raise() -> None:
    """Residual minor: an entry evicted while run_job was awaited is skipped, never a KeyError."""
    from henchmen.config.settings import Settings
    from henchmen.mastermind.lair_manager import LairManager

    manager: LairManager

    async def _run_job(**kwargs):
        manager._active_lairs.pop(kwargs["job_id"], None)
        return "exec-9"

    orchestrator = MagicMock()
    orchestrator.run_job = AsyncMock(side_effect=_run_job)
    store = MagicMock()
    store.delete = AsyncMock()
    manager = LairManager(Settings(_env_file=None, provider="gcp", gcp_project_id="p"), orchestrator, store)
    task, node = TestProvisionalLairRegistration._task_and_node()
    lair_id = await manager.create_lair(task, node)
    assert lair_id.startswith("lair-")
    assert lair_id not in manager._active_lairs
