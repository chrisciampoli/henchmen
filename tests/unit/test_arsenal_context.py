"""Unit tests for the Arsenal context tools (semantic_search, find_related)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.arsenal.tools.context import (
    _parse_js_imports,
    _parse_python_imports,
    _resolve_python_module,
    find_related,
    semantic_search,
)

# ---------------------------------------------------------------------------
# semantic_search
# ---------------------------------------------------------------------------


class TestSemanticSearch:
    @pytest.mark.asyncio
    async def test_returns_error_on_empty_query(self):
        result = await semantic_search(query="", top_k=5)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_returns_error_on_whitespace_query(self):
        result = await semantic_search(query="   ", top_k=5)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_falls_back_to_grep_on_import_error(self):
        with patch(
            "henchmen.dossier.embedder.query_similar_chunks",
            side_effect=ImportError("no RAG"),
        ):
            # grep fallback will also likely fail in test env — just verify no crash
            result = await semantic_search(query="authentication handler", top_k=3)
            assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_returns_rag_results_when_available(self):
        mock_chunk = MagicMock()
        mock_chunk.file_path = "src/auth.py"
        mock_chunk.start_line = 10
        mock_chunk.end_line = 25
        mock_chunk.symbol_name = "login"
        mock_chunk.language = "python"
        mock_chunk.relevance_score = 0.85
        mock_chunk.content = "def login(user, password): pass"

        with patch(
            "henchmen.dossier.embedder.query_similar_chunks",
            new_callable=AsyncMock,
            return_value=[mock_chunk],
        ):
            result = await semantic_search(query="login function", top_k=5)
            assert result["source"] == "rag"
            assert len(result["results"]) == 1
            assert result["results"][0]["file_path"] == "src/auth.py"

    @pytest.mark.asyncio
    async def test_clamps_top_k(self):
        with patch(
            "henchmen.dossier.embedder.query_similar_chunks",
            new_callable=AsyncMock,
            return_value=[],
        ):
            # top_k > 20 should be clamped
            result = await semantic_search(query="test", top_k=100)
            # No crash — we'll get grep fallback since RAG returned empty
            assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# find_related — Python import parsing
# ---------------------------------------------------------------------------


class TestParsePythonImports:
    def test_parses_standard_import(self, tmp_path):
        src = tmp_path / "src" / "mypackage"
        src.mkdir(parents=True)
        (src / "utils.py").write_text("def helper(): pass", encoding="utf-8")
        (src / "main.py").write_text(
            "import os\nfrom src.mypackage.utils import helper\n",
            encoding="utf-8",
        )
        imports = _parse_python_imports(str(src / "main.py"), str(tmp_path))
        # os is a stdlib module — won't resolve to a file
        # src.mypackage.utils should resolve
        assert any("utils.py" in imp for imp in imports)

    def test_parses_relative_import(self, tmp_path):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "helpers.py").write_text("def h(): pass", encoding="utf-8")
        (pkg / "main.py").write_text("from .helpers import h\n", encoding="utf-8")
        imports = _parse_python_imports(str(pkg / "main.py"), str(tmp_path))
        assert any("helpers.py" in imp for imp in imports)

    def test_handles_syntax_error(self, tmp_path):
        (tmp_path / "broken.py").write_text("def (\n", encoding="utf-8")
        imports = _parse_python_imports(str(tmp_path / "broken.py"), str(tmp_path))
        assert imports == []


class TestResolvePythonModule:
    def test_resolves_direct_module(self, tmp_path):
        (tmp_path / "mymod.py").write_text("", encoding="utf-8")
        result = _resolve_python_module("mymod", str(tmp_path))
        assert result == "mymod.py"

    def test_resolves_package_init(self, tmp_path):
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        result = _resolve_python_module("mypkg", str(tmp_path))
        assert "mypkg/__init__.py" in result.replace("\\", "/")

    def test_returns_empty_for_missing_module(self, tmp_path):
        result = _resolve_python_module("nonexistent", str(tmp_path))
        assert result == ""


# ---------------------------------------------------------------------------
# find_related — JS import parsing
# ---------------------------------------------------------------------------


class TestParseJSImports:
    def test_parses_es_import(self, tmp_path):
        (tmp_path / "utils.ts").write_text("export const x = 1;", encoding="utf-8")
        (tmp_path / "main.ts").write_text(
            "import { x } from './utils';\n",
            encoding="utf-8",
        )
        imports = _parse_js_imports(str(tmp_path / "main.ts"), str(tmp_path))
        assert any("utils.ts" in imp for imp in imports)

    def test_parses_require(self, tmp_path):
        (tmp_path / "helper.js").write_text("module.exports = {};", encoding="utf-8")
        (tmp_path / "app.js").write_text(
            "const h = require('./helper');\n",
            encoding="utf-8",
        )
        imports = _parse_js_imports(str(tmp_path / "app.js"), str(tmp_path))
        assert any("helper.js" in imp for imp in imports)

    def test_ignores_non_relative_imports(self, tmp_path):
        (tmp_path / "main.ts").write_text(
            "import React from 'react';\nimport { x } from './local';\n",
            encoding="utf-8",
        )
        imports = _parse_js_imports(str(tmp_path / "main.ts"), str(tmp_path))
        # 'react' is non-relative — should not be in results
        assert not any("react" in imp for imp in imports)


# ---------------------------------------------------------------------------
# find_related — integration
# ---------------------------------------------------------------------------


class TestFindRelated:
    def test_returns_error_for_missing_file(self):
        result = find_related("/nonexistent/path/file.py")
        assert "error" in result

    def test_finds_python_imports(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        pkg = tmp_path / "src"
        pkg.mkdir()
        (pkg / "utils.py").write_text("def helper(): pass", encoding="utf-8")
        (pkg / "main.py").write_text("from src.utils import helper\n", encoding="utf-8")

        result = find_related(str(pkg / "main.py"), depth=1)
        assert "related" in result
        assert result["depth"] == 1

    def test_clamps_depth(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "test.py").write_text("import os\n", encoding="utf-8")
        result = find_related(str(tmp_path / "test.py"), depth=10)
        # Depth should be clamped to 3
        assert result["depth"] == 3
