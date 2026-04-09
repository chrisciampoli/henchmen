"""Tests for all 6 AWS provider implementations."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from henchmen.providers.interfaces.ci_provider import CIStatus
from henchmen.providers.interfaces.container_orchestrator import JobStatus

# ---------------------------------------------------------------------------
# boto3 stub — injected into sys.modules so lazy imports inside __init__ work
# ---------------------------------------------------------------------------

_boto3_stub = MagicMock()
_dynamodb_conditions_stub = MagicMock()


def _install_boto3_stub():
    """Install a fresh boto3 MagicMock into sys.modules."""
    stub = MagicMock()
    sys.modules["boto3"] = stub
    sys.modules["boto3.dynamodb"] = MagicMock()
    sys.modules["boto3.dynamodb.conditions"] = _dynamodb_conditions_stub
    return stub


def _remove_aws_modules():
    """Evict any cached AWS provider modules so re-imports pick up new mocks."""
    for key in list(sys.modules):
        if "henchmen.providers.aws" in key:
            del sys.modules[key]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings(**overrides):
    s = MagicMock()
    s.aws_region = "us-east-1"
    s.aws_account_id = "123456789012"
    s.aws_resource_prefix = "henchmen"
    s.aws_dynamodb_table = "henchmen"
    s.aws_ecs_cluster = "henchmen"
    s.aws_ecs_subnets = "subnet-aaa,subnet-bbb"
    s.aws_ecs_security_groups = "sg-xxx"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ---------------------------------------------------------------------------
# SNSMessageBroker
# ---------------------------------------------------------------------------


class TestSNSMessageBroker:
    def setup_method(self):
        _remove_aws_modules()
        self._boto3 = _install_boto3_stub()

    def teardown_method(self):
        _remove_aws_modules()
        sys.modules.pop("boto3", None)

    def test_importable(self):
        from henchmen.providers.aws.sns import SNSMessageBroker

        assert SNSMessageBroker is not None

    def test_init_calls_boto3_client(self):
        mock_client = MagicMock()
        self._boto3.client.return_value = mock_client

        from henchmen.providers.aws.sns import SNSMessageBroker

        broker = SNSMessageBroker(_mock_settings())
        assert broker is not None
        self._boto3.client.assert_called_once_with("sns", region_name="us-east-1")

    def test_topic_arn_construction(self):
        self._boto3.client.return_value = MagicMock()

        from henchmen.providers.aws.sns import SNSMessageBroker

        broker = SNSMessageBroker(_mock_settings())
        arn = broker._topic_arn("task-intake")
        assert arn == "arn:aws:sns:us-east-1:123456789012:henchmen-task-intake"

    @pytest.mark.asyncio
    async def test_publish_returns_message_id(self):
        mock_client = MagicMock()
        mock_client.publish.return_value = {"MessageId": "msg-001"}
        self._boto3.client.return_value = mock_client

        from henchmen.providers.aws.sns import SNSMessageBroker

        broker = SNSMessageBroker(_mock_settings())
        msg_id = await broker.publish("task-intake", b"hello world")
        assert msg_id == "msg-001"

    @pytest.mark.asyncio
    async def test_publish_uses_correct_topic_arn(self):
        mock_client = MagicMock()
        captured: dict = {}

        def capture_publish(**kwargs):
            captured.update(kwargs)
            return {"MessageId": "msg-002"}

        mock_client.publish.side_effect = capture_publish
        self._boto3.client.return_value = mock_client

        from henchmen.providers.aws.sns import SNSMessageBroker

        broker = SNSMessageBroker(_mock_settings())
        await broker.publish("my-topic", b"payload")

        assert captured["TopicArn"] == "arn:aws:sns:us-east-1:123456789012:henchmen-my-topic"
        assert captured["Message"] == "payload"

    @pytest.mark.asyncio
    async def test_publish_with_attributes(self):
        mock_client = MagicMock()
        captured: dict = {}

        def capture_publish(**kwargs):
            captured.update(kwargs)
            return {"MessageId": "msg-003"}

        mock_client.publish.side_effect = capture_publish
        self._boto3.client.return_value = mock_client

        from henchmen.providers.aws.sns import SNSMessageBroker

        broker = SNSMessageBroker(_mock_settings())
        await broker.publish("t", b"data", source="cli", env="dev")

        assert "MessageAttributes" in captured
        assert captured["MessageAttributes"]["source"]["StringValue"] == "cli"

    @pytest.mark.asyncio
    async def test_publish_with_ordering_key(self):
        mock_client = MagicMock()
        captured: dict = {}

        def capture_publish(**kwargs):
            captured.update(kwargs)
            return {"MessageId": "msg-004"}

        mock_client.publish.side_effect = capture_publish
        self._boto3.client.return_value = mock_client

        from henchmen.providers.aws.sns import SNSMessageBroker

        broker = SNSMessageBroker(_mock_settings())
        await broker.publish("t", b"data", ordering_key="group-1")

        assert captured.get("MessageGroupId") == "group-1"


# ---------------------------------------------------------------------------
# DynamoDBDocumentStore
# ---------------------------------------------------------------------------


class TestDynamoDBDocumentStore:
    def setup_method(self):
        _remove_aws_modules()
        self._boto3 = _install_boto3_stub()

    def teardown_method(self):
        _remove_aws_modules()
        sys.modules.pop("boto3", None)

    def _make_store(self, table_mock: MagicMock):
        mock_resource = MagicMock()
        mock_resource.Table.return_value = table_mock
        self._boto3.resource.return_value = mock_resource

        from henchmen.providers.aws.dynamodb import DynamoDBDocumentStore

        return DynamoDBDocumentStore(_mock_settings())

    def test_importable(self):
        from henchmen.providers.aws.dynamodb import DynamoDBDocumentStore

        assert DynamoDBDocumentStore is not None

    def test_init_uses_correct_table(self):
        mock_table = MagicMock()
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        self._boto3.resource.return_value = mock_resource

        from henchmen.providers.aws.dynamodb import DynamoDBDocumentStore

        store = DynamoDBDocumentStore(_mock_settings())
        assert store is not None
        mock_resource.Table.assert_called_once_with("henchmen")

    @pytest.mark.asyncio
    async def test_get_returns_none_when_missing(self):
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}
        store = self._make_store(mock_table)
        result = await store.get("tasks", "nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_document(self):
        data = {"status": "pending", "title": "Fix bug"}
        mock_table = MagicMock()
        mock_table.get_item.return_value = {"Item": {"pk": "tasks", "sk": "t-1", "data": json.dumps(data)}}
        store = self._make_store(mock_table)
        result = await store.get("tasks", "t-1")

        assert result is not None
        assert result["status"] == "pending"
        assert result["title"] == "Fix bug"
        assert result["_id"] == "t-1"

    @pytest.mark.asyncio
    async def test_set_puts_correct_item(self):
        mock_table = MagicMock()
        captured: dict = {}

        def capture_put(**kwargs):
            captured.update(kwargs)
            return {}

        mock_table.put_item.side_effect = capture_put
        store = self._make_store(mock_table)
        await store.set("tasks", "t-2", {"status": "running"})

        item = captured["Item"]
        assert item["pk"] == "tasks"
        assert item["sk"] == "t-2"
        assert json.loads(item["data"]) == {"status": "running"}

    @pytest.mark.asyncio
    async def test_set_strips_id_field(self):
        mock_table = MagicMock()
        captured: dict = {}

        def capture_put(**kwargs):
            captured.update(kwargs)
            return {}

        mock_table.put_item.side_effect = capture_put
        store = self._make_store(mock_table)
        await store.set("tasks", "t-3", {"_id": "t-3", "status": "done"})

        stored = json.loads(captured["Item"]["data"])
        assert "_id" not in stored
        assert stored["status"] == "done"

    @pytest.mark.asyncio
    async def test_update_merges_with_existing(self):
        existing_data = {"status": "pending", "priority": 1}
        get_calls = [0]
        mock_table = MagicMock()

        def mock_get_item(**kwargs):
            get_calls[0] += 1
            if get_calls[0] == 1:
                return {"Item": {"pk": "tasks", "sk": "t-4", "data": json.dumps(existing_data)}}
            return {}

        mock_table.get_item.side_effect = mock_get_item
        put_captured: dict = {}

        def capture_put(**kwargs):
            put_captured.update(kwargs)
            return {}

        mock_table.put_item.side_effect = capture_put
        store = self._make_store(mock_table)
        await store.update("tasks", "t-4", {"status": "running"})

        stored = json.loads(put_captured["Item"]["data"])
        assert stored["status"] == "running"
        assert stored["priority"] == 1

    @pytest.mark.asyncio
    async def test_delete_calls_delete_item(self):
        mock_table = MagicMock()
        mock_table.delete_item.return_value = {}
        store = self._make_store(mock_table)
        await store.delete("tasks", "t-5")
        mock_table.delete_item.assert_called_once_with(Key={"pk": "tasks", "sk": "t-5"})

    @pytest.mark.asyncio
    async def test_query_filters_in_memory(self):
        items = [
            {"pk": "tasks", "sk": "a", "data": json.dumps({"status": "pending"})},
            {"pk": "tasks", "sk": "b", "data": json.dumps({"status": "running"})},
            {"pk": "tasks", "sk": "c", "data": json.dumps({"status": "pending"})},
        ]
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": items}

        # Mock the Key condition expression
        mock_key = MagicMock()
        mock_key.eq.return_value = MagicMock()
        _dynamodb_conditions_stub.Key.return_value = mock_key

        store = self._make_store(mock_table)
        results = await store.query("tasks", filters=[("status", "==", "pending")])

        assert len(results) == 2
        assert all(r["status"] == "pending" for r in results)

    @pytest.mark.asyncio
    async def test_query_with_limit(self):
        items = [{"pk": "tasks", "sk": f"item-{i}", "data": json.dumps({"index": i})} for i in range(5)]
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": items}

        mock_key = MagicMock()
        mock_key.eq.return_value = MagicMock()
        _dynamodb_conditions_stub.Key.return_value = mock_key

        store = self._make_store(mock_table)
        results = await store.query("tasks", limit=3)

        assert len(results) == 3


# ---------------------------------------------------------------------------
# S3ObjectStore
# ---------------------------------------------------------------------------


class TestS3ObjectStore:
    def setup_method(self):
        _remove_aws_modules()
        self._boto3 = _install_boto3_stub()

    def teardown_method(self):
        _remove_aws_modules()
        sys.modules.pop("boto3", None)

    def _make_store(self, client_mock: MagicMock):
        self._boto3.client.return_value = client_mock

        from henchmen.providers.aws.s3 import S3ObjectStore

        return S3ObjectStore(_mock_settings())

    def test_importable(self):
        from henchmen.providers.aws.s3 import S3ObjectStore

        assert S3ObjectStore is not None

    def test_init_creates_client(self):
        mock_client = MagicMock()
        self._boto3.client.return_value = mock_client

        from henchmen.providers.aws.s3 import S3ObjectStore

        store = S3ObjectStore(_mock_settings())
        assert store is not None
        self._boto3.client.assert_called_once_with("s3", region_name="us-east-1")

    @pytest.mark.asyncio
    async def test_put_calls_put_object(self):
        mock_client = MagicMock()
        mock_client.put_object.return_value = {}
        store = self._make_store(mock_client)
        await store.put("my-bucket", "path/to/key", b"data")
        mock_client.put_object.assert_called_once_with(Bucket="my-bucket", Key="path/to/key", Body=b"data")

    @pytest.mark.asyncio
    async def test_get_returns_bytes(self):
        mock_client = MagicMock()
        mock_body = MagicMock()
        mock_body.read.return_value = b"file content"
        mock_client.get_object.return_value = {"Body": mock_body}
        store = self._make_store(mock_client)
        data = await store.get("bucket", "key")
        assert data == b"file content"

    @pytest.mark.asyncio
    async def test_exists_returns_true(self):
        mock_client = MagicMock()
        mock_client.head_object.return_value = {"ContentLength": 10}
        store = self._make_store(mock_client)
        result = await store.exists("bucket", "existing-key")
        assert result is True

    @pytest.mark.asyncio
    async def test_exists_returns_false_on_404(self):
        class _FakeClientError(Exception):
            def __init__(self):
                self.response = {"Error": {"Code": "404", "Message": "Not Found"}}

        mock_client = MagicMock()
        mock_client.head_object.side_effect = _FakeClientError()
        store = self._make_store(mock_client)
        result = await store.exists("bucket", "missing-key")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_calls_delete_object(self):
        mock_client = MagicMock()
        mock_client.delete_object.return_value = {}
        store = self._make_store(mock_client)
        await store.delete("bucket", "old-key")
        mock_client.delete_object.assert_called_once_with(Bucket="bucket", Key="old-key")

    @pytest.mark.asyncio
    async def test_list_keys_returns_keys(self):
        mock_client = MagicMock()
        mock_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "a/file1.json"},
                {"Key": "a/file2.json"},
            ]
        }
        store = self._make_store(mock_client)
        keys = await store.list_keys("bucket", prefix="a/")
        assert keys == ["a/file1.json", "a/file2.json"]

    @pytest.mark.asyncio
    async def test_list_keys_empty(self):
        mock_client = MagicMock()
        mock_client.list_objects_v2.return_value = {}
        store = self._make_store(mock_client)
        keys = await store.list_keys("bucket")
        assert keys == []

    @pytest.mark.asyncio
    async def test_put_file_calls_upload_file(self):
        mock_client = MagicMock()
        mock_client.upload_file.return_value = None
        store = self._make_store(mock_client)
        await store.put_file("bucket", "key", "/tmp/file.txt")
        mock_client.upload_file.assert_called_once_with("/tmp/file.txt", "bucket", "key")

    @pytest.mark.asyncio
    async def test_get_file_calls_download_file(self):
        mock_client = MagicMock()
        mock_client.download_file.return_value = None
        store = self._make_store(mock_client)
        await store.get_file("bucket", "key", "/tmp/out.txt")
        mock_client.download_file.assert_called_once_with("bucket", "key", "/tmp/out.txt")


# ---------------------------------------------------------------------------
# BedrockProvider
# ---------------------------------------------------------------------------


class TestBedrockProvider:
    def setup_method(self):
        _remove_aws_modules()
        self._boto3 = _install_boto3_stub()

    def teardown_method(self):
        _remove_aws_modules()
        sys.modules.pop("boto3", None)

    def _make_provider(self, client_mock: MagicMock):
        self._boto3.client.return_value = client_mock

        from henchmen.providers.aws.bedrock import BedrockProvider

        return BedrockProvider(_mock_settings())

    def test_importable(self):
        from henchmen.providers.aws.bedrock import BedrockProvider

        assert BedrockProvider is not None

    def test_init_creates_bedrock_runtime_client(self):
        mock_client = MagicMock()
        self._boto3.client.return_value = mock_client

        from henchmen.providers.aws.bedrock import BedrockProvider

        provider = BedrockProvider(_mock_settings())
        assert provider is not None
        self._boto3.client.assert_called_once_with("bedrock-runtime", region_name="us-east-1")

    def test_resolve_tier_complex(self):
        from henchmen.models.llm import ModelTier

        provider = self._make_provider(MagicMock())
        model = provider.resolve_tier(ModelTier.COMPLEX)
        assert "claude" in model.lower()
        assert "sonnet" in model.lower()

    def test_resolve_tier_light(self):
        from henchmen.models.llm import ModelTier

        provider = self._make_provider(MagicMock())
        model = provider.resolve_tier(ModelTier.LIGHT)
        assert "claude" in model.lower()
        assert "haiku" in model.lower()

    def test_resolve_tier_passthrough_unknown(self):
        provider = self._make_provider(MagicMock())
        assert provider.resolve_tier("custom-model-id") == "custom-model-id"

    def test_supported_models_not_empty(self):
        provider = self._make_provider(MagicMock())
        models = provider.supported_models()
        assert len(models) > 0
        assert all(isinstance(m, str) for m in models)

    @pytest.mark.asyncio
    async def test_count_tokens_approximation(self):
        provider = self._make_provider(MagicMock())
        count = await provider.count_tokens("hello world", "any-model")
        assert count == 2  # 11 chars // 4

    @pytest.mark.asyncio
    async def test_generate_basic_response(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": "Hello from Bedrock!"}],
                }
            },
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        }
        provider = self._make_provider(mock_client)

        from henchmen.models.llm import Message, MessageRole

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Hi")],
            model="anthropic.claude-sonnet-4-20250514-v1:0",
        )
        assert result.content == "Hello from Bedrock!"
        assert result.finish_reason == "stop"
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5

    @pytest.mark.asyncio
    async def test_generate_with_tool_calls(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "tool-123",
                                "name": "code_edit",
                                "input": {"file": "main.py", "content": "..."},
                            }
                        }
                    ],
                }
            },
            "usage": {"inputTokens": 20, "outputTokens": 8},
            "stopReason": "tool_use",
        }
        provider = self._make_provider(mock_client)

        from henchmen.models.llm import Message, MessageRole

        result = await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Edit the file")],
            model="anthropic.claude-sonnet-4-20250514-v1:0",
        )
        assert result.finish_reason == "tool_use"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "tool-123"
        assert result.tool_calls[0].name == "code_edit"

    @pytest.mark.asyncio
    async def test_generate_with_system_prompt(self):
        mock_client = MagicMock()
        captured: dict = {}

        def capture_converse(**kwargs):
            captured.update(kwargs)
            return {
                "output": {"message": {"role": "assistant", "content": [{"text": "OK"}]}},
                "usage": {"inputTokens": 5, "outputTokens": 1},
                "stopReason": "end_turn",
            }

        mock_client.converse.side_effect = capture_converse
        provider = self._make_provider(mock_client)

        from henchmen.models.llm import Message, MessageRole

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Go")],
            model="anthropic.claude-haiku-4-5-20251001-v1:0",
            system_prompt="You are helpful.",
        )
        assert captured.get("system") == [{"text": "You are helpful."}]

    @pytest.mark.asyncio
    async def test_generate_with_tools_passes_tool_config(self):
        mock_client = MagicMock()
        captured: dict = {}

        def capture_converse(**kwargs):
            captured.update(kwargs)
            return {
                "output": {"message": {"role": "assistant", "content": [{"text": "Done"}]}},
                "usage": {"inputTokens": 5, "outputTokens": 2},
                "stopReason": "end_turn",
            }

        mock_client.converse.side_effect = capture_converse
        provider = self._make_provider(mock_client)

        from henchmen.models.llm import Message, MessageRole, ToolDefinition, ToolParameter

        await provider.generate(
            messages=[Message(role=MessageRole.USER, content="Run tool")],
            model="anthropic.claude-sonnet-4-20250514-v1:0",
            tools=[
                ToolDefinition(
                    name="my_tool",
                    description="Does a thing",
                    parameters=[ToolParameter(name="x", type="string", description="An input")],
                )
            ],
        )
        assert "toolConfig" in captured
        assert captured["toolConfig"]["tools"][0]["toolSpec"]["name"] == "my_tool"


# ---------------------------------------------------------------------------
# ECSOrchestrator
# ---------------------------------------------------------------------------


class TestECSOrchestrator:
    def setup_method(self):
        _remove_aws_modules()
        self._boto3 = _install_boto3_stub()

    def teardown_method(self):
        _remove_aws_modules()
        sys.modules.pop("boto3", None)

    def _make_orch(self, client_mock: MagicMock):
        self._boto3.client.return_value = client_mock

        from henchmen.providers.aws.ecs import ECSOrchestrator

        return ECSOrchestrator(_mock_settings())

    def test_importable(self):
        from henchmen.providers.aws.ecs import ECSOrchestrator

        assert ECSOrchestrator is not None

    def test_init_creates_ecs_client(self):
        mock_client = MagicMock()
        self._boto3.client.return_value = mock_client

        from henchmen.providers.aws.ecs import ECSOrchestrator

        orch = ECSOrchestrator(_mock_settings())
        assert orch is not None
        self._boto3.client.assert_called_once_with("ecs", region_name="us-east-1")

    def test_cpu_conversion(self):
        from henchmen.providers.aws.ecs import _cpu_to_fargate_units

        assert _cpu_to_fargate_units("4") == "4096"
        assert _cpu_to_fargate_units("1") == "1024"
        assert _cpu_to_fargate_units("0.5") == "512"

    def test_memory_conversion(self):
        from henchmen.providers.aws.ecs import _memory_to_mb

        assert _memory_to_mb("8Gi") == "8192"
        assert _memory_to_mb("2Gi") == "2048"
        assert _memory_to_mb("512Mi") == "512"
        assert _memory_to_mb("4096") == "4096"

    @pytest.mark.asyncio
    async def test_run_job_returns_task_arn(self):
        mock_client = MagicMock()
        mock_client.register_task_definition.return_value = {
            "taskDefinition": {"taskDefinitionArn": "arn:aws:ecs:us-east-1:123:task-definition/henchmen-job-1:1"}
        }
        mock_client.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-east-1:123:task/henchmen/abc123"}],
            "failures": [],
        }
        orch = self._make_orch(mock_client)
        task_arn = await orch.run_job(
            job_id="job-1",
            image="nginx:latest",
            env_vars={"KEY": "VALUE"},
        )
        assert task_arn == "arn:aws:ecs:us-east-1:123:task/henchmen/abc123"

    @pytest.mark.asyncio
    async def test_run_job_raises_on_no_tasks(self):
        mock_client = MagicMock()
        mock_client.register_task_definition.return_value = {
            "taskDefinition": {"taskDefinitionArn": "arn:aws:ecs:us-east-1:123:task-definition/henchmen-job-2:1"}
        }
        mock_client.run_task.return_value = {
            "tasks": [],
            "failures": [{"reason": "AGENT"}],
        }
        orch = self._make_orch(mock_client)
        with pytest.raises(RuntimeError, match="AGENT"):
            await orch.run_job(job_id="job-2", image="nginx:latest", env_vars={})

    @pytest.mark.asyncio
    async def test_get_status_running(self):
        mock_client = MagicMock()
        mock_client.describe_tasks.return_value = {"tasks": [{"lastStatus": "RUNNING", "containers": []}]}
        orch = self._make_orch(mock_client)
        result = await orch.get_status("arn:aws:ecs:us-east-1:123:task/henchmen/abc")
        assert result.status == JobStatus.RUNNING

    @pytest.mark.asyncio
    async def test_get_status_stopped_success(self):
        mock_client = MagicMock()
        mock_client.describe_tasks.return_value = {
            "tasks": [{"lastStatus": "STOPPED", "containers": [{"exitCode": 0}]}]
        }
        orch = self._make_orch(mock_client)
        result = await orch.get_status("arn:aws:ecs:us-east-1:123:task/abc")
        assert result.status == JobStatus.COMPLETED
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_get_status_stopped_failure(self):
        mock_client = MagicMock()
        mock_client.describe_tasks.return_value = {
            "tasks": [{"lastStatus": "STOPPED", "containers": [{"exitCode": 1}]}]
        }
        orch = self._make_orch(mock_client)
        result = await orch.get_status("arn:aws:ecs:us-east-1:123:task/abc")
        assert result.status == JobStatus.FAILED
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_get_status_no_tasks(self):
        mock_client = MagicMock()
        mock_client.describe_tasks.return_value = {"tasks": []}
        orch = self._make_orch(mock_client)
        result = await orch.get_status("arn:aws:ecs:unknown")
        assert result.status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_cancel_calls_stop_task(self):
        mock_client = MagicMock()
        mock_client.stop_task.return_value = {}
        orch = self._make_orch(mock_client)
        await orch.cancel("arn:aws:ecs:us-east-1:123:task/abc")
        mock_client.stop_task.assert_called_once_with(
            cluster="henchmen",
            task="arn:aws:ecs:us-east-1:123:task/abc",
            reason="Cancelled by Henchmen",
        )

    @pytest.mark.asyncio
    async def test_stream_logs_is_async_generator(self):
        orch = self._make_orch(MagicMock())
        gen = orch.stream_logs("task-arn")
        items = []
        async for item in gen:
            items.append(item)
        assert items == []


# ---------------------------------------------------------------------------
# CodeBuildCIProvider
# ---------------------------------------------------------------------------


class TestCodeBuildCIProvider:
    def setup_method(self):
        _remove_aws_modules()
        self._boto3 = _install_boto3_stub()

    def teardown_method(self):
        _remove_aws_modules()
        sys.modules.pop("boto3", None)

    def _make_provider(self, client_mock: MagicMock):
        self._boto3.client.return_value = client_mock

        from henchmen.providers.aws.codebuild import CodeBuildCIProvider

        return CodeBuildCIProvider(_mock_settings())

    def test_importable(self):
        from henchmen.providers.aws.codebuild import CodeBuildCIProvider

        assert CodeBuildCIProvider is not None

    def test_init_creates_codebuild_client(self):
        mock_client = MagicMock()
        self._boto3.client.return_value = mock_client

        from henchmen.providers.aws.codebuild import CodeBuildCIProvider

        provider = CodeBuildCIProvider(_mock_settings())
        assert provider is not None
        self._boto3.client.assert_called_once_with("codebuild", region_name="us-east-1")

    def test_init_without_settings(self):
        self._boto3.client.return_value = MagicMock()

        from henchmen.providers.aws.codebuild import CodeBuildCIProvider

        provider = CodeBuildCIProvider()
        assert provider is not None

    def test_buildspec_generation(self):
        from henchmen.providers.aws.codebuild import _build_buildspec

        spec = _build_buildspec(
            "https://github.com/example/repo",
            "main",
            ["npm install", "npm test"],
        )
        assert "git clone" in spec
        assert "npm install" in spec
        assert "npm test" in spec

    @pytest.mark.asyncio
    async def test_trigger_build_returns_id(self):
        mock_client = MagicMock()
        mock_client.start_build.return_value = {"build": {"id": "henchmen-ci:build-001"}}
        provider = self._make_provider(mock_client)
        build_id = await provider.trigger_build(
            repo_url="https://github.com/example/repo",
            branch="main",
            commands=["npm test"],
        )
        assert build_id == "henchmen-ci:build-001"

    @pytest.mark.asyncio
    async def test_trigger_build_passes_buildspec(self):
        mock_client = MagicMock()
        captured: dict = {}

        def capture_start(**kwargs):
            captured.update(kwargs)
            return {"build": {"id": "henchmen-ci:b-002"}}

        mock_client.start_build.side_effect = capture_start
        provider = self._make_provider(mock_client)
        await provider.trigger_build(
            repo_url="https://github.com/example/repo",
            branch="feat/test",
            commands=["make test"],
        )

        assert "buildspecOverride" in captured
        assert "make test" in captured["buildspecOverride"]

    @pytest.mark.asyncio
    async def test_get_status_success(self):
        mock_client = MagicMock()
        mock_client.batch_get_builds.return_value = {
            "builds": [
                {
                    "id": "b-001",
                    "buildStatus": "SUCCEEDED",
                    "logs": {"deepLink": "https://console.aws.amazon.com/..."},
                }
            ]
        }
        provider = self._make_provider(mock_client)
        result = await provider.get_status("b-001")
        assert result.status == CIStatus.SUCCESS
        assert result.logs_url is not None

    @pytest.mark.asyncio
    async def test_get_status_failure(self):
        mock_client = MagicMock()
        mock_client.batch_get_builds.return_value = {
            "builds": [
                {
                    "id": "b-002",
                    "buildStatus": "FAILED",
                    "logs": {},
                    "phases": [
                        {
                            "phaseStatus": "FAILED",
                            "contexts": [{"message": "Tests failed"}],
                        }
                    ],
                }
            ]
        }
        provider = self._make_provider(mock_client)
        result = await provider.get_status("b-002")
        assert result.status == CIStatus.FAILURE
        assert result.error_message == "Tests failed"

    @pytest.mark.asyncio
    async def test_get_status_build_not_found(self):
        mock_client = MagicMock()
        mock_client.batch_get_builds.return_value = {"builds": []}
        provider = self._make_provider(mock_client)
        result = await provider.get_status("nonexistent")
        assert result.status == CIStatus.FAILURE
        assert result.error_message == "Build not found"

    @pytest.mark.asyncio
    async def test_get_status_in_progress(self):
        mock_client = MagicMock()
        mock_client.batch_get_builds.return_value = {
            "builds": [{"id": "b-003", "buildStatus": "IN_PROGRESS", "logs": {}}]
        }
        provider = self._make_provider(mock_client)
        result = await provider.get_status("b-003")
        assert result.status == CIStatus.RUNNING

    @pytest.mark.asyncio
    async def test_get_status_with_duration(self):
        start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        end = start + timedelta(seconds=120)
        mock_client = MagicMock()
        mock_client.batch_get_builds.return_value = {
            "builds": [
                {
                    "id": "b-004",
                    "buildStatus": "SUCCEEDED",
                    "logs": {},
                    "startTime": start,
                    "endTime": end,
                }
            ]
        }
        provider = self._make_provider(mock_client)
        result = await provider.get_status("b-004")
        assert result.duration_seconds == 120.0

    @pytest.mark.asyncio
    async def test_get_logs_returns_url(self):
        mock_client = MagicMock()
        mock_client.batch_get_builds.return_value = {
            "builds": [{"id": "b-005", "buildStatus": "SUCCEEDED", "logs": {"deepLink": "https://cw.example.com"}}]
        }
        provider = self._make_provider(mock_client)
        logs_url = await provider.get_logs("b-005")
        assert logs_url == "https://cw.example.com"

    @pytest.mark.asyncio
    async def test_get_logs_fallback_url(self):
        mock_client = MagicMock()
        mock_client.batch_get_builds.return_value = {"builds": []}
        provider = self._make_provider(mock_client)
        logs_url = await provider.get_logs("b-missing")
        assert "codebuild" in logs_url.lower() or "b-missing" in logs_url

    @pytest.mark.asyncio
    async def test_cancel_calls_stop_build(self):
        mock_client = MagicMock()
        mock_client.stop_build.return_value = {}
        provider = self._make_provider(mock_client)
        await provider.cancel("b-running")
        mock_client.stop_build.assert_called_once_with(id="b-running")


# ---------------------------------------------------------------------------
# Settings — AWS fields
# ---------------------------------------------------------------------------


class TestAWSSettings:
    def test_aws_settings_have_defaults(self):
        with patch.dict(
            "os.environ",
            {"HENCHMEN_GCP_PROJECT_ID": "test-project"},
            clear=False,
        ):
            from henchmen.config.settings import Settings

            s = Settings()
            assert s.aws_region == "us-east-1"
            assert s.aws_account_id == ""
            assert s.aws_resource_prefix == "henchmen"
            assert s.aws_dynamodb_table == "henchmen"
            assert s.aws_ecs_cluster == "henchmen"
            assert s.aws_ecs_subnets == ""
            assert s.aws_ecs_security_groups == ""

    def test_aws_settings_overridable_via_env(self):
        with patch.dict(
            "os.environ",
            {
                "HENCHMEN_GCP_PROJECT_ID": "test-project",
                "HENCHMEN_AWS_REGION": "eu-west-1",
                "HENCHMEN_AWS_ACCOUNT_ID": "999888777666",
                "HENCHMEN_AWS_ECS_CLUSTER": "my-cluster",
            },
            clear=False,
        ):
            from henchmen.config.settings import Settings

            s = Settings()
            assert s.aws_region == "eu-west-1"
            assert s.aws_account_id == "999888777666"
            assert s.aws_ecs_cluster == "my-cluster"
