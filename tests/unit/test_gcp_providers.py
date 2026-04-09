"""Tests for GCP provider implementations.

These tests mock the GCP SDK to verify our wrappers work correctly
without requiring actual GCP credentials.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.providers.interfaces.container_orchestrator import JobStatus


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.gcp_project_id = "test-project"
    s.gcp_region = "us-central1"
    s.firestore_database = "(default)"
    s.vertex_ai_model_complex = "gemini-2.5-pro"
    s.vertex_ai_model_light = "gemini-2.5-flash"
    s.vertex_ai_context_cache_enabled = False
    s.vertex_ai_safety_threshold = "BLOCK_MEDIUM_AND_ABOVE"
    s.environment = MagicMock()
    s.environment.value = "dev"
    return s


class TestPubSubMessageBroker:
    @pytest.mark.asyncio
    async def test_publish(self, mock_settings):
        with patch("henchmen.providers.gcp.pubsub.pubsub_v1") as mock_pubsub:
            mock_client = MagicMock()
            mock_pubsub.PublisherClient.return_value = mock_client
            mock_client.topic_path.return_value = "projects/test-project/topics/test-topic"
            mock_future = MagicMock()
            mock_future.result.return_value = "msg-123"
            mock_client.publish.return_value = mock_future

            from henchmen.providers.gcp.pubsub import PubSubMessageBroker

            broker = PubSubMessageBroker(mock_settings)
            msg_id = await broker.publish("test-topic", b'{"task_id": "t-1"}', source="cli")
            assert msg_id == "msg-123"
            mock_client.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_with_ordering_key(self, mock_settings):
        with patch("henchmen.providers.gcp.pubsub.pubsub_v1") as mock_pubsub:
            mock_client = MagicMock()
            mock_pubsub.PublisherClient.return_value = mock_client
            mock_client.topic_path.return_value = "projects/test-project/topics/test-topic"
            mock_future = MagicMock()
            mock_future.result.return_value = "msg-456"
            mock_client.publish.return_value = mock_future

            from henchmen.providers.gcp.pubsub import PubSubMessageBroker

            broker = PubSubMessageBroker(mock_settings)
            msg_id = await broker.publish("test-topic", b"data", ordering_key="key-1")
            assert msg_id == "msg-456"
            call_kwargs = mock_client.publish.call_args[1]
            assert call_kwargs.get("ordering_key") == "key-1"


class TestFirestoreDocumentStore:
    @pytest.mark.asyncio
    async def test_get_existing(self, mock_settings):
        with patch("henchmen.providers.gcp.firestore.firestore") as mock_fs:
            mock_client = MagicMock()
            mock_fs.AsyncClient.return_value = mock_client
            mock_doc = MagicMock()
            mock_doc.exists = True
            mock_doc.to_dict.return_value = {"status": "completed"}
            mock_doc.id = "doc-1"
            mock_client.collection.return_value.document.return_value.get = AsyncMock(return_value=mock_doc)

            from henchmen.providers.gcp.firestore import FirestoreDocumentStore

            store = FirestoreDocumentStore(mock_settings)
            result = await store.get("tasks", "doc-1")
            assert result == {"status": "completed", "_id": "doc-1"}

    @pytest.mark.asyncio
    async def test_get_missing(self, mock_settings):
        with patch("henchmen.providers.gcp.firestore.firestore") as mock_fs:
            mock_client = MagicMock()
            mock_fs.AsyncClient.return_value = mock_client
            mock_doc = MagicMock()
            mock_doc.exists = False
            mock_client.collection.return_value.document.return_value.get = AsyncMock(return_value=mock_doc)

            from henchmen.providers.gcp.firestore import FirestoreDocumentStore

            store = FirestoreDocumentStore(mock_settings)
            result = await store.get("tasks", "missing")
            assert result is None

    @pytest.mark.asyncio
    async def test_set(self, mock_settings):
        with patch("henchmen.providers.gcp.firestore.firestore") as mock_fs:
            mock_client = MagicMock()
            mock_fs.AsyncClient.return_value = mock_client
            mock_client.collection.return_value.document.return_value.set = AsyncMock()

            from henchmen.providers.gcp.firestore import FirestoreDocumentStore

            store = FirestoreDocumentStore(mock_settings)
            await store.set("tasks", "doc-1", {"status": "pending"})
            mock_client.collection.return_value.document.return_value.set.assert_called_once_with({"status": "pending"})

    @pytest.mark.asyncio
    async def test_delete(self, mock_settings):
        with patch("henchmen.providers.gcp.firestore.firestore") as mock_fs:
            mock_client = MagicMock()
            mock_fs.AsyncClient.return_value = mock_client
            mock_client.collection.return_value.document.return_value.delete = AsyncMock()

            from henchmen.providers.gcp.firestore import FirestoreDocumentStore

            store = FirestoreDocumentStore(mock_settings)
            await store.delete("tasks", "doc-1")
            mock_client.collection.return_value.document.return_value.delete.assert_called_once()


class TestGCSObjectStore:
    @pytest.mark.asyncio
    async def test_put_and_exists(self, mock_settings):
        with patch("henchmen.providers.gcp.gcs.storage") as mock_storage:
            mock_client = MagicMock()
            mock_storage.Client.return_value = mock_client
            mock_blob = MagicMock()
            mock_client.bucket.return_value.blob.return_value = mock_blob
            mock_blob.exists.return_value = True

            from henchmen.providers.gcp.gcs import GCSObjectStore

            store = GCSObjectStore(mock_settings)
            await store.put("my-bucket", "file.json", b'{"key": "value"}')
            mock_blob.upload_from_string.assert_called_once()

            result = await store.exists("my-bucket", "file.json")
            assert result is True

    @pytest.mark.asyncio
    async def test_get(self, mock_settings):
        with patch("henchmen.providers.gcp.gcs.storage") as mock_storage:
            mock_client = MagicMock()
            mock_storage.Client.return_value = mock_client
            mock_blob = MagicMock()
            mock_blob.download_as_bytes.return_value = b"file-content"
            mock_client.bucket.return_value.blob.return_value = mock_blob

            from henchmen.providers.gcp.gcs import GCSObjectStore

            store = GCSObjectStore(mock_settings)
            data = await store.get("my-bucket", "file.json")
            assert data == b"file-content"

    @pytest.mark.asyncio
    async def test_delete(self, mock_settings):
        with patch("henchmen.providers.gcp.gcs.storage") as mock_storage:
            mock_client = MagicMock()
            mock_storage.Client.return_value = mock_client
            mock_blob = MagicMock()
            mock_client.bucket.return_value.blob.return_value = mock_blob

            from henchmen.providers.gcp.gcs import GCSObjectStore

            store = GCSObjectStore(mock_settings)
            await store.delete("my-bucket", "file.json")
            mock_blob.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_keys(self, mock_settings):
        with patch("henchmen.providers.gcp.gcs.storage") as mock_storage:
            mock_client = MagicMock()
            mock_storage.Client.return_value = mock_client
            b1 = MagicMock()
            b1.name = "prefix/a.json"
            b2 = MagicMock()
            b2.name = "prefix/b.json"
            mock_client.list_blobs.return_value = [b1, b2]

            from henchmen.providers.gcp.gcs import GCSObjectStore

            store = GCSObjectStore(mock_settings)
            keys = await store.list_keys("my-bucket", prefix="prefix/")
            assert keys == ["prefix/a.json", "prefix/b.json"]


class TestVertexAIProvider:
    def test_resolve_tier(self, mock_settings):
        with patch("henchmen.providers.gcp.vertex_ai.genai", MagicMock()):
            from henchmen.models.llm import ModelTier
            from henchmen.providers.gcp.vertex_ai import VertexAIProvider

            provider = VertexAIProvider(mock_settings)
            assert provider.resolve_tier(ModelTier.COMPLEX) == "gemini-2.5-pro"
            assert provider.resolve_tier(ModelTier.LIGHT) == "gemini-2.5-flash"

    def test_resolve_tier_unknown_passthrough(self, mock_settings):
        with patch("henchmen.providers.gcp.vertex_ai.genai", MagicMock()):
            from henchmen.providers.gcp.vertex_ai import VertexAIProvider

            provider = VertexAIProvider(mock_settings)
            assert provider.resolve_tier("gemini-2.5-pro") == "gemini-2.5-pro"

    def test_supported_models(self, mock_settings):
        with patch("henchmen.providers.gcp.vertex_ai.genai", MagicMock()):
            from henchmen.providers.gcp.vertex_ai import VertexAIProvider

            provider = VertexAIProvider(mock_settings)
            models = provider.supported_models()
            assert "gemini-2.5-pro" in models
            assert "gemini-2.5-flash" in models

    def test_estimate_cost(self, mock_settings):
        with patch("henchmen.providers.gcp.vertex_ai.genai", MagicMock()):
            from henchmen.providers.gcp.vertex_ai import VertexAIProvider

            provider = VertexAIProvider(mock_settings)
            cost = provider._estimate_cost("gemini-2.5-pro", 1_000_000, 1_000_000, 0)
            assert cost == pytest.approx(1.25 + 10.0)

    def test_estimate_cost_with_cache(self, mock_settings):
        with patch("henchmen.providers.gcp.vertex_ai.genai", MagicMock()):
            from henchmen.providers.gcp.vertex_ai import VertexAIProvider

            provider = VertexAIProvider(mock_settings)
            # 500k cached, 500k non-cached input, 100k output — gemini-2.5-pro
            cost = provider._estimate_cost("gemini-2.5-pro", 1_000_000, 100_000, 500_000)
            expected = (500_000 / 1_000_000) * 1.25 + (500_000 / 1_000_000) * 1.25 * 0.25 + (100_000 / 1_000_000) * 10.0
            assert cost == pytest.approx(expected)

    def test_estimate_cost_unknown_model_defaults(self, mock_settings):
        with patch("henchmen.providers.gcp.vertex_ai.genai", MagicMock()):
            from henchmen.providers.gcp.vertex_ai import VertexAIProvider

            provider = VertexAIProvider(mock_settings)
            # Unknown model falls back to gemini-2.5-pro pricing
            cost = provider._estimate_cost("unknown-model", 1_000_000, 0, 0)
            assert cost == pytest.approx(1.25)


class TestCloudRunOrchestrator:
    def test_parent_path_constructed_correctly(self, mock_settings):
        with patch("henchmen.providers.gcp.cloud_run.JobResult"), patch("henchmen.providers.gcp.cloud_run.JobStatus"):
            from henchmen.providers.gcp.cloud_run import CloudRunOrchestrator

            orch = CloudRunOrchestrator(mock_settings)
            assert orch._parent == "projects/test-project/locations/us-central1"

    @pytest.mark.asyncio
    async def test_get_status_completed(self, mock_settings):
        with patch("henchmen.providers.gcp.cloud_run.run_v2", create=True):
            from henchmen.providers.gcp.cloud_run import CloudRunOrchestrator

            orch = CloudRunOrchestrator(mock_settings)

            mock_exec_client = AsyncMock()
            mock_condition = MagicMock()
            mock_condition.type_ = "CONDITION_SUCCEEDED"
            mock_condition.state.name = "CONDITION_TRUE"
            mock_execution = MagicMock()
            mock_execution.conditions = [mock_condition]
            mock_exec_client.get_execution = AsyncMock(return_value=mock_execution)
            orch._exec_client = mock_exec_client

            result = await orch.get_status("projects/test-project/locations/us-central1/jobs/j1/executions/e1")
            assert result.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_get_status_provisioning_when_no_matching_condition(self, mock_settings):
        from henchmen.providers.gcp.cloud_run import CloudRunOrchestrator

        orch = CloudRunOrchestrator(mock_settings)

        mock_exec_client = AsyncMock()
        mock_condition = MagicMock()
        mock_condition.type_ = "CONDITION_UNKNOWN"
        mock_condition.state.name = "CONDITION_TRUE"
        mock_execution = MagicMock()
        mock_execution.conditions = [mock_condition]
        mock_exec_client.get_execution = AsyncMock(return_value=mock_execution)
        orch._exec_client = mock_exec_client

        result = await orch.get_status("exec-1")
        assert result.status == JobStatus.PROVISIONING


class TestCloudBuildCIProvider:
    @pytest.mark.asyncio
    async def test_get_status_success(self, mock_settings):
        import sys

        mock_cloudbuild = MagicMock()
        mock_cloudbuild.Build.Status.SUCCESS = "SUCCESS"
        mock_cloudbuild.Build.Status.FAILURE = "FAILURE"
        mock_cloudbuild.Build.Status.TIMEOUT = "TIMEOUT"
        mock_cloudbuild.Build.Status.CANCELLED = "CANCELLED"
        mock_cloudbuild.Build.Status.WORKING = "WORKING"
        mock_cloudbuild.Build.Status.QUEUED = "QUEUED"

        with patch.dict(sys.modules, {"google.cloud.cloudbuild_v1": mock_cloudbuild}):
            from henchmen.providers.gcp.cloud_build import CloudBuildCIProvider

            provider = CloudBuildCIProvider(mock_settings)

            mock_client = AsyncMock()
            mock_build = MagicMock()
            mock_build.status = "SUCCESS"
            mock_build.log_url = "https://example.com/logs/build-1"
            mock_client.get_build = AsyncMock(return_value=mock_build)
            provider._client = mock_client

            result = await provider.get_status("build-1")
            assert result.build_id == "build-1"
            assert result.logs_url == "https://example.com/logs/build-1"

    @pytest.mark.asyncio
    async def test_get_logs_fallback_url(self, mock_settings):
        import sys

        mock_cloudbuild = MagicMock()
        mock_cloudbuild.Build.Status.SUCCESS = "SUCCESS"
        mock_cloudbuild.Build.Status.FAILURE = "FAILURE"
        mock_cloudbuild.Build.Status.TIMEOUT = "TIMEOUT"
        mock_cloudbuild.Build.Status.CANCELLED = "CANCELLED"
        mock_cloudbuild.Build.Status.WORKING = "WORKING"
        mock_cloudbuild.Build.Status.QUEUED = "QUEUED"

        with patch.dict(sys.modules, {"google.cloud.cloudbuild_v1": mock_cloudbuild}):
            from henchmen.providers.gcp.cloud_build import CloudBuildCIProvider

            provider = CloudBuildCIProvider(mock_settings)

            mock_client = AsyncMock()
            mock_build = MagicMock()
            mock_build.status = "QUEUED"
            mock_build.log_url = None
            mock_client.get_build = AsyncMock(return_value=mock_build)
            provider._client = mock_client

            logs = await provider.get_logs("build-99")
            assert "build-99" in logs
            assert "cloud-build" in logs

    @pytest.mark.asyncio
    async def test_cancel(self, mock_settings):
        from henchmen.providers.gcp.cloud_build import CloudBuildCIProvider

        provider = CloudBuildCIProvider(mock_settings)

        mock_client = AsyncMock()
        mock_client.cancel_build = AsyncMock()
        provider._client = mock_client

        await provider.cancel("build-42")
        mock_client.cancel_build.assert_called_once_with(project_id="test-project", id="build-42")
