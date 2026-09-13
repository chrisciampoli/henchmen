"""Unit tests for the embedding pipeline (Vertex AI RAG Engine)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from henchmen.dossier import embedder
from henchmen.dossier.chunker import CodeChunk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(**kwargs) -> CodeChunk:
    defaults = {
        "file_path": "src/foo.py",
        "start_line": 1,
        "end_line": 10,
        "symbol_name": "foo",
        "language": "python",
        "content": "def foo(): pass",
        "chunk_type": "function",
    }
    defaults.update(kwargs)
    return CodeChunk(**defaults)


def _context(text: str, score: float = 0.0, display_name: str = "") -> SimpleNamespace:
    """A stand-in for ``RagContexts.Context`` (no ``distance`` attribute)."""
    return SimpleNamespace(text=text, score=score, source_display_name=display_name, source_uri="")


@pytest.fixture
def _rag_available():
    with patch.object(embedder, "_rag_available", return_value=True):
        yield


# ---------------------------------------------------------------------------
# chunk_record_id
# ---------------------------------------------------------------------------


class TestChunkRecordId:
    def test_deterministic_id(self):
        id1 = embedder.chunk_record_id("org/repo", "src/foo.py", 1, 10)
        id2 = embedder.chunk_record_id("org/repo", "src/foo.py", 1, 10)
        assert id1 == id2

    def test_different_inputs_different_id(self):
        id1 = embedder.chunk_record_id("org/repo", "src/foo.py", 1, 10)
        id2 = embedder.chunk_record_id("org/repo", "src/bar.py", 1, 10)
        assert id1 != id2

    def test_id_is_hex_string(self):
        int(embedder.chunk_record_id("org/repo", "src/foo.py", 1, 10), 16)  # Should not raise


# ---------------------------------------------------------------------------
# Display name / chunk header encoding
# ---------------------------------------------------------------------------


class TestChunkEncoding:
    def test_display_name_stays_within_api_limit(self):
        """RagFile.display_name is capped at 128 characters by the API."""
        chunk = _make_chunk(
            file_path="src/components/dashboard/widgets/revenue/RevenueChartContainer.tsx",
            symbol_name="RevenueChartContainerComponentWithSuspenseBoundary",
            language="typescript",
            start_line=1200,
            end_line=1480,
        )
        name = embedder.build_display_name("chrisciampoli/strapboot", chunk)
        assert len(name) <= 128

    def test_display_name_encodes_source_path_key(self):
        chunk = _make_chunk()
        name = embedder.build_display_name("org/repo", chunk)
        assert name.split("|")[1] == embedder.source_path_key("org/repo", "src/foo.py")

    def test_chunk_payload_roundtrips_metadata(self):
        chunk = _make_chunk(file_path="a/b/c.py", start_line=3, end_line=9, symbol_name="do_it")
        payload = embedder.build_chunk_payload("org/repo", chunk)
        parsed = embedder._parse_chunk_header(payload)
        assert parsed == ("org/repo", "a/b/c.py", 3, 9, "do_it", "python")
        assert embedder._strip_chunk_header(payload) == "def foo(): pass"

    def test_chunk_payload_handles_missing_symbol(self):
        chunk = _make_chunk(symbol_name=None, chunk_type="fixed")
        parsed = embedder._parse_chunk_header(embedder.build_chunk_payload("org/repo", chunk))
        assert parsed is not None
        assert parsed[4] == ""

    def test_parse_chunk_header_rejects_plain_text(self):
        assert embedder._parse_chunk_header("def foo(): pass") is None


class TestParseLegacyDisplayName:
    def test_parses_full_metadata(self):
        display = "abc123|org/repo|src/auth.py|10|25|login|python|function"
        parsed = embedder._parse_display_name(display, "org/repo")
        assert parsed == ("org/repo", "src/auth.py", 10, 25, "login", "python")

    def test_handles_empty_symbol(self):
        display = "abc123|org/repo|src/main.py|1|50||python|fixed"
        parsed = embedder._parse_display_name(display, "org/repo")
        assert parsed is not None
        assert parsed[1] == "src/main.py"
        assert parsed[4] == ""

    def test_handles_malformed_input(self):
        assert embedder._parse_display_name("garbage", "org/repo") is None

    def test_ignores_v2_names(self):
        chunk = _make_chunk()
        name = embedder.build_display_name("org/repo", chunk)
        assert embedder._parse_display_name(name, "org/repo") is None


# ---------------------------------------------------------------------------
# query_similar_chunks
# ---------------------------------------------------------------------------


class TestQuerySimilarChunks:
    @pytest.mark.asyncio
    async def test_returns_empty_when_rag_unavailable(self):
        """Semantic search degrades gracefully when RAG Engine is unavailable."""
        with patch.object(embedder, "_rag_available", return_value=False):
            results = await embedder.query_similar_chunks(
                query_text="anything",
                repo="org/repo",
                collection_name="henchmen-code",
                project_id="test-project",
            )
        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_corpus_missing(self, _rag_available):
        """A read must never create the corpus it is reading from."""
        with patch.object(embedder, "get_corpus", new_callable=AsyncMock, return_value="") as get_corpus:
            results = await embedder.query_similar_chunks(
                query_text="anything", repo="org/repo", project_id="p", region="r"
            )
        assert results == []
        get_corpus.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_filters_out_other_repos(self, _rag_available):
        mine = embedder.build_chunk_payload("org/repo", _make_chunk(file_path="src/mine.py"))
        theirs = embedder.build_chunk_payload("other/repo", _make_chunk(file_path="src/theirs.py"))

        async def _fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        fake_rag = SimpleNamespace(
            retrieval_query=lambda **kw: SimpleNamespace(
                contexts=SimpleNamespace(contexts=[_context(theirs, 0.1), _context(mine, 0.2)])
            ),
            RagResource=lambda **kw: kw,
            RagRetrievalConfig=lambda **kw: kw,
        )

        with (
            patch.object(embedder, "get_corpus", new_callable=AsyncMock, return_value="corpora/1"),
            patch.dict("sys.modules", {"vertexai": SimpleNamespace(rag=fake_rag, init=lambda **kw: None)}),
            patch.object(embedder.asyncio, "to_thread", new=_fake_to_thread),
        ):
            results = await embedder.query_similar_chunks(query_text="q", repo="org/repo", project_id="p", region="r")

        assert [c.file_path for c in results] == ["src/mine.py"]

    @pytest.mark.asyncio
    async def test_reads_score_not_distance(self, _rag_available):
        """RagContexts.Context exposes ``score``; there is no ``distance``."""
        payload = embedder.build_chunk_payload("org/repo", _make_chunk())

        async def _fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        fake_rag = SimpleNamespace(
            retrieval_query=lambda **kw: SimpleNamespace(
                contexts=SimpleNamespace(contexts=[_context(payload, score=0.25)])
            ),
            RagResource=lambda **kw: kw,
            RagRetrievalConfig=lambda **kw: kw,
        )

        with (
            patch.object(embedder, "get_corpus", new_callable=AsyncMock, return_value="corpora/1"),
            patch.dict("sys.modules", {"vertexai": SimpleNamespace(rag=fake_rag, init=lambda **kw: None)}),
            patch.object(embedder.asyncio, "to_thread", new=_fake_to_thread),
        ):
            results = await embedder.query_similar_chunks(query_text="q", repo="org/repo", project_id="p", region="r")

        assert results[0].relevance_score == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# upsert_chunks
# ---------------------------------------------------------------------------


class TestUpsertChunks:
    @pytest.mark.asyncio
    async def test_empty_chunks_is_ok(self):
        result = await embedder.upsert_chunks([], repo="org/repo", commit_sha="abc")
        assert result.uploaded == 0
        assert result.ok

    @pytest.mark.asyncio
    async def test_skipped_result_is_not_ok(self):
        """A skipped run must be distinguishable from a successful empty run."""
        with patch.object(embedder, "_rag_available", return_value=False):
            result = await embedder.upsert_chunks([_make_chunk()], repo="org/repo", commit_sha="abc")
        assert result.uploaded == 0
        assert not result.ok
        assert result.skipped_reason

    @pytest.mark.asyncio
    async def test_rate_limit_is_retried(self, _rag_available):
        """Rate-limit errors must reach the backoff loop, not be swallowed."""
        chunks = [_make_chunk(start_line=i, end_line=i + 1) for i in range(3)]
        calls: list[int] = []

        async def _fake_to_thread(fn, batch):
            calls.append(len(batch))
            if len(calls) == 1:
                return 1, batch[1:], 0  # uploaded 1, rate limited on the rest
            return len(batch), [], 0

        with (
            patch.object(embedder, "get_or_create_corpus", new_callable=AsyncMock, return_value="corpora/1"),
            patch.object(embedder.asyncio, "to_thread", new=_fake_to_thread),
            patch.object(embedder.asyncio, "sleep", new_callable=AsyncMock) as sleep,
        ):
            result = await embedder.upsert_chunks(chunks, repo="org/repo", commit_sha="abc")

        assert calls == [3, 2]
        assert result.uploaded == 3
        assert result.failed == 0
        sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_permanent_failures_are_reported(self, _rag_available):
        async def _fake_to_thread(fn, batch):
            return 1, [], len(batch) - 1

        with (
            patch.object(embedder, "get_or_create_corpus", new_callable=AsyncMock, return_value="corpora/1"),
            patch.object(embedder.asyncio, "to_thread", new=_fake_to_thread),
        ):
            result = await embedder.upsert_chunks(
                [_make_chunk(start_line=i, end_line=i + 1) for i in range(4)],
                repo="org/repo",
                commit_sha="abc",
            )

        assert result.uploaded == 1
        assert result.failed == 3
        assert not result.ok

    @pytest.mark.asyncio
    async def test_replace_existing_deletes_first(self, _rag_available):
        async def _fake_to_thread(fn, batch):
            return len(batch), [], 0

        with (
            patch.object(embedder, "get_or_create_corpus", new_callable=AsyncMock, return_value="corpora/1"),
            patch.object(embedder, "delete_repo_chunks", new_callable=AsyncMock) as delete,
            patch.object(embedder.asyncio, "to_thread", new=_fake_to_thread),
        ):
            await embedder.upsert_chunks([_make_chunk()], repo="org/repo", commit_sha="abc", replace_existing=True)

        delete.assert_awaited_once()


# ---------------------------------------------------------------------------
# Corpus resolution
# ---------------------------------------------------------------------------


class TestCorpusResolution:
    @pytest.mark.asyncio
    async def test_get_corpus_returns_empty_when_unavailable(self):
        with patch.object(embedder, "_rag_available", return_value=False):
            assert await embedder.get_corpus("henchmen-code", "p", "r") == ""

    @pytest.mark.asyncio
    async def test_get_corpus_caches_resolved_name(self, _rag_available):
        embedder._reset_corpus_cache()
        calls = []

        async def _fake_to_thread(fn, *args, **kwargs):
            calls.append(fn)
            return "projects/p/locations/r/ragCorpora/1"

        with patch.object(embedder.asyncio, "to_thread", new=_fake_to_thread):
            first = await embedder.get_corpus("henchmen-code", "p", "r")
            second = await embedder.get_corpus("henchmen-code", "p", "r")

        assert first == second
        assert len(calls) == 1
        embedder._reset_corpus_cache()

    @pytest.mark.asyncio
    async def test_create_corpus_uses_configured_embedding_model(self, _rag_available, monkeypatch):
        """HENCHMEN_RAG_EMBEDDING_MODEL must not be ignored."""
        monkeypatch.setenv("HENCHMEN_RAG_EMBEDDING_MODEL", "text-embedding-large-exp")
        from henchmen.config.settings import Settings

        settings = Settings(provider="local")
        captured: dict[str, str] = {}

        def _fake_create(**kwargs):
            captured.update({"display_name": kwargs["display_name"]})
            return SimpleNamespace(name="corpora/new")

        async def _fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        fake_rag = SimpleNamespace(
            create_corpus=_fake_create,
            RagEmbeddingModelConfig=lambda **kw: captured.update(kw) or kw,
            VertexPredictionEndpoint=lambda **kw: captured.update(kw) or kw,
            RagVectorDbConfig=lambda **kw: kw,
        )

        embedder._reset_corpus_cache()
        with (
            patch.object(embedder, "get_corpus", new_callable=AsyncMock, return_value=""),
            patch.dict("sys.modules", {"vertexai": SimpleNamespace(rag=fake_rag, init=lambda **kw: None)}),
            patch.object(embedder.asyncio, "to_thread", new=_fake_to_thread),
        ):
            name = await embedder.get_or_create_corpus("henchmen-code", "p", "r", settings=settings)

        assert name == "corpora/new"
        assert captured["publisher_model"] == "publishers/google/models/text-embedding-large-exp"
        embedder._reset_corpus_cache()


# ---------------------------------------------------------------------------
# Commit tracking metadata
# ---------------------------------------------------------------------------


class TestCommitTracking:
    @pytest.mark.asyncio
    async def test_get_last_indexed_commit_returns_none_on_error(self):
        store = AsyncMock()
        store.get.side_effect = RuntimeError("backend down")
        assert await embedder.get_last_indexed_commit("org/repo", document_store=store) is None

    @pytest.mark.asyncio
    async def test_set_last_indexed_commit_no_raise_on_error(self):
        store = AsyncMock()
        store.set.side_effect = RuntimeError("backend down")
        await embedder.set_last_indexed_commit("org/repo", "abc123", document_store=store)

    @pytest.mark.asyncio
    async def test_document_store_roundtrip_with_mock(self):
        mock_store = AsyncMock()
        mock_store.get.return_value = {"commit_sha": "abc123"}

        await embedder.set_last_indexed_commit("org/repo", "abc123", document_store=mock_store)
        result = await embedder.get_last_indexed_commit("org/repo", document_store=mock_store)

        mock_store.set.assert_awaited_once_with(
            "vector_search_metadata", "org/repo", {"commit_sha": "abc123", "repo": "org/repo"}
        )
        assert result == "abc123"

    @pytest.mark.asyncio
    async def test_default_store_follows_configured_provider(self):
        """It used to be hardwired to Firestore regardless of the provider."""
        from henchmen.config.settings import Settings
        from henchmen.providers.local.sqlite import SQLiteDocumentStore

        store = embedder._default_document_store(Settings(provider="local"), "")
        assert isinstance(store, SQLiteDocumentStore)
