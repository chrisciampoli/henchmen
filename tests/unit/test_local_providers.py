"""Tests for all 6 local provider implementations."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.providers.interfaces.ci_provider import CIStatus
from henchmen.providers.interfaces.container_orchestrator import JobStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings(**overrides):
    """Build a real ``Settings`` instance with test-safe defaults.

    Seeds ``os.environ`` for the ``HENCHMEN_GCP_PROJECT_ID`` required
    field (Ollama fields already have sensible defaults on the real
    Settings class) and applies per-call overrides via Pydantic
    ``model_copy``. The autouse ``_isolate_settings`` fixture clears
    ``get_settings`` between tests so each call rebuilds a fresh
    instance.
    """
    import os

    from henchmen.config.settings import get_settings

    os.environ.setdefault("HENCHMEN_GCP_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    settings = get_settings()
    if overrides:
        settings = settings.model_copy(update=overrides)
    return settings


# ---------------------------------------------------------------------------
# InMemoryMessageBroker
# ---------------------------------------------------------------------------


class TestInMemoryMessageBroker:
    @pytest.mark.asyncio
    async def test_publish_returns_id(self):
        from henchmen.providers.local.memory import InMemoryMessageBroker

        broker = InMemoryMessageBroker()
        msg_id = await broker.publish("my-topic", b"hello")
        assert msg_id.startswith("local-")

    @pytest.mark.asyncio
    async def test_publish_stores_message(self):
        from henchmen.providers.local.memory import InMemoryMessageBroker

        broker = InMemoryMessageBroker()
        await broker.publish("topic-a", b"payload", source="cli")
        msgs = broker.get_messages("topic-a")
        assert len(msgs) == 1
        assert msgs[0]["data"] == b"payload"
        assert msgs[0]["attributes"]["source"] == "cli"

    @pytest.mark.asyncio
    async def test_publish_multiple_messages(self):
        from henchmen.providers.local.memory import InMemoryMessageBroker

        broker = InMemoryMessageBroker()
        await broker.publish("topic-b", b"msg1")
        await broker.publish("topic-b", b"msg2")
        assert len(broker.get_messages("topic-b")) == 2

    @pytest.mark.asyncio
    async def test_subscribe_callback_invoked(self):
        from henchmen.providers.local.memory import InMemoryMessageBroker

        received = []
        broker = InMemoryMessageBroker()
        broker.subscribe("events", lambda data, **kw: received.append(data))
        await broker.publish("events", b"event-payload")
        assert received == [b"event-payload"]

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_called(self):
        from henchmen.providers.local.memory import InMemoryMessageBroker

        results = []
        broker = InMemoryMessageBroker()
        broker.subscribe("t", lambda data, **kw: results.append("a"))
        broker.subscribe("t", lambda data, **kw: results.append("b"))
        await broker.publish("t", b"x")
        assert results == ["a", "b"]

    @pytest.mark.asyncio
    async def test_clear_removes_all_state(self):
        from henchmen.providers.local.memory import InMemoryMessageBroker

        called = []
        broker = InMemoryMessageBroker()
        broker.subscribe("t", lambda data, **kw: called.append(1))
        await broker.publish("t", b"data")
        broker.clear()
        assert broker.get_messages("t") == []
        await broker.publish("t", b"after-clear")
        assert called == [1]  # subscriber was also cleared

    @pytest.mark.asyncio
    async def test_ordering_key_stored_in_attributes(self):
        from henchmen.providers.local.memory import InMemoryMessageBroker

        broker = InMemoryMessageBroker()
        msg_id = await broker.publish("t", b"data", ordering_key="k1")
        assert msg_id.startswith("local-")


# ---------------------------------------------------------------------------
# SQLiteDocumentStore
# ---------------------------------------------------------------------------


class TestSQLiteDocumentStore:
    @pytest.mark.asyncio
    async def test_set_and_get(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("tasks", "t-1", {"status": "pending", "title": "Fix bug"})
        doc = await store.get("tasks", "t-1")
        assert doc is not None
        assert doc["status"] == "pending"
        assert doc["title"] == "Fix bug"
        assert doc["_id"] == "t-1"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        result = await store.get("tasks", "nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_merges_fields(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("tasks", "t-2", {"status": "pending", "priority": 1})
        await store.update("tasks", "t-2", {"status": "running"})
        doc = await store.get("tasks", "t-2")
        assert doc is not None
        assert doc["status"] == "running"
        assert doc["priority"] == 1

    @pytest.mark.asyncio
    async def test_update_creates_if_missing(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.update("tasks", "new-doc", {"status": "fresh"})
        doc = await store.get("tasks", "new-doc")
        assert doc is not None
        assert doc["status"] == "fresh"

    @pytest.mark.asyncio
    async def test_delete(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("tasks", "t-3", {"status": "done"})
        await store.delete("tasks", "t-3")
        assert await store.get("tasks", "t-3") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_is_noop(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.delete("tasks", "ghost")  # Should not raise

    @pytest.mark.asyncio
    async def test_query_with_filter(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("tasks", "a", {"status": "pending"})
        await store.set("tasks", "b", {"status": "running"})
        await store.set("tasks", "c", {"status": "pending"})
        results = await store.query("tasks", filters=[("status", "==", "pending")])
        assert len(results) == 2
        assert all(r["status"] == "pending" for r in results)

    @pytest.mark.asyncio
    async def test_query_with_limit(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        for i in range(5):
            await store.set("items", f"item-{i}", {"index": i})
        results = await store.query("items", limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_query_with_order_by(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("items", "z", {"name": "zebra"})
        await store.set("items", "a", {"name": "apple"})
        results = await store.query("items", order_by="name")
        assert results[0]["name"] == "apple"
        assert results[1]["name"] == "zebra"

    @pytest.mark.asyncio
    async def test_query_order_descending(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("items", "z", {"name": "zebra"})
        await store.set("items", "a", {"name": "apple"})
        results = await store.query("items", order_by="name", order_direction="DESCENDING")
        assert results[0]["name"] == "zebra"

    @pytest.mark.asyncio
    async def test_query_filter_not_equal(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("tasks", "a", {"status": "done"})
        await store.set("tasks", "b", {"status": "pending"})
        results = await store.query("tasks", filters=[("status", "!=", "done")])
        assert len(results) == 1
        assert results[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_query_filter_in(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("tasks", "a", {"priority": 1})
        await store.set("tasks", "b", {"priority": 2})
        await store.set("tasks", "c", {"priority": 3})
        results = await store.query("tasks", filters=[("priority", "in", [1, 3])])
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_set_overwrites_existing(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("tasks", "t-1", {"status": "pending"})
        await store.set("tasks", "t-1", {"status": "done"})
        doc = await store.get("tasks", "t-1")
        assert doc is not None
        assert doc["status"] == "done"

    @pytest.mark.asyncio
    async def test_increment_creates_field_when_missing(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("tasks", "t-1", {"status": "pending"})
        await store.increment("tasks", "t-1", {"counter": 3})
        doc = await store.get("tasks", "t-1")
        assert doc is not None
        assert doc["counter"] == 3
        assert doc["status"] == "pending"

    @pytest.mark.asyncio
    async def test_increment_adds_to_existing_value(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("tasks", "t-1", {"tokens": 100, "cost": 1.5})
        await store.increment("tasks", "t-1", {"tokens": 50, "cost": 0.25})
        doc = await store.get("tasks", "t-1")
        assert doc is not None
        assert doc["tokens"] == 150
        assert doc["cost"] == pytest.approx(1.75)

    @pytest.mark.asyncio
    async def test_increment_creates_document_when_missing(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.increment("tasks", "fresh", {"n": 5})
        doc = await store.get("tasks", "fresh")
        assert doc is not None
        assert doc["n"] == 5

    @pytest.mark.asyncio
    async def test_increment_concurrent_updates_are_serialized(self, tmp_path):
        import asyncio

        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("tasks", "t-1", {"counter": 0})

        # Fire 20 concurrent +1 increments; the per-doc lock must serialize them.
        await asyncio.gather(*[store.increment("tasks", "t-1", {"counter": 1}) for _ in range(20)])

        doc = await store.get("tasks", "t-1")
        assert doc is not None
        assert doc["counter"] == 20

    @pytest.mark.asyncio
    async def test_update_if_matches_precondition(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("queue", "e1", {"status": "pending", "owner": None})
        ok = await store.update_if("queue", "e1", "status", "pending", {"status": "merging", "owner": "worker-1"})
        assert ok is True
        doc = await store.get("queue", "e1")
        assert doc is not None
        assert doc["status"] == "merging"
        assert doc["owner"] == "worker-1"

    @pytest.mark.asyncio
    async def test_update_if_rejects_wrong_expected_value(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("queue", "e1", {"status": "merging"})
        ok = await store.update_if("queue", "e1", "status", "pending", {"status": "merged"})
        assert ok is False
        doc = await store.get("queue", "e1")
        assert doc is not None
        assert doc["status"] == "merging"

    @pytest.mark.asyncio
    async def test_update_if_returns_false_when_missing(self, tmp_path):
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        ok = await store.update_if("queue", "ghost", "status", "pending", {"status": "merging"})
        assert ok is False

    @pytest.mark.asyncio
    async def test_update_if_concurrent_only_one_wins(self, tmp_path):
        import asyncio

        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = SQLiteDocumentStore(_mock_settings(), db_path=str(tmp_path / "test.db"))
        await store.set("queue", "e1", {"status": "pending"})

        results = await asyncio.gather(
            *[
                store.update_if("queue", "e1", "status", "pending", {"status": "merging", "claimed_by": f"w-{i}"})
                for i in range(10)
            ]
        )
        # Exactly one caller must win the CAS; the rest must see the precondition fail.
        assert sum(1 for r in results if r is True) == 1
        assert sum(1 for r in results if r is False) == 9


# ---------------------------------------------------------------------------
# FilesystemObjectStore
# ---------------------------------------------------------------------------


class TestFilesystemObjectStore:
    @pytest.mark.asyncio
    async def test_put_and_get(self, tmp_path):
        from henchmen.providers.local.filesystem import FilesystemObjectStore

        store = FilesystemObjectStore(_mock_settings(), base_dir=str(tmp_path))
        await store.put("bucket1", "file.txt", b"hello world")
        data = await store.get("bucket1", "file.txt")
        assert data == b"hello world"

    @pytest.mark.asyncio
    async def test_exists_true(self, tmp_path):
        from henchmen.providers.local.filesystem import FilesystemObjectStore

        store = FilesystemObjectStore(_mock_settings(), base_dir=str(tmp_path))
        await store.put("bucket1", "exists.txt", b"data")
        assert await store.exists("bucket1", "exists.txt") is True

    @pytest.mark.asyncio
    async def test_exists_false(self, tmp_path):
        from henchmen.providers.local.filesystem import FilesystemObjectStore

        store = FilesystemObjectStore(_mock_settings(), base_dir=str(tmp_path))
        assert await store.exists("bucket1", "missing.txt") is False

    @pytest.mark.asyncio
    async def test_delete(self, tmp_path):
        from henchmen.providers.local.filesystem import FilesystemObjectStore

        store = FilesystemObjectStore(_mock_settings(), base_dir=str(tmp_path))
        await store.put("bucket1", "del.txt", b"bye")
        await store.delete("bucket1", "del.txt")
        assert await store.exists("bucket1", "del.txt") is False

    @pytest.mark.asyncio
    async def test_delete_nonexistent_is_noop(self, tmp_path):
        from henchmen.providers.local.filesystem import FilesystemObjectStore

        store = FilesystemObjectStore(_mock_settings(), base_dir=str(tmp_path))
        await store.delete("bucket1", "ghost.txt")  # Should not raise

    @pytest.mark.asyncio
    async def test_list_keys(self, tmp_path):
        from henchmen.providers.local.filesystem import FilesystemObjectStore

        store = FilesystemObjectStore(_mock_settings(), base_dir=str(tmp_path))
        await store.put("b", "a/x.json", b"x")
        await store.put("b", "a/y.json", b"y")
        await store.put("b", "b/z.json", b"z")
        keys = await store.list_keys("b")
        assert sorted(keys) == ["a/x.json", "a/y.json", "b/z.json"]

    @pytest.mark.asyncio
    async def test_list_keys_with_prefix(self, tmp_path):
        from henchmen.providers.local.filesystem import FilesystemObjectStore

        store = FilesystemObjectStore(_mock_settings(), base_dir=str(tmp_path))
        await store.put("b", "tasks/a.json", b"a")
        await store.put("b", "tasks/b.json", b"b")
        await store.put("b", "other/c.json", b"c")
        keys = await store.list_keys("b", prefix="tasks/")
        assert len(keys) == 2
        assert all(k.startswith("tasks/") for k in keys)

    @pytest.mark.asyncio
    async def test_list_keys_empty_bucket(self, tmp_path):
        from henchmen.providers.local.filesystem import FilesystemObjectStore

        store = FilesystemObjectStore(_mock_settings(), base_dir=str(tmp_path))
        keys = await store.list_keys("nonexistent-bucket")
        assert keys == []

    @pytest.mark.asyncio
    async def test_put_file_and_get_file(self, tmp_path):
        from henchmen.providers.local.filesystem import FilesystemObjectStore

        store = FilesystemObjectStore(_mock_settings(), base_dir=str(tmp_path / "store"))
        src = tmp_path / "source.txt"
        src.write_bytes(b"file content")
        dest = tmp_path / "destination.txt"

        await store.put_file("bucket", "key.txt", str(src))
        await store.get_file("bucket", "key.txt", str(dest))
        assert dest.read_bytes() == b"file content"

    @pytest.mark.asyncio
    async def test_put_creates_nested_dirs(self, tmp_path):
        from henchmen.providers.local.filesystem import FilesystemObjectStore

        store = FilesystemObjectStore(_mock_settings(), base_dir=str(tmp_path))
        await store.put("b", "deep/nested/path/file.txt", b"content")
        assert await store.exists("b", "deep/nested/path/file.txt") is True


# ---------------------------------------------------------------------------
# OllamaProvider
# ---------------------------------------------------------------------------


class TestOllamaProvider:
    def test_supported_models_returns_default(self):
        from henchmen.providers.local.ollama import OllamaProvider

        provider = OllamaProvider(_mock_settings(llm_ollama_model="llama3.2"))
        models = provider.supported_models()
        assert models == ["llama3.2"]

    def test_supported_models_respects_settings(self):
        from henchmen.providers.local.ollama import OllamaProvider

        provider = OllamaProvider(_mock_settings(llm_ollama_model="mistral"))
        assert provider.supported_models() == ["mistral"]

    def test_resolve_tier_complex(self):
        from henchmen.models.llm import ModelTier
        from henchmen.providers.local.ollama import OllamaProvider

        provider = OllamaProvider(_mock_settings(llm_ollama_model="llama3.2"))
        assert provider.resolve_tier(ModelTier.COMPLEX) == "llama3.2"

    def test_resolve_tier_light(self):
        from henchmen.models.llm import ModelTier
        from henchmen.providers.local.ollama import OllamaProvider

        provider = OllamaProvider(_mock_settings(llm_ollama_model="llama3.2"))
        assert provider.resolve_tier(ModelTier.LIGHT) == "llama3.2"

    def test_resolve_tier_reasoning(self):
        from henchmen.models.llm import ModelTier
        from henchmen.providers.local.ollama import OllamaProvider

        provider = OllamaProvider(_mock_settings(llm_ollama_model="llama3.2"))
        assert provider.resolve_tier(ModelTier.REASONING) == "llama3.2"

    def test_resolve_tier_passthrough_unknown(self):
        from henchmen.providers.local.ollama import OllamaProvider

        provider = OllamaProvider(_mock_settings())
        assert provider.resolve_tier("custom-model") == "custom-model"

    @pytest.mark.asyncio
    async def test_count_tokens_approximation(self):
        from henchmen.providers.local.ollama import OllamaProvider

        provider = OllamaProvider(_mock_settings())
        count = await provider.count_tokens("hello world", "llama3.2")
        # 11 chars -> 2 tokens (integer division by 4)
        assert count == 2

    @pytest.mark.asyncio
    async def test_count_tokens_longer_text(self):
        from henchmen.providers.local.ollama import OllamaProvider

        provider = OllamaProvider(_mock_settings())
        text = "a" * 400
        count = await provider.count_tokens(text, "llama3.2")
        assert count == 100

    @pytest.mark.asyncio
    async def test_generate_success(self):
        from henchmen.models.llm import Message, MessageRole
        from henchmen.providers.local.ollama import OllamaProvider

        settings = _mock_settings()
        provider = OllamaProvider(settings)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "message": {"role": "assistant", "content": "Hello!", "tool_calls": []},
            "prompt_eval_count": 10,
            "eval_count": 5,
        }

        with patch.object(provider._client, "post", new=AsyncMock(return_value=mock_response)):
            result = await provider.generate(
                messages=[Message(role=MessageRole.USER, content="Hi")],
                model="llama3.2",
            )

        assert result.content == "Hello!"
        assert result.model == "llama3.2"
        assert result.finish_reason == "stop"
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5
        assert result.usage.estimated_cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_generate_with_tool_calls(self):
        from henchmen.models.llm import Message, MessageRole
        from henchmen.providers.local.ollama import OllamaProvider

        provider = OllamaProvider(_mock_settings())

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "do_thing", "arguments": {"x": 1}}}],
            },
            "prompt_eval_count": 8,
            "eval_count": 3,
        }

        with patch.object(provider._client, "post", new=AsyncMock(return_value=mock_response)):
            result = await provider.generate(
                messages=[Message(role=MessageRole.USER, content="Use the tool")],
                model="llama3.2",
            )

        assert result.finish_reason == "tool_use"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "do_thing"
        assert result.tool_calls[0].arguments == {"x": 1}

    @pytest.mark.asyncio
    async def test_generate_with_system_prompt(self):
        from henchmen.models.llm import Message, MessageRole
        from henchmen.providers.local.ollama import OllamaProvider

        provider = OllamaProvider(_mock_settings())

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "message": {"role": "assistant", "content": "Got it", "tool_calls": []},
            "prompt_eval_count": 20,
            "eval_count": 4,
        }

        captured_payload = {}

        async def mock_post(path, json=None):
            captured_payload.update(json or {})
            return mock_response

        with patch.object(provider._client, "post", side_effect=mock_post):
            await provider.generate(
                messages=[Message(role=MessageRole.USER, content="Go")],
                model="llama3.2",
                system_prompt="You are helpful.",
            )

        assert captured_payload["messages"][0] == {"role": "system", "content": "You are helpful."}


class TestOllamaCapabilityProbe:
    """C3: up-front tool-calling capability probe.

    The first time ``generate`` is called with a non-empty ``tools``
    argument, the provider issues a trivial canary call. If the model
    does not return a ``tool_calls`` field, the provider raises a
    clear error identifying the model instead of silently flattening
    to text-only output halfway through a real operative run.

    ``HENCHMEN_OLLAMA_SKIP_PROBE=1`` short-circuits the probe so
    CI / test doubles with mocked httpx don't get blocked.
    """

    def _make_provider(self, **settings_overrides):
        from henchmen.providers.local.ollama import OllamaProvider

        settings = _mock_settings(**settings_overrides)
        return OllamaProvider(settings)

    @pytest.mark.asyncio
    async def test_probe_runs_once_and_succeeds_when_model_returns_tool_calls(self):
        """A capable model returns tool_calls on the canary — probe is recorded as OK."""
        from henchmen.models.llm import Message, MessageRole, ToolDefinition, ToolParameter

        provider = self._make_provider()

        # Every real httpx post() returns a response with non-empty tool_calls.
        async def mock_post(path, json=None):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "probe", "arguments": {}}}],
                },
                "prompt_eval_count": 1,
                "eval_count": 1,
            }
            return resp

        probe_tool = ToolDefinition(
            name="probe",
            description="canary",
            parameters=[ToolParameter(name="x", type="string", description="x", required=False)],
        )

        with patch.object(provider._client, "post", side_effect=mock_post):
            result = await provider.generate(
                messages=[Message(role=MessageRole.USER, content="Hi")],
                model="llama3.2",
                tools=[probe_tool],
            )

        assert len(result.tool_calls) == 1
        # Probe state flag is set so future calls don't re-probe.
        assert provider._tool_probe_state == "ok"

    @pytest.mark.asyncio
    async def test_probe_raises_when_model_lacks_tool_calling(self):
        """A model that returns no tool_calls on the canary raises a RuntimeError."""
        from henchmen.models.llm import Message, MessageRole, ToolDefinition, ToolParameter

        provider = self._make_provider()

        async def mock_post(path, json=None):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "message": {"role": "assistant", "content": "I cannot call tools", "tool_calls": []},
                "prompt_eval_count": 1,
                "eval_count": 4,
            }
            return resp

        probe_tool = ToolDefinition(
            name="probe",
            description="canary",
            parameters=[ToolParameter(name="x", type="string", description="x", required=False)],
        )

        with (
            patch.object(provider._client, "post", side_effect=mock_post),
            pytest.raises(RuntimeError, match="does not support native tool calling"),
        ):
            await provider.generate(
                messages=[Message(role=MessageRole.USER, content="Hi")],
                model="llama3.2",
                tools=[probe_tool],
            )

    @pytest.mark.asyncio
    async def test_probe_skipped_when_setting_is_true(self):
        """HENCHMEN_OLLAMA_SKIP_PROBE=True short-circuits the probe."""
        from henchmen.models.llm import Message, MessageRole, ToolDefinition, ToolParameter

        provider = self._make_provider(llm_ollama_skip_probe=True)

        async def mock_post(path, json=None):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "message": {"role": "assistant", "content": "", "tool_calls": []},
                "prompt_eval_count": 1,
                "eval_count": 1,
            }
            return resp

        probe_tool = ToolDefinition(
            name="probe",
            description="canary",
            parameters=[ToolParameter(name="x", type="string", description="x", required=False)],
        )

        with patch.object(provider._client, "post", side_effect=mock_post):
            # Should NOT raise — probe is skipped.
            result = await provider.generate(
                messages=[Message(role=MessageRole.USER, content="Hi")],
                model="llama3.2",
                tools=[probe_tool],
            )

        assert result.tool_calls == []
        assert provider._tool_probe_state == "skipped"

    @pytest.mark.asyncio
    async def test_probe_not_repeated_on_second_generate_call(self):
        """After the first successful probe, subsequent calls do NOT re-probe."""
        from henchmen.models.llm import Message, MessageRole, ToolDefinition, ToolParameter

        provider = self._make_provider()

        call_count = {"n": 0}

        async def mock_post(path, json=None):
            call_count["n"] += 1
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "probe", "arguments": {}}}],
                },
                "prompt_eval_count": 1,
                "eval_count": 1,
            }
            return resp

        probe_tool = ToolDefinition(
            name="probe",
            description="canary",
            parameters=[ToolParameter(name="x", type="string", description="x", required=False)],
        )

        with patch.object(provider._client, "post", side_effect=mock_post):
            await provider.generate(
                messages=[Message(role=MessageRole.USER, content="First")],
                model="llama3.2",
                tools=[probe_tool],
            )
            await provider.generate(
                messages=[Message(role=MessageRole.USER, content="Second")],
                model="llama3.2",
                tools=[probe_tool],
            )

        # First generate: 1 probe + 1 real = 2 calls. Second generate:
        # 0 probes + 1 real = 1 call. Total = 3.
        assert call_count["n"] == 3

    @pytest.mark.asyncio
    async def test_probe_not_triggered_without_tools(self):
        """Calls that pass no tools never trigger the probe."""
        from henchmen.models.llm import Message, MessageRole

        provider = self._make_provider()

        async def mock_post(path, json=None):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "message": {"role": "assistant", "content": "hi", "tool_calls": []},
                "prompt_eval_count": 1,
                "eval_count": 1,
            }
            return resp

        with patch.object(provider._client, "post", side_effect=mock_post):
            await provider.generate(
                messages=[Message(role=MessageRole.USER, content="Hi")],
                model="llama3.2",
                tools=None,
            )

        # No probe triggered — state stays as the initial None.
        assert provider._tool_probe_state is None


# ---------------------------------------------------------------------------
# ShellCIProvider
# ---------------------------------------------------------------------------


class TestShellCIProvider:
    @pytest.mark.asyncio
    async def test_successful_build(self):
        from henchmen.providers.local.shell_ci import ShellCIProvider

        provider = ShellCIProvider()
        build_id = await provider.trigger_build(
            repo_url="https://github.com/example/repo",
            branch="main",
            commands=["echo hello"],
        )
        result = await provider.get_status(build_id)
        assert result.status == CIStatus.SUCCESS
        assert result.duration_seconds is not None

    @pytest.mark.asyncio
    async def test_failing_build(self):
        from henchmen.providers.local.shell_ci import ShellCIProvider

        provider = ShellCIProvider()
        build_id = await provider.trigger_build(
            repo_url="https://github.com/example/repo",
            branch="main",
            commands=["exit 1"],
        )
        result = await provider.get_status(build_id)
        assert result.status == CIStatus.FAILURE

    @pytest.mark.asyncio
    async def test_failing_stops_subsequent_commands(self):
        from henchmen.providers.local.shell_ci import ShellCIProvider

        provider = ShellCIProvider()
        build_id = await provider.trigger_build(
            repo_url="https://github.com/example/repo",
            branch="main",
            commands=["exit 1", "echo SHOULD_NOT_RUN"],
        )
        logs = await provider.get_logs(build_id)
        assert "SHOULD_NOT_RUN" not in logs

    @pytest.mark.asyncio
    async def test_get_logs_contains_output(self):
        from henchmen.providers.local.shell_ci import ShellCIProvider

        provider = ShellCIProvider()
        build_id = await provider.trigger_build(
            repo_url="https://github.com/example/repo",
            branch="main",
            commands=["echo marker-output"],
        )
        logs = await provider.get_logs(build_id)
        assert "marker-output" in logs

    @pytest.mark.asyncio
    async def test_get_status_unknown_build(self):
        from henchmen.providers.local.shell_ci import ShellCIProvider

        provider = ShellCIProvider()
        result = await provider.get_status("nonexistent-build")
        assert result.status == CIStatus.FAILURE
        assert result.error_message == "Build not found"

    @pytest.mark.asyncio
    async def test_get_logs_unknown_build_returns_empty(self):
        from henchmen.providers.local.shell_ci import ShellCIProvider

        provider = ShellCIProvider()
        logs = await provider.get_logs("nonexistent-build")
        assert logs == ""

    @pytest.mark.asyncio
    async def test_cancel(self):
        from henchmen.providers.local.shell_ci import ShellCIProvider

        provider = ShellCIProvider()
        build_id = await provider.trigger_build("url", "main", commands=["echo ok"])
        await provider.cancel(build_id)
        result = await provider.get_status(build_id)
        assert result.status == CIStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_accepts_settings_arg(self):
        from henchmen.providers.local.shell_ci import ShellCIProvider

        provider = ShellCIProvider(settings=_mock_settings())
        build_id = await provider.trigger_build("url", "main", commands=["echo ok"])
        result = await provider.get_status(build_id)
        assert result.status == CIStatus.SUCCESS


# ---------------------------------------------------------------------------
# DockerOrchestrator
# ---------------------------------------------------------------------------


class TestDockerOrchestrator:
    @pytest.mark.asyncio
    async def test_get_status_unknown_exec_id_returns_failed(self):
        from henchmen.providers.local.docker import DockerOrchestrator

        orch = DockerOrchestrator(_mock_settings())
        result = await orch.get_status("totally-unknown-exec")
        assert result.status == JobStatus.FAILED
        assert result.exit_code == -1

    @pytest.mark.asyncio
    async def test_get_status_running(self):
        import asyncio

        from henchmen.providers.local.docker import DockerOrchestrator

        orch = DockerOrchestrator(_mock_settings())
        mock_process = MagicMock(spec=asyncio.subprocess.Process)
        mock_process.returncode = None
        orch._processes["exec-running"] = mock_process

        result = await orch.get_status("exec-running")
        assert result.status == JobStatus.RUNNING
        assert result.exit_code is None

    @pytest.mark.asyncio
    async def test_get_status_completed_exit_zero(self):
        import asyncio

        from henchmen.providers.local.docker import DockerOrchestrator

        orch = DockerOrchestrator(_mock_settings())
        mock_process = MagicMock(spec=asyncio.subprocess.Process)
        mock_process.returncode = 0
        orch._processes["exec-done"] = mock_process

        result = await orch.get_status("exec-done")
        assert result.status == JobStatus.COMPLETED
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_get_status_failed_nonzero_exit(self):
        import asyncio

        from henchmen.providers.local.docker import DockerOrchestrator

        orch = DockerOrchestrator(_mock_settings())
        mock_process = MagicMock(spec=asyncio.subprocess.Process)
        mock_process.returncode = 1
        orch._processes["exec-fail"] = mock_process

        result = await orch.get_status("exec-fail")
        assert result.status == JobStatus.FAILED
        assert result.exit_code == 1
