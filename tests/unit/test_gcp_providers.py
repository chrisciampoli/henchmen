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

    @pytest.mark.asyncio
    async def test_increment_uses_firestore_increment_transform(self, mock_settings):
        with patch("henchmen.providers.gcp.firestore.firestore") as mock_fs:
            mock_client = MagicMock()
            mock_fs.AsyncClient.return_value = mock_client

            # Sentinel objects so we can identify the Increment wrapper.
            mock_fs.Increment.side_effect = lambda v: ("INCREMENT", v)

            mock_doc_ref = MagicMock()
            mock_doc_ref.update = AsyncMock()
            mock_client.collection.return_value.document.return_value = mock_doc_ref

            from henchmen.providers.gcp.firestore import FirestoreDocumentStore

            store = FirestoreDocumentStore(mock_settings)
            await store.increment("tasks", "doc-1", {"total_input_tokens": 10, "estimated_cost_usd": 0.5})

            mock_doc_ref.update.assert_awaited_once()
            payload = mock_doc_ref.update.call_args.args[0]
            assert payload == {
                "total_input_tokens": ("INCREMENT", 10),
                "estimated_cost_usd": ("INCREMENT", 0.5),
            }

    @pytest.mark.asyncio
    async def test_update_if_success_commits_in_transaction(self, mock_settings):
        """update_if returns True when the expected value matches under a transaction."""
        with patch("henchmen.providers.gcp.firestore.firestore") as mock_fs:
            mock_client = MagicMock()
            mock_fs.AsyncClient.return_value = mock_client

            mock_doc_ref = MagicMock()
            mock_client.collection.return_value.document.return_value = mock_doc_ref

            # Snapshot read inside the transaction returns status == "pending"
            mock_snapshot = MagicMock()
            mock_snapshot.exists = True
            mock_snapshot.to_dict.return_value = {"status": "pending"}

            # Emulate the async-transactional decorator used by google-cloud-firestore:
            # the wrapped function is awaited with a transaction object as the first arg.
            def async_transactional_decorator(func):
                async def wrapper(transaction, *args, **kwargs):
                    return await func(transaction, *args, **kwargs)

                return wrapper

            mock_fs.async_transactional.side_effect = async_transactional_decorator
            mock_transaction = MagicMock()
            mock_transaction.update = MagicMock()
            mock_client.transaction.return_value = mock_transaction
            mock_doc_ref.get = AsyncMock(return_value=mock_snapshot)

            from henchmen.providers.gcp.firestore import FirestoreDocumentStore

            store = FirestoreDocumentStore(mock_settings)
            ok = await store.update_if("queue", "e1", "status", "pending", {"status": "merging"})
            assert ok is True
            mock_transaction.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_if_rejects_when_field_mismatch(self, mock_settings):
        with patch("henchmen.providers.gcp.firestore.firestore") as mock_fs:
            mock_client = MagicMock()
            mock_fs.AsyncClient.return_value = mock_client

            mock_doc_ref = MagicMock()
            mock_client.collection.return_value.document.return_value = mock_doc_ref

            mock_snapshot = MagicMock()
            mock_snapshot.exists = True
            mock_snapshot.to_dict.return_value = {"status": "merging"}

            def async_transactional_decorator(func):
                async def wrapper(transaction, *args, **kwargs):
                    return await func(transaction, *args, **kwargs)

                return wrapper

            mock_fs.async_transactional.side_effect = async_transactional_decorator
            mock_transaction = MagicMock()
            mock_transaction.update = MagicMock()
            mock_client.transaction.return_value = mock_transaction
            mock_doc_ref.get = AsyncMock(return_value=mock_snapshot)

            from henchmen.providers.gcp.firestore import FirestoreDocumentStore

            store = FirestoreDocumentStore(mock_settings)
            ok = await store.update_if("queue", "e1", "status", "pending", {"status": "merged"})
            assert ok is False
            mock_transaction.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_if_returns_false_when_doc_missing(self, mock_settings):
        with patch("henchmen.providers.gcp.firestore.firestore") as mock_fs:
            mock_client = MagicMock()
            mock_fs.AsyncClient.return_value = mock_client

            mock_doc_ref = MagicMock()
            mock_client.collection.return_value.document.return_value = mock_doc_ref

            mock_snapshot = MagicMock()
            mock_snapshot.exists = False
            mock_snapshot.to_dict.return_value = None

            def async_transactional_decorator(func):
                async def wrapper(transaction, *args, **kwargs):
                    return await func(transaction, *args, **kwargs)

                return wrapper

            mock_fs.async_transactional.side_effect = async_transactional_decorator
            mock_transaction = MagicMock()
            mock_transaction.update = MagicMock()
            mock_client.transaction.return_value = mock_transaction
            mock_doc_ref.get = AsyncMock(return_value=mock_snapshot)

            from henchmen.providers.gcp.firestore import FirestoreDocumentStore

            store = FirestoreDocumentStore(mock_settings)
            ok = await store.update_if("queue", "ghost", "status", "pending", {"status": "merging"})
            assert ok is False


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


