"""Unit tests for the embedding pipeline (Vertex AI RAG Engine)."""

from unittest.mock import AsyncMock

import pytest

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


# ---------------------------------------------------------------------------
# chunk_record_id
# ---------------------------------------------------------------------------


class TestChunkRecordId:
    def test_deterministic_id(self):
        from henchmen.dossier.embedder import chunk_record_id

        id1 = chunk_record_id("org/repo", "src/foo.py", 1, 10)
        id2 = chunk_record_id("org/repo", "src/foo.py", 1, 10)
        assert id1 == id2

    def test_different_inputs_different_id(self):
        from henchmen.dossier.embedder import chunk_record_id

        id1 = chunk_record_id("org/repo", "src/foo.py", 1, 10)
        id2 = chunk_record_id("org/repo", "src/bar.py", 1, 10)
        assert id1 != id2

    def test_id_is_hex_string(self):
        from henchmen.dossier.embedder import chunk_record_id

        record_id = chunk_record_id("org/repo", "src/foo.py", 1, 10)
        int(record_id, 16)  # Should not raise


# ---------------------------------------------------------------------------
# _parse_display_name
# ---------------------------------------------------------------------------


class TestParseDisplayName:
    def test_parses_full_metadata(self):
        from henchmen.dossier.embedder import _parse_display_name

        display = "abc123|org/repo|src/auth.py|10|25|login|python|function"
        fp, start, end, sym, lang = _parse_display_name(display, "org/repo")
        assert fp == "src/auth.py"
        assert start == 10
        assert end == 25
        assert sym == "login"
        assert lang == "python"

    def test_handles_empty_symbol(self):
        from henchmen.dossier.embedder import _parse_display_name

        display = "abc123|org/repo|src/main.py|1|50||python|fixed"
        fp, start, end, sym, lang = _parse_display_name(display, "org/repo")
        assert fp == "src/main.py"
        assert sym == ""

    def test_handles_malformed_input(self):
        from henchmen.dossier.embedder import _parse_display_name

        fp, start, end, sym, lang = _parse_display_name("garbage", "org/repo")
        assert fp == "unknown"
        assert start == 0


# ---------------------------------------------------------------------------
# query_similar_chunks (graceful degradation)
# ---------------------------------------------------------------------------


class TestQuerySimilarChunks:
    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        """Semantic search should degrade gracefully when RAG Engine is unavailable."""
        from henchmen.dossier.embedder import query_similar_chunks

        results = await query_similar_chunks(
            query_text="anything",
            repo="org/repo",
            collection_name="henchmen-code",
            project_id="test-project",
        )
        assert results == []


# ---------------------------------------------------------------------------
# Commit tracking metadata (Firestore-based)
# ---------------------------------------------------------------------------


class TestCommitTracking:
    @pytest.mark.asyncio
    async def test_get_last_indexed_commit_returns_none_on_error(self):
        from henchmen.dossier.embedder import get_last_indexed_commit

        result = await get_last_indexed_commit("org/repo", project_id="nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_last_indexed_commit_no_raise_on_error(self):
        from henchmen.dossier.embedder import set_last_indexed_commit

        await set_last_indexed_commit("org/repo", "abc123", project_id="nonexistent")

    @pytest.mark.asyncio
    async def test_document_store_roundtrip_with_mock(self):
        from henchmen.dossier.embedder import get_last_indexed_commit, set_last_indexed_commit

        mock_store = AsyncMock()
        mock_store.get.return_value = {"commit_sha": "abc123"}

        await set_last_indexed_commit("org/repo", "abc123", document_store=mock_store)
        result = await get_last_indexed_commit("org/repo", document_store=mock_store)

        mock_store.set.assert_awaited_once_with(
            "vector_search_metadata", "org/repo", {"commit_sha": "abc123", "repo": "org/repo"}
        )
        assert result == "abc123"
