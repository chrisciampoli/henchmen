"""Tests for the GCP infrastructure providers (Cloud Run, Pub/Sub, Firestore, GCS, Cloud Build).

The LLM provider (Vertex AI) is covered in ``test_gcp_providers.py``; this
module owns everything else under ``henchmen.providers.gcp``.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.providers.interfaces.ci_provider import CIStatus
from henchmen.providers.interfaces.container_orchestrator import JobStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**overrides):
    """Build a real ``Settings`` instance with GCP-provider defaults."""
    import os

    from henchmen.config.settings import get_settings

    os.environ.setdefault("HENCHMEN_GCP_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"gcp_project_id": "test-project", "gcp_region": "us-central1"})
    if overrides:
        settings = settings.model_copy(update=overrides)
    return settings


def _operation(result=None, metadata=None):
    """A stand-in for a google-api-core AsyncOperation."""
    op = MagicMock()
    op.result = AsyncMock(return_value=result)
    op.metadata = metadata
    return op


def _orchestrator():
    from henchmen.providers.gcp.cloud_run import CloudRunOrchestrator

    return CloudRunOrchestrator(_settings())


# ---------------------------------------------------------------------------
# CloudRunOrchestrator.get_status — real SDK condition shapes
# ---------------------------------------------------------------------------


class TestCloudRunGetStatus:
    """The SDK reports Knative condition names ("Completed") plus a
    ``Condition.State`` enum; there is no "CONDITION_TRUE" state.
    """

    @staticmethod
    def _execution(**kwargs):
        from google.cloud.run_v2.types import Execution

        return Execution(**kwargs)

    @staticmethod
    def _condition(**kwargs):
        from google.cloud.run_v2.types import Condition

        return Condition(**kwargs)

    def _with_execution(self, execution):
        orch = _orchestrator()
        exec_client = AsyncMock()
        exec_client.get_execution = AsyncMock(return_value=execution)
        orch._exec_client = exec_client
        return orch

    @pytest.mark.asyncio
    async def test_completed_condition_succeeded_maps_to_completed(self):
        from google.cloud.run_v2.types import Condition

        execution = self._execution(
            conditions=[self._condition(type_="Completed", state=Condition.State.CONDITION_SUCCEEDED)]
        )
        result = await self._with_execution(execution).get_status("exec-1")
        assert result.status == JobStatus.COMPLETED
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_completed_condition_failed_maps_to_failed(self):
        from google.cloud.run_v2.types import Condition

        execution = self._execution(
            conditions=[
                self._condition(
                    type_="Completed",
                    state=Condition.State.CONDITION_FAILED,
                    execution_reason=Condition.ExecutionReason.NON_ZERO_EXIT_CODE,
                    message="task exited 1",
                )
            ]
        )
        result = await self._with_execution(execution).get_status("exec-1")
        assert result.status == JobStatus.FAILED
        assert result.logs == "task exited 1"

    @pytest.mark.asyncio
    async def test_cancelled_execution_reason_maps_to_cancelled(self):
        from google.cloud.run_v2.types import Condition

        execution = self._execution(
            conditions=[
                self._condition(
                    type_="Completed",
                    state=Condition.State.CONDITION_FAILED,
                    execution_reason=Condition.ExecutionReason.CANCELLED,
                )
            ]
        )
        result = await self._with_execution(execution).get_status("exec-1")
        assert result.status == JobStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_progress_deadline_exceeded_maps_to_timed_out(self):
        from google.cloud.run_v2.types import Condition

        execution = self._execution(
            conditions=[
                self._condition(
                    type_="Completed",
                    state=Condition.State.CONDITION_FAILED,
                    reason=Condition.CommonReason.PROGRESS_DEADLINE_EXCEEDED,
                )
            ]
        )
        result = await self._with_execution(execution).get_status("exec-1")
        assert result.status == JobStatus.TIMED_OUT

    @pytest.mark.asyncio
    async def test_started_but_not_completed_maps_to_running(self):
        from google.cloud.run_v2.types import Condition

        execution = self._execution(
            conditions=[
                self._condition(type_="Started", state=Condition.State.CONDITION_SUCCEEDED),
                self._condition(type_="Completed", state=Condition.State.CONDITION_PENDING),
            ]
        )
        result = await self._with_execution(execution).get_status("exec-1")
        assert result.status == JobStatus.RUNNING

    @pytest.mark.asyncio
    async def test_running_count_implies_running(self):
        execution = self._execution(running_count=1)
        result = await self._with_execution(execution).get_status("exec-1")
        assert result.status == JobStatus.RUNNING

    @pytest.mark.asyncio
    async def test_no_conditions_maps_to_provisioning(self):
        result = await self._with_execution(self._execution()).get_status("exec-1")
        assert result.status == JobStatus.PROVISIONING


# ---------------------------------------------------------------------------
# CloudRunOrchestrator.run_job
# ---------------------------------------------------------------------------


class TestCloudRunRunJob:
    def _client(self, execution_name="projects/p/locations/r/jobs/j/executions/e1"):
        client = MagicMock()
        client.create_job = AsyncMock(return_value=_operation())
        client.update_job = AsyncMock(return_value=_operation())
        client.delete_job = AsyncMock(return_value=_operation())
        client.run_job = AsyncMock(return_value=_operation(metadata=SimpleNamespace(name=execution_name)))
        return client

    @pytest.mark.asyncio
    async def test_secrets_become_secret_key_ref_env_vars(self):
        """Without this the operative clones and pushes unauthenticated on GCP."""
        orch = _orchestrator()
        client = self._client()
        orch._jobs_client = client

        await orch.run_job(
            job_id="lair-abc-implement-fix",
            image="img:latest",
            env_vars={"TASK_ID": "t-1"},
            secrets={"GITHUB_TOKEN": "projects/p/secrets/henchmen-dev-github-token"},
        )

        job = client.create_job.await_args.kwargs["job"]
        env = job.template.template.containers[0].env
        by_name = {var.name: var for var in env}
        assert by_name["TASK_ID"].value == "t-1"
        token = by_name["GITHUB_TOKEN"]
        assert token.value_source.secret_key_ref.secret == "projects/p/secrets/henchmen-dev-github-token"
        assert token.value_source.secret_key_ref.version == "latest"

    @pytest.mark.asyncio
    async def test_timeout_and_service_account_are_applied(self):
        orch = _orchestrator()
        client = self._client()
        orch._jobs_client = client

        await orch.run_job(
            job_id="lair-1",
            image="img",
            env_vars={},
            timeout_seconds=900,
            service_account="sa@project.iam.gserviceaccount.com",
        )

        template = client.create_job.await_args.kwargs["job"].template.template
        assert template.timeout.total_seconds() == 900
        assert template.service_account == "sa@project.iam.gserviceaccount.com"

    @pytest.mark.asyncio
    async def test_returns_execution_name_from_operation_metadata(self):
        orch = _orchestrator()
        client = self._client(execution_name="projects/p/locations/r/jobs/j/executions/exec-9")
        orch._jobs_client = client

        name = await orch.run_job(job_id="lair-1", image="img", env_vars={})

        assert name == "projects/p/locations/r/jobs/j/executions/exec-9"
        # Awaiting the run operation's result would block until the job ended.
        client.run_job.return_value.result.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_job_is_updated_instead_of_failing(self):
        """Node retries and the CI-fix loop reuse the deterministic lair id."""
        from google.api_core import exceptions as api_exceptions

        orch = _orchestrator()
        client = self._client()
        client.create_job = AsyncMock(side_effect=api_exceptions.AlreadyExists("exists"))
        orch._jobs_client = client

        name = await orch.run_job(job_id="lair-dup", image="img", env_vars={})

        client.update_job.assert_awaited_once()
        updated = client.update_job.await_args.kwargs["job"]
        assert updated.name == "projects/test-project/locations/us-central1/jobs/lair-dup"
        assert name.endswith("/executions/e1")

    @pytest.mark.asyncio
    async def test_delete_job_swallows_not_found(self):
        from google.api_core import exceptions as api_exceptions

        orch = _orchestrator()
        client = self._client()
        client.delete_job = AsyncMock(side_effect=api_exceptions.NotFound("gone"))
        orch._jobs_client = client

        await orch.delete_job("lair-gone")  # must not raise

        client.delete_job.assert_awaited_once_with(name="projects/test-project/locations/us-central1/jobs/lair-gone")


# ---------------------------------------------------------------------------
# PubSubMessageBroker
# ---------------------------------------------------------------------------


class TestPubSubMessageBroker:
    @pytest.mark.asyncio
    async def test_publisher_enables_message_ordering(self):
        """Publishing with an ordering_key raises ValueError otherwise."""
        with patch("henchmen.providers.gcp.pubsub.pubsub_v1") as mock_pubsub:
            from henchmen.providers.gcp.pubsub import PubSubMessageBroker

            PubSubMessageBroker(_settings())

            options = mock_pubsub.types.PublisherOptions.call_args.kwargs
            assert options == {"enable_message_ordering": True}

    @pytest.mark.asyncio
    async def test_publish_does_not_block_the_event_loop(self):
        """future.result() is synchronous and must run in a worker thread."""
        import threading

        with patch("henchmen.providers.gcp.pubsub.pubsub_v1") as mock_pubsub:
            mock_client = MagicMock()
            mock_pubsub.PublisherClient.return_value = mock_client
            mock_client.topic_path.return_value = "projects/test-project/topics/t"
            seen: dict[str, int] = {}

            def blocking_result():
                seen["thread"] = threading.get_ident()
                return "msg-1"

            mock_client.publish.return_value = MagicMock(result=blocking_result)

            from henchmen.providers.gcp.pubsub import PubSubMessageBroker

            broker = PubSubMessageBroker(_settings())
            msg_id = await broker.publish("t", b"x", ordering_key="k")

            assert msg_id == "msg-1"
            assert seen["thread"] != threading.get_ident()
            assert mock_client.publish.call_args.kwargs["ordering_key"] == "k"

    @pytest.mark.asyncio
    async def test_aclose_releases_clients(self):
        with patch("henchmen.providers.gcp.pubsub.pubsub_v1") as mock_pubsub:
            mock_client = MagicMock()
            mock_pubsub.PublisherClient.return_value = mock_client
            mock_subscriber = MagicMock()
            mock_pubsub.SubscriberClient.return_value = mock_subscriber

            from henchmen.providers.gcp.pubsub import PubSubMessageBroker

            broker = PubSubMessageBroker(_settings())
            broker._get_subscriber()
            await broker.aclose()

            mock_subscriber.close.assert_called_once()
            mock_client.stop.assert_called_once()


# ---------------------------------------------------------------------------
# FirestoreDocumentStore — create-or-merge contract
# ---------------------------------------------------------------------------


class TestFirestoreCreateOrMerge:
    def _store_with_missing_doc(self, mock_fs):
        from google.api_core.exceptions import NotFound

        mock_client = MagicMock()
        mock_fs.AsyncClient.return_value = mock_client
        doc_ref = MagicMock()
        doc_ref.update = AsyncMock(side_effect=NotFound("missing"))
        doc_ref.set = AsyncMock()
        mock_client.collection.return_value.document.return_value = doc_ref

        from henchmen.providers.gcp.firestore import FirestoreDocumentStore

        return FirestoreDocumentStore(_settings()), doc_ref

    @pytest.mark.asyncio
    async def test_update_creates_missing_document(self):
        with patch("henchmen.providers.gcp.firestore.firestore") as mock_fs:
            store, doc_ref = self._store_with_missing_doc(mock_fs)
            await store.update("task_executions", "t-1", {"status": "running"})
            doc_ref.set.assert_awaited_once_with({"status": "running"}, merge=True)

    @pytest.mark.asyncio
    async def test_increment_creates_missing_document(self):
        with patch("henchmen.providers.gcp.firestore.firestore") as mock_fs:
            mock_fs.Increment.side_effect = lambda v: ("INCREMENT", v)
            store, doc_ref = self._store_with_missing_doc(mock_fs)
            await store.increment("task_executions", "t-1", {"total_input_tokens": 10})
            doc_ref.set.assert_awaited_once_with({"total_input_tokens": ("INCREMENT", 10)}, merge=True)


# ---------------------------------------------------------------------------
# GCSObjectStore
# ---------------------------------------------------------------------------


class TestGCSObjectStore:
    @pytest.mark.asyncio
    async def test_list_keys_iterates_off_the_event_loop(self):
        """list_blobs returns a lazy iterator — iterating it must not block."""
        import threading

        with patch("henchmen.providers.gcp.gcs.storage") as mock_storage:
            mock_client = MagicMock()
            mock_storage.Client.return_value = mock_client
            seen: dict[str, int] = {}

            def lazy_list_blobs(bucket, prefix=""):
                seen["thread"] = threading.get_ident()
                return [SimpleNamespace(name="a.json"), SimpleNamespace(name="b.json")]

            mock_client.list_blobs.side_effect = lazy_list_blobs

            from henchmen.providers.gcp.gcs import GCSObjectStore

            store = GCSObjectStore(_settings())
            keys = await store.list_keys("bucket", prefix="p/")

            assert keys == ["a.json", "b.json"]
            assert seen["thread"] != threading.get_ident()


# ---------------------------------------------------------------------------
# CloudBuildCIProvider
# ---------------------------------------------------------------------------


def _cloudbuild_stub():
    stub = MagicMock()
    for name in (
        "SUCCESS",
        "FAILURE",
        "INTERNAL_ERROR",
        "STATUS_UNKNOWN",
        "TIMEOUT",
        "EXPIRED",
        "CANCELLED",
        "WORKING",
        "QUEUED",
        "PENDING",
    ):
        setattr(stub.Build.Status, name, name)
    return stub


class TestCloudBuildCIProvider:
    @pytest.mark.asyncio
    async def test_internal_error_is_terminal_not_pending(self):
        """A PENDING reading would make callers poll for ever."""
        stub = _cloudbuild_stub()
        with patch.dict(sys.modules, {"google.cloud.cloudbuild_v1": stub}):
            from henchmen.providers.gcp.cloud_build import CloudBuildCIProvider

            provider = CloudBuildCIProvider(_settings())
            build = MagicMock()
            build.status = "INTERNAL_ERROR"
            build.log_url = None
            build.start_time = None
            build.finish_time = None
            provider._client = AsyncMock(get_build=AsyncMock(return_value=build))

            result = await provider.get_status("b-1")
            assert result.status == CIStatus.FAILURE

    @pytest.mark.asyncio
    async def test_builder_image_is_configurable_and_defaults_to_python(self):
        stub = _cloudbuild_stub()
        with patch.dict(sys.modules, {"google.cloud.cloudbuild_v1": stub}):
            from henchmen.providers.gcp.cloud_build import CloudBuildCIProvider

            provider = CloudBuildCIProvider(_settings())
            provider._client = AsyncMock(
                create_build=AsyncMock(
                    return_value=_operation(metadata=SimpleNamespace(build=SimpleNamespace(id="b-7")))
                )
            )

            build_id = await provider.trigger_build("https://github.com/o/r.git", "main", ["pytest -q"])

            assert build_id == "b-7"
            images = [call.kwargs.get("name") for call in stub.BuildStep.call_args_list]
            assert images[0] == "gcr.io/cloud-builders/git"
            assert images[1] == "python:3.12"

    @pytest.mark.asyncio
    async def test_trigger_build_does_not_wait_for_the_build(self):
        stub = _cloudbuild_stub()
        with patch.dict(sys.modules, {"google.cloud.cloudbuild_v1": stub}):
            from henchmen.providers.gcp.cloud_build import CloudBuildCIProvider

            provider = CloudBuildCIProvider(_settings())
            operation = _operation(metadata=SimpleNamespace(build=SimpleNamespace(id="b-8")))
            provider._client = AsyncMock(create_build=AsyncMock(return_value=operation))

            await provider.trigger_build("https://github.com/o/r.git", "main", ["true"])

            operation.result.assert_not_awaited()
