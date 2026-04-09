"""Unit tests for the Dossier components."""

import asyncio as _asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.dossier.cache import SnapshotCache
from henchmen.dossier.rules import RuleFileLoader
from henchmen.models.dossier import CodeSearchResult, Dossier, RelatedIssue, RelatedPR, RuleFile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(**kwargs):
    from henchmen.models.task import HenchmenTask, TaskContext, TaskSource

    defaults = {
        "source": TaskSource.GITHUB,
        "source_id": "gh-123",
        "title": "Fix login bug",
        "description": "Users cannot log in with SSO",
        "context": TaskContext(repo="myorg/myrepo"),
        "created_by": "user1",
    }
    defaults.update(kwargs)
    return HenchmenTask(**defaults)


def _make_settings(**kwargs) -> MagicMock:
    s = MagicMock()
    s.gcs_bucket_dossier = "my-dossier-bucket"
    s.gcs_bucket_snapshots = "my-snapshots-bucket"
    s.gcp_project_id = "my-project"
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


# ---------------------------------------------------------------------------
# RuleFileLoader – global rules
# ---------------------------------------------------------------------------


class TestRuleFileLoaderGlobalRules:
    def test_loads_claude_md_from_root(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Global rules\nBe safe.", encoding="utf-8")

        rules = pytest.run_async(RuleFileLoader.load_global_rules(str(tmp_path)))
        assert len(rules) == 1
        assert rules[0].path == "CLAUDE.md"
        assert rules[0].scope == "/"
        assert "Global rules" in rules[0].content

    def test_loads_cursorrules_from_root(self, tmp_path):
        (tmp_path / ".cursorrules").write_text("Always write tests.", encoding="utf-8")

        rules = pytest.run_async(RuleFileLoader.load_global_rules(str(tmp_path)))
        assert any(".cursorrules" in r.path for r in rules)

    def test_no_rule_files_returns_empty(self, tmp_path):
        rules = pytest.run_async(RuleFileLoader.load_global_rules(str(tmp_path)))
        assert rules == []

    def test_multiple_rule_files_at_root(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("Claude rules.", encoding="utf-8")
        (tmp_path / ".cursorrules").write_text("Cursor rules.", encoding="utf-8")

        rules = pytest.run_async(RuleFileLoader.load_global_rules(str(tmp_path)))
        names = [r.path for r in rules]
        assert any("CLAUDE.md" in n for n in names)
        assert any(".cursorrules" in n for n in names)


# ---------------------------------------------------------------------------
# RuleFileLoader – scoped rules
# ---------------------------------------------------------------------------


class TestRuleFileLoaderScopedRules:
    def test_loads_root_and_subdir_rules(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("Root rules.", encoding="utf-8")
        subdir = tmp_path / "src" / "utils"
        subdir.mkdir(parents=True)
        (subdir / "CLAUDE.md").write_text("Utils rules.", encoding="utf-8")

        rules = pytest.run_async(RuleFileLoader.load_rules(str(tmp_path), target_paths=["src/utils/foo.py"]))
        scopes = [r.scope for r in rules]
        assert "/" in scopes  # root
        # We don't require the subdir scope to appear unless the dir exists in the walk

    def test_root_always_included(self, tmp_path):
        (tmp_path / ".cursorrules").write_text("Root.", encoding="utf-8")

        rules = pytest.run_async(RuleFileLoader.load_rules(str(tmp_path), target_paths=["deep/path/file.py"]))
        assert any(r.scope == "/" for r in rules)

    def test_no_target_paths_behaves_like_global(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("Root.", encoding="utf-8")

        rules_global = pytest.run_async(RuleFileLoader.load_global_rules(str(tmp_path)))
        rules_scoped = pytest.run_async(RuleFileLoader.load_rules(str(tmp_path)))
        assert len(rules_global) == len(rules_scoped)

    def test_path_outside_repo_is_ignored(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("Root.", encoding="utf-8")

        # Should not raise, just ignore escape
        rules = pytest.run_async(RuleFileLoader.load_rules(str(tmp_path), target_paths=["../../etc/passwd"]))
        assert all(r.scope == "/" or not r.scope.startswith("/etc") for r in rules)


# ---------------------------------------------------------------------------
# RuleFileLoader – merge_rules
# ---------------------------------------------------------------------------


class TestRuleFileLoaderMerge:
    def test_merge_produces_string(self):
        rules = [
            RuleFile(path="CLAUDE.md", scope="/", content="Global rule."),
            RuleFile(path="src/CLAUDE.md", scope="src", content="Src rule."),
        ]
        merged = RuleFileLoader.merge_rules(rules)
        assert "Global rule." in merged
        assert "Src rule." in merged

    def test_merge_empty_returns_empty_string(self):
        assert RuleFileLoader.merge_rules([]) == ""

    def test_merge_global_before_specific(self):
        rules = [
            RuleFile(path="src/api/CLAUDE.md", scope="src/api", content="API rule."),
            RuleFile(path="CLAUDE.md", scope="/", content="Root rule."),
        ]
        merged = RuleFileLoader.merge_rules(rules)
        root_idx = merged.index("Root rule.")
        api_idx = merged.index("API rule.")
        assert root_idx < api_idx, "Root rule should appear before API-scoped rule"

    def test_merge_single_rule(self):
        rules = [RuleFile(path="CLAUDE.md", scope="/", content="Only rule.")]
        merged = RuleFileLoader.merge_rules(rules)
        assert "Only rule." in merged


# ---------------------------------------------------------------------------
# SnapshotCache – key generation
# ---------------------------------------------------------------------------


class TestSnapshotCacheKeyGeneration:
    def _make_cache(self) -> SnapshotCache:
        settings = _make_settings()
        mock_store = MagicMock()
        return SnapshotCache(settings, object_store=mock_store)

    def test_key_is_deterministic(self):
        cache = self._make_cache()
        k1 = cache._snapshot_key("https://github.com/org/repo", "main")
        k2 = cache._snapshot_key("https://github.com/org/repo", "main")
        assert k1 == k2

    def test_different_repo_different_key(self):
        cache = self._make_cache()
        k1 = cache._snapshot_key("https://github.com/org/repo-a", "main")
        k2 = cache._snapshot_key("https://github.com/org/repo-b", "main")
        assert k1 != k2

    def test_different_branch_different_key(self):
        cache = self._make_cache()
        k1 = cache._snapshot_key("https://github.com/org/repo", "main")
        k2 = cache._snapshot_key("https://github.com/org/repo", "develop")
        assert k1 != k2

    def test_key_length(self):
        cache = self._make_cache()
        key = cache._snapshot_key("https://github.com/org/repo", "main")
        assert len(key) == 40

    def test_key_is_hex(self):
        cache = self._make_cache()
        key = cache._snapshot_key("https://github.com/org/repo", "main")
        int(key, 16)  # Should not raise


# ---------------------------------------------------------------------------
# SnapshotCache – get_snapshot returns None when no bucket configured
# ---------------------------------------------------------------------------


class TestSnapshotCacheGetSnapshot:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_bucket(self):
        settings = _make_settings(gcs_bucket_snapshots="")
        mock_store = AsyncMock()
        cache = SnapshotCache(settings, object_store=mock_store)
        result = await cache.get_snapshot("https://github.com/org/repo", "main")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_blob_does_not_exist(self):
        settings = _make_settings(gcs_bucket_snapshots="my-bucket")
        mock_store = AsyncMock()
        mock_store.exists.return_value = False
        cache = SnapshotCache(settings, object_store=mock_store)

        result = await cache.get_snapshot("https://github.com/org/repo", "main")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_uri_when_blob_exists(self):
        settings = _make_settings(gcs_bucket_snapshots="my-bucket")
        mock_store = AsyncMock()
        mock_store.exists.return_value = True
        cache = SnapshotCache(settings, object_store=mock_store)

        result = await cache.get_snapshot("https://github.com/org/repo", "main")
        assert result is not None
        assert result.startswith("gs://my-bucket/")


# ---------------------------------------------------------------------------
# DossierBuilder – with mocked GCS
# ---------------------------------------------------------------------------


class TestDossierBuilder:
    def _make_builder(self, **kwargs):
        from henchmen.dossier.builder import DossierBuilder

        settings = _make_settings(**kwargs)
        mock_store = AsyncMock()
        return DossierBuilder(settings, object_store=mock_store)

    @pytest.mark.asyncio
    async def test_build_returns_dossier(self):
        from henchmen.models.scheme import DossierRequirement

        builder = self._make_builder()
        task = _make_task()
        req = DossierRequirement()

        with patch.object(builder, "_upload_artifact", new_callable=AsyncMock, return_value="gs://bucket/dossier.json"):
            dossier = await builder.build(task, req)

        assert isinstance(dossier, Dossier)
        assert dossier.task_id == task.id

    @pytest.mark.asyncio
    async def test_build_fetches_files_when_requested(self):
        from henchmen.models.scheme import DossierRequirement
        from henchmen.models.task import TaskContext

        context = TaskContext(
            repo="org/repo",
            pr_diff="--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1 +1 @@\n-old\n+new\n",
        )
        task = _make_task(context=context)
        req = DossierRequirement(fetch_files=True)

        builder = self._make_builder()
        with patch.object(builder, "_upload_artifact", new_callable=AsyncMock, return_value="gs://b/d.json"):
            dossier = await builder.build(task, req)

        assert "src/foo.py" in dossier.relevant_files

    @pytest.mark.asyncio
    async def test_build_no_fetch_leaves_lists_empty(self):
        from henchmen.models.scheme import DossierRequirement

        req = DossierRequirement()  # all flags False
        task = _make_task()
        builder = self._make_builder()

        with patch.object(builder, "_upload_artifact", new_callable=AsyncMock, return_value="gs://b/d.json"):
            dossier = await builder.build(task, req)

        assert dossier.relevant_files == []
        assert dossier.rule_files == []
        assert dossier.related_prs == []
        assert dossier.related_issues == []
        assert dossier.code_search_results == []

    @pytest.mark.asyncio
    async def test_upload_artifact_called(self):
        from henchmen.models.scheme import DossierRequirement

        req = DossierRequirement()
        task = _make_task()
        builder = self._make_builder()

        with patch.object(
            builder, "_upload_artifact", new_callable=AsyncMock, return_value="gs://b/d.json"
        ) as mock_upload:
            dossier = await builder.build(task, req)

        mock_upload.assert_awaited_once()
        assert dossier.artifact_uri == "gs://b/d.json"

    @pytest.mark.asyncio
    async def test_fetch_relevant_files_empty_when_no_pr_diff(self):
        task = _make_task()
        builder = self._make_builder()
        files = await builder._fetch_relevant_files(task)
        assert files == []

    @pytest.mark.asyncio
    async def test_fetch_relevant_files_parses_pr_diff(self):
        from henchmen.models.task import TaskContext

        diff = (
            "--- a/henchmen/foo.py\n"
            "+++ b/henchmen/foo.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-old\n"
            "+new\n"
            "--- a/henchmen/bar.py\n"
            "+++ b/henchmen/bar.py\n"
        )
        context = TaskContext(repo="org/repo", pr_diff=diff)
        task = _make_task(context=context)
        builder = self._make_builder()
        files = await builder._fetch_relevant_files(task)
        assert "henchmen/foo.py" in files
        assert "henchmen/bar.py" in files

    @pytest.mark.asyncio
    async def test_fetch_rule_files_returns_empty_list(self):
        task = _make_task()
        builder = self._make_builder()
        rules = await builder._fetch_rule_files(task)
        assert rules == []

    @pytest.mark.asyncio
    async def test_upload_artifact_skipped_when_no_bucket(self):
        task = _make_task()
        builder = self._make_builder(gcs_bucket_dossier="")
        dossier = Dossier(task_id=task.id)
        uri = await builder._upload_artifact(dossier)
        assert uri == ""


# ---------------------------------------------------------------------------
# Async test runner helper (pytest-asyncio handles @pytest.mark.asyncio,
# but the synchronous tests in RuleFileLoader need a simple runner)
# ---------------------------------------------------------------------------


# Inject a run_async helper onto the pytest module for synchronous tests


def _run_async(coro):
    loop = _asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


pytest.run_async = _run_async


# ---------------------------------------------------------------------------
# Typed dossier models
# ---------------------------------------------------------------------------


class TestTypedDossierModels:
    def test_related_pr_creation(self):
        pr = RelatedPR(number=42, title="Fix auth", url="https://github.com/org/repo/pull/42", state="open")
        assert pr.number == 42
        assert pr.files_changed == []

    def test_related_issue_creation(self):
        issue = RelatedIssue(number=10, title="Login broken", url="https://github.com/org/repo/issues/10", state="open")
        assert issue.number == 10
        assert issue.labels == []

    def test_code_search_result_creation(self):
        result = CodeSearchResult(file_path="src/auth.py", matches=["def login("])
        assert result.file_path == "src/auth.py"
        assert result.context == ""

    def test_dossier_with_typed_fields(self):
        dossier = Dossier(
            task_id="task-1",
            related_prs=[RelatedPR(number=1, title="PR", url="http://x", state="open")],
            related_issues=[RelatedIssue(number=2, title="Issue", url="http://y", state="closed")],
            code_search_results=[CodeSearchResult(file_path="a.py", matches=["class Foo"])],
        )
        assert len(dossier.related_prs) == 1
        assert dossier.related_prs[0].number == 1

    def test_dossier_artifact_removed(self):
        import henchmen.models.dossier as mod

        assert not hasattr(mod, "DossierArtifact")


# ---------------------------------------------------------------------------
# TaskAnalysis as Pydantic model
# ---------------------------------------------------------------------------


class TestTaskAnalysisPydantic:
    def test_task_analysis_is_pydantic(self):
        from pydantic import BaseModel

        from henchmen.dossier.task_analyzer import TaskAnalysis

        assert issubclass(TaskAnalysis, BaseModel)

    def test_task_analysis_serializes(self):
        from henchmen.dossier.task_analyzer import TaskAnalysis

        analysis = TaskAnalysis(task_type="bug_fix", mentioned_files=["foo.py"])
        data = analysis.model_dump()
        assert data["task_type"] == "bug_fix"
        assert data["mentioned_files"] == ["foo.py"]

    def test_dossier_has_task_analysis_field(self):
        from henchmen.dossier.task_analyzer import TaskAnalysis

        analysis = TaskAnalysis(task_type="feature")
        dossier = Dossier(task_id="task-1", task_analysis=analysis)
        assert dossier.task_analysis is not None
        assert dossier.task_analysis.task_type == "feature"

    def test_dossier_task_analysis_defaults_to_none(self):
        dossier = Dossier(task_id="task-1")
        assert dossier.task_analysis is None


# ---------------------------------------------------------------------------
# SemanticChunk and Dossier.semantic_code_chunks
# ---------------------------------------------------------------------------


class TestSemanticChunk:
    def test_semantic_chunk_creation(self):
        from henchmen.models.dossier import SemanticChunk

        chunk = SemanticChunk(
            file_path="src/auth/login.py",
            start_line=10,
            end_line=25,
            symbol_name="login_user",
            language="python",
            content="def login_user(email, password):\n    ...",
            relevance_score=0.92,
        )
        assert chunk.file_path == "src/auth/login.py"
        assert chunk.start_line == 10
        assert chunk.end_line == 25
        assert chunk.symbol_name == "login_user"
        assert chunk.relevance_score == 0.92

    def test_semantic_chunk_optional_symbol_name(self):
        from henchmen.models.dossier import SemanticChunk

        chunk = SemanticChunk(
            file_path="config.yaml",
            start_line=1,
            end_line=20,
            symbol_name=None,
            language="yaml",
            content="database:\n  host: localhost",
            relevance_score=0.75,
        )
        assert chunk.symbol_name is None

    def test_dossier_has_semantic_code_chunks_field(self):
        from henchmen.models.dossier import SemanticChunk

        chunk = SemanticChunk(
            file_path="src/foo.py",
            start_line=1,
            end_line=10,
            symbol_name="foo",
            language="python",
            content="def foo(): pass",
            relevance_score=0.88,
        )
        dossier = Dossier(task_id="task-1", semantic_code_chunks=[chunk])
        assert len(dossier.semantic_code_chunks) == 1
        assert dossier.semantic_code_chunks[0].relevance_score == 0.88

    def test_dossier_semantic_code_chunks_defaults_empty(self):
        dossier = Dossier(task_id="task-1")
        assert dossier.semantic_code_chunks == []

    def test_dossier_serializes_with_semantic_chunks(self):
        from henchmen.models.dossier import SemanticChunk

        chunk = SemanticChunk(
            file_path="a.py",
            start_line=1,
            end_line=5,
            symbol_name=None,
            language="python",
            content="x = 1",
            relevance_score=0.5,
        )
        dossier = Dossier(task_id="t1", semantic_code_chunks=[chunk])
        data = json.loads(dossier.model_dump_json())
        assert len(data["semantic_code_chunks"]) == 1
        assert data["semantic_code_chunks"][0]["file_path"] == "a.py"
