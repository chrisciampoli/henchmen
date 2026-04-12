"""Unit tests for the dossier reranker module."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from henchmen.dossier.reranker import (
    RerankerResult,
    _build_chunk_summaries,
    _fallback_sort,
    _parse_rerank_response,
    rerank_chunks,
)

# ---------------------------------------------------------------------------
# _build_chunk_summaries
# ---------------------------------------------------------------------------


class TestBuildChunkSummaries:
    def test_builds_indexed_summary_lines(self):
        chunks = [
            {"file_path": "src/app.py", "content": "def main():\n    pass"},
            {"file_path": "src/utils.py", "content": "import os\nimport sys"},
        ]
        result = _build_chunk_summaries(chunks)
        assert "0 | src/app.py |" in result
        assert "1 | src/utils.py |" in result

    def test_truncates_content_preview(self):
        chunks = [{"file_path": "big.py", "content": "x" * 500}]
        result = _build_chunk_summaries(chunks, preview_chars=50)
        # The preview should not contain 500 characters
        lines = result.strip().split("\n")
        assert len(lines) == 1
        # Preview is truncated to 50 chars
        preview_part = lines[0].split(" | ", 2)[2]
        assert len(preview_part) <= 50

    def test_empty_chunks(self):
        assert _build_chunk_summaries([]) == ""


# ---------------------------------------------------------------------------
# _parse_rerank_response
# ---------------------------------------------------------------------------


class TestParseRerankResponse:
    def test_parses_valid_json(self):
        response = '[{"index": 0, "score": 0.9}, {"index": 1, "score": 0.3}]'
        result = _parse_rerank_response(response, 2)
        assert len(result) == 2
        assert result[0] == (0, 0.9)
        assert result[1] == (1, 0.3)

    def test_strips_markdown_fences(self):
        response = '```json\n[{"index": 0, "score": 0.8}]\n```'
        result = _parse_rerank_response(response, 1)
        assert len(result) == 1
        assert result[0] == (0, 0.8)

    def test_clamps_scores(self):
        response = '[{"index": 0, "score": 1.5}, {"index": 1, "score": -0.2}]'
        result = _parse_rerank_response(response, 2)
        assert result[0] == (0, 1.0)
        assert result[1] == (1, 0.0)

    def test_filters_out_of_range_indices(self):
        response = '[{"index": 0, "score": 0.5}, {"index": 99, "score": 0.9}]'
        result = _parse_rerank_response(response, 3)
        assert len(result) == 1
        assert result[0] == (0, 0.5)

    def test_returns_empty_on_invalid_json(self):
        result = _parse_rerank_response("not json at all", 3)
        assert result == []

    def test_returns_empty_on_non_array(self):
        result = _parse_rerank_response('{"index": 0, "score": 0.5}', 3)
        assert result == []

    def test_skips_malformed_entries(self):
        response = '[{"index": 0, "score": 0.5}, {"bad": "entry"}, {"index": 1, "score": "invalid"}]'
        result = _parse_rerank_response(response, 3)
        assert len(result) == 1
        assert result[0] == (0, 0.5)


# ---------------------------------------------------------------------------
# _fallback_sort
# ---------------------------------------------------------------------------


class TestFallbackSort:
    def test_sorts_by_existing_relevance_score(self):
        chunks = [
            {"file_path": "a.py", "content": "aaa", "relevance_score": 0.3},
            {"file_path": "b.py", "content": "bbb", "relevance_score": 0.9},
            {"file_path": "c.py", "content": "ccc", "relevance_score": 0.6},
        ]
        result = _fallback_sort(chunks, top_k=2)
        assert len(result) == 2
        assert result[0].file_path == "b.py"
        assert result[1].file_path == "c.py"

    def test_handles_missing_relevance_score(self):
        chunks = [{"file_path": "x.py", "content": "xxx"}]
        result = _fallback_sort(chunks, top_k=5)
        assert len(result) == 1
        assert result[0].relevance_score == 0.0


# ---------------------------------------------------------------------------
# rerank_chunks (integration with mocked LLM)
# ---------------------------------------------------------------------------


class TestRerankChunks:
    @pytest.mark.asyncio
    async def test_successful_reranking(self):
        chunks = [
            {"file_path": "auth.py", "content": "def login(): pass", "relevance_score": 0.5},
            {"file_path": "utils.py", "content": "def helper(): pass", "relevance_score": 0.8},
            {"file_path": "config.py", "content": "DEBUG = True", "relevance_score": 0.3},
        ]

        mock_response = MagicMock()
        mock_response.text = '[{"index": 2, "score": 0.95}, {"index": 0, "score": 0.7}, {"index": 1, "score": 0.2}]'

        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(return_value=mock_response)

        result = await rerank_chunks(chunks, "Fix the login bug", mock_provider, top_k=2)

        assert len(result) == 2
        assert result[0].file_path == "config.py"
        assert result[0].relevance_score == 0.95
        assert result[1].file_path == "auth.py"

    @pytest.mark.asyncio
    async def test_falls_back_on_llm_failure(self):
        chunks = [
            {"file_path": "a.py", "content": "aaa", "relevance_score": 0.3},
            {"file_path": "b.py", "content": "bbb", "relevance_score": 0.9},
        ]

        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        result = await rerank_chunks(chunks, "some task", mock_provider, top_k=2)

        # Should fall back to original order by relevance_score
        assert len(result) == 2
        assert result[0].file_path == "b.py"
        assert result[0].relevance_score == 0.9

    @pytest.mark.asyncio
    async def test_falls_back_on_malformed_response(self):
        chunks = [
            {"file_path": "x.py", "content": "xxx", "relevance_score": 0.5},
        ]

        mock_response = MagicMock()
        mock_response.text = "this is not valid JSON at all"

        mock_provider = AsyncMock()
        mock_provider.generate = AsyncMock(return_value=mock_response)

        result = await rerank_chunks(chunks, "task", mock_provider, top_k=5)

        # Should fall back to original order
        assert len(result) == 1
        assert result[0].file_path == "x.py"

    @pytest.mark.asyncio
    async def test_empty_chunks_returns_empty(self):
        result = await rerank_chunks([], "task", AsyncMock())
        assert result == []

    @pytest.mark.asyncio
    async def test_reranker_result_model_validation(self):
        r = RerankerResult(
            file_path="test.py",
            content="hello",
            relevance_score=0.5,
            original_index=0,
        )
        assert r.file_path == "test.py"
        assert r.relevance_score == 0.5

    @pytest.mark.asyncio
    async def test_reranker_result_rejects_out_of_range_score(self):
        with pytest.raises(Exception):
            RerankerResult(
                file_path="test.py",
                content="hello",
                relevance_score=1.5,
                original_index=0,
            )
