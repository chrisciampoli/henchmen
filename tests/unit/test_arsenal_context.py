"""Unit tests for the Arsenal live-context tools (semantic_search, find_related)."""

import inspect
import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from henchmen.arsenal._workspace import set_workspace_root
from henchmen.arsenal.tools import context


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A workspace root that is also the process cwd, like the operative's clone."""
    root = tmp_path / "workspace"
    root.mkdir()
    set_workspace_root(root)
    monkeypatch.chdir(root)
    yield root
    set_workspace_root(None)


class TestFindRelated:
    def test_is_async(self):
        """The agent loop awaits every handler; a sync find_related failed every call."""
        assert inspect.iscoroutinefunction(context.find_related)

    @pytest.mark.asyncio
    async def test_resolves_python_imports(self, workspace: Path):
        pkg = workspace / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "util.py").write_text("X = 1\n", encoding="utf-8")
        (workspace / "main.py").write_text("from pkg import util\nimport pkg.util\n", encoding="utf-8")

        result = await context.find_related(str(workspace / "main.py"))

        assert "error" not in result
        assert set(result["related"]["main.py"]) == {"pkg/__init__.py", "pkg/util.py"}

    @pytest.mark.asyncio
    async def test_resolves_relative_js_imports(self, workspace: Path):
        (workspace / "lib.ts").write_text("export const a = 1\n", encoding="utf-8")
        (workspace / "app.ts").write_text("import { a } from './lib'\n", encoding="utf-8")

        result = await context.find_related(str(workspace / "app.ts"))

        assert result["related"]["app.ts"] == ["lib.ts"]

    @pytest.mark.asyncio
    async def test_outside_workspace_denied(self, workspace: Path, tmp_path: Path):
        outside = tmp_path / "secret.py"
        outside.write_text("import os\n", encoding="utf-8")

        result = await context.find_related(str(outside))

        assert "access denied" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_file(self, workspace: Path):
        result = await context.find_related(str(workspace / "nope.py"))
        assert "File not found" in result["error"]


class TestSemanticSearch:
    @pytest.mark.asyncio
    async def test_queries_corpus_region_and_repo_from_settings(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        """The corpus lives in rag_corpus_region, not gcp_region; querying elsewhere finds nothing."""
        import henchmen.dossier.embedder as embedder

        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "proj-1")
        monkeypatch.setenv("HENCHMEN_GCP_REGION", "us-central1")
        monkeypatch.setenv("HENCHMEN_RAG_CORPUS_REGION", "us-west1")
        monkeypatch.setenv("REPO_URL", "https://github.com/acme/widgets.git")
        query = AsyncMock(return_value=[])
        monkeypatch.setattr(embedder, "query_similar_chunks", query)
        monkeypatch.setattr(context, "_grep_fallback", AsyncMock(return_value={"results": [], "source": "grep"}))

        await context.semantic_search("login bug")

        kwargs = query.await_args.kwargs
        assert kwargs["region"] == "us-west1"
        assert kwargs["project_id"] == "proj-1"
        assert kwargs["repo"] == "acme/widgets"

    @pytest.mark.asyncio
    async def test_empty_query_rejected(self):
        assert "error" in await context.semantic_search("   ")

    @pytest.mark.skipif(shutil.which("grep") is None, reason="grep is not installed")
    @pytest.mark.asyncio
    async def test_grep_fallback_handles_dash_prefixed_terms(self, workspace: Path):
        (workspace / "cli.py").write_text("flag = '--verbose-mode'\n", encoding="utf-8")

        result = await context._grep_fallback("--verbose-mode", top_k=5)

        assert "error" not in result
        assert result["results"] == [{"file_path": "cli.py", "source": "grep"}]