def _vertex_provider(mock_settings, **overrides):
    """Build a VertexAIProvider with a fully populated tier mapping."""
    mock_settings.vertex_ai_model_complex = overrides.get("complex", "gemini-2.5-pro")
    mock_settings.vertex_ai_model_light = overrides.get("light", "gemini-2.5-flash")
    mock_settings.vertex_ai_model_reasoning = overrides.get("reasoning", "gemini-3.1-pro")
    with patch("henchmen.providers.gcp.vertex_ai.genai", MagicMock()):
        from henchmen.providers.gcp.vertex_ai import VertexAIProvider

        return VertexAIProvider(mock_settings)


def _vertex_response(parts, *, prompt_tokens=100, output_tokens=20, cached=0, finish_reason="STOP"):
    usage = MagicMock()
    usage.prompt_token_count = prompt_tokens
    usage.candidates_token_count = output_tokens
    usage.cached_content_token_count = cached

    candidate = MagicMock()
    candidate.content.parts = parts
    candidate.finish_reason = finish_reason

    response = MagicMock()
    response.candidates = [candidate]
    response.usage_metadata = usage
    return response


def _text_part(text):
    part = MagicMock()
    part.text = text
    part.function_call = None
    return part


class TestVertexAIProvider:
    def test_resolve_tier(self, mock_settings):
        from henchmen.models.llm import ModelTier

        provider = _vertex_provider(mock_settings)
        assert provider.resolve_tier(ModelTier.COMPLEX) == "gemini-2.5-pro"
        assert provider.resolve_tier(ModelTier.LIGHT) == "gemini-2.5-flash"

    def test_resolve_tier_reasoning_uses_its_own_setting(self, mock_settings):
        """REASONING used to silently alias COMPLEX, downgrading fix_tests/analyze_goal."""
        from henchmen.models.llm import ModelTier

        provider = _vertex_provider(mock_settings)
        assert provider.resolve_tier(ModelTier.REASONING) == "gemini-3.1-pro"

    def test_resolve_tier_unknown_passthrough(self, mock_settings):
        provider = _vertex_provider(mock_settings)
        assert provider.resolve_tier("gemini-2.5-pro") == "gemini-2.5-pro"

    def test_resolve_tier_unconfigured_raises(self, mock_settings):
        from henchmen.models.llm import ModelTier

        provider = _vertex_provider(mock_settings, reasoning="")
        with pytest.raises(ValueError, match="No model configured"):
            provider.resolve_tier(ModelTier.REASONING)

    def test_supported_models_comes_from_settings(self, mock_settings):
        provider = _vertex_provider(mock_settings, complex="gemini-4-pro")
        models = provider.supported_models()
        assert models == ["gemini-4-pro", "gemini-2.5-flash", "gemini-3.1-pro"]

    @pytest.mark.asyncio
    async def test_generate_resolves_tier_name(self, mock_settings):
        from henchmen.models.llm import Message, MessageRole, ModelTier

        provider = _vertex_provider(mock_settings)
        generate = AsyncMock(return_value=_vertex_response([_text_part("hi")]))
        provider._client.aio.models.generate_content = generate

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model=ModelTier.REASONING.value,
        )

        assert generate.call_args.kwargs["model"] == "gemini-3.1-pro"
        assert result.model == "gemini-3.1-pro"
        assert result.content == "hi"
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_generate_costs_via_shared_price_table(self, mock_settings):
        from henchmen.models.llm import Message, MessageRole
        from henchmen.providers.pricing import estimate_cost

        provider = _vertex_provider(mock_settings)
        provider._client.aio.models.generate_content = AsyncMock(
            return_value=_vertex_response(
                [_text_part("hi")], prompt_tokens=1_000_000, output_tokens=100_000, cached=500_000
            )
        )

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="gemini-2.5-pro",
        )

        assert result.usage.input_tokens == 1_000_000
        assert result.usage.cached_tokens == 500_000
        assert result.usage.estimated_cost_usd == pytest.approx(
            estimate_cost("gemini-2.5-pro", 1_000_000, 100_000, cached_input_tokens=500_000)
        )

    @pytest.mark.asyncio
    async def test_generate_normalizes_max_tokens_finish_reason(self, mock_settings):
        from henchmen.models.llm import Message, MessageRole

        provider = _vertex_provider(mock_settings)
        provider._client.aio.models.generate_content = AsyncMock(
            return_value=_vertex_response([_text_part("hi")], finish_reason="FinishReason.MAX_TOKENS")
        )

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="gemini-2.5-pro",
        )
        assert result.finish_reason == "max_tokens"

    @pytest.mark.asyncio
    async def test_generate_normalizes_safety_block_as_refusal(self, mock_settings):
        from henchmen.models.llm import Message, MessageRole

        provider = _vertex_provider(mock_settings)
        provider._client.aio.models.generate_content = AsyncMock(
            return_value=_vertex_response([_text_part("")], finish_reason="SAFETY")
        )

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="gemini-2.5-pro",
        )
        assert result.finish_reason == "refusal"

    def test_build_contents_emits_function_calls_and_responses(self, mock_settings):
        """Tool-only assistant turns must not become empty text parts (400 INVALID_ARGUMENT)."""
        from google.genai import types

        from henchmen.models.llm import Message, MessageRole, ToolCall

        provider = _vertex_provider(mock_settings)
        contents = provider._build_contents(
            [
                Message(role=MessageRole.USER, content="Edit main.py"),
                Message(
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_calls=[ToolCall(id="call_1", name="file_edit", arguments={"path": "main.py"})],
                ),
                Message(role=MessageRole.TOOL, content="edited", tool_call_id="call_1"),
            ],
            types,
        )

        assert [c.role for c in contents] == ["user", "model", "user"]
        model_parts = contents[1].parts
        assert len(model_parts) == 1
        assert model_parts[0].text is None
        assert model_parts[0].function_call.name == "file_edit"
        tool_part = contents[2].parts[0]
        assert tool_part.function_response.name == "file_edit"
        assert tool_part.function_response.response == {"result": "edited"}

    def test_build_contents_merges_consecutive_same_role_turns(self, mock_settings):
        from google.genai import types

        from henchmen.models.llm import Message, MessageRole, ToolCall

        provider = _vertex_provider(mock_settings)
        contents = provider._build_contents(
            [
                Message(
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_calls=[
                        ToolCall(id="call_1", name="a", arguments={}),
                        ToolCall(id="call_2", name="b", arguments={}),
                    ],
                ),
                Message(role=MessageRole.TOOL, content="ra", tool_call_id="call_1"),
                Message(role=MessageRole.TOOL, content="rb", tool_call_id="call_2"),
            ],
            types,
        )

        assert [c.role for c in contents] == ["model", "user"]
        assert len(contents[1].parts) == 2

    @pytest.mark.asyncio
    async def test_generate_forwards_enum_and_array_items(self, mock_settings):
        from henchmen.models.llm import Message, MessageRole, ToolDefinition, ToolParameter

        provider = _vertex_provider(mock_settings)
        generate = AsyncMock(return_value=_vertex_response([_text_part("hi")]))
        provider._client.aio.models.generate_content = generate

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="gemini-2.5-pro",
            tools=[
                ToolDefinition(
                    name="file_edit",
                    description="Edit",
                    parameters=[
                        ToolParameter(name="mode", type="string", description="m", enum=["a", "b"]),
                        ToolParameter(name="paths", type="array", description="p"),
                    ],
                )
            ],
        )

        declaration = generate.call_args.kwargs["config"].tools[0].function_declarations[0]
        schema = declaration.parameters
        assert schema.properties["mode"].enum == ["a", "b"]
        # Gemini rejects an ARRAY with no item type.
        assert schema.properties["paths"].items is not None


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

            # Mirrors the real Cloud Run v2 shape: Condition.type_ is the
            # condition name ("Completed"), Condition.state is an enum whose
            # member name carries the outcome ("CONDITION_SUCCEEDED").
            mock_exec_client = AsyncMock()
            mock_condition = MagicMock()
            mock_condition.type_ = "Completed"
            mock_condition.state.name = "CONDITION_SUCCEEDED"
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
