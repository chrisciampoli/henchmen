"""Unit tests for the dossier file scorer module."""

from henchmen.dossier.file_scorer import FileScorer, FileScorerConfig


class TestFileScorerConfig:
    def test_default_weights(self):
        config = FileScorerConfig()
        assert config.mentioned_weight == 30
        assert config.rag_weight == 25
        assert config.import_neighbor_weight == 20
        assert config.recently_changed_weight == 15
        assert config.stack_trace_weight == 10

    def test_custom_weights(self):
        config = FileScorerConfig(mentioned_weight=50, rag_weight=10)
        assert config.mentioned_weight == 50
        assert config.rag_weight == 10


class TestFileScorer:
    def test_mentioned_files_get_high_score(self):
        scorer = FileScorer()
        files = ["src/auth.py", "src/utils.py", "src/config.py"]
        result = scorer.score_files(
            all_files=files,
            task_title="Fix auth.py login bug",
            task_description="The auth module is broken",
            mentioned_files={"auth.py"},
            rag_file_paths=set(),
            analysis_keywords=set(),
        )
        # auth.py should be first
        assert result[0][1] == "src/auth.py"
        assert result[0][0] > result[1][0]

    def test_rag_files_get_boosted(self):
        scorer = FileScorer()
        files = ["src/auth.py", "src/utils.py"]
        result = scorer.score_files(
            all_files=files,
            task_title="Fix something",
            task_description="",
            mentioned_files=set(),
            rag_file_paths={"src/utils.py"},
            analysis_keywords=set(),
        )
        # utils.py should score higher because it's in RAG results
        scores = {rel: score for score, rel in result}
        assert scores["src/utils.py"] > scores["src/auth.py"]

    def test_keyword_overlap_boosts_score(self):
        scorer = FileScorer()
        files = ["src/authentication/handler.py", "src/database/pool.py"]
        result = scorer.score_files(
            all_files=files,
            task_title="Fix authentication handler",
            task_description="",
            mentioned_files=set(),
            rag_file_paths=set(),
            analysis_keywords={"authentication", "handler"},
        )
        scores = {rel: score for score, rel in result}
        assert scores["src/authentication/handler.py"] > scores["src/database/pool.py"]

    def test_context_window_budget_limits_files(self):
        scorer = FileScorer()
        # Create many files
        files = [f"src/file_{i}.py" for i in range(100)]
        result = scorer.score_files(
            all_files=files,
            task_title="Generic task",
            task_description="No specific file mentioned",
            mentioned_files=set(),
            rag_file_paths=set(),
            analysis_keywords=set(),
            max_context_chars=20_000,  # Small budget
        )
        # Should select fewer than 100 files due to budget
        assert len(result) < 100
        # But should select at least some
        assert len(result) > 0

    def test_top_level_config_files_get_boosted(self):
        scorer = FileScorer()
        files = ["package.json", "src/deep/nested/file.py"]
        result = scorer.score_files(
            all_files=files,
            task_title="Generic task",
            task_description="",
            mentioned_files=set(),
            rag_file_paths=set(),
            analysis_keywords=set(),
        )
        scores = {rel: score for score, rel in result}
        assert scores["package.json"] > scores["src/deep/nested/file.py"]

    def test_source_files_get_small_boost(self):
        scorer = FileScorer()
        files = ["src/main.py", "data/notes.txt"]
        result = scorer.score_files(
            all_files=files,
            task_title="Generic task",
            task_description="",
            mentioned_files=set(),
            rag_file_paths=set(),
            analysis_keywords=set(),
        )
        scores = {rel: score for score, rel in result}
        assert scores["src/main.py"] >= scores["data/notes.txt"]

    def test_import_neighbor_boost(self):
        scorer = FileScorer()
        files = ["src/auth/login.py", "src/auth/session.py", "src/db/pool.py"]
        result = scorer.score_files(
            all_files=files,
            task_title="Fix login",
            task_description="",
            mentioned_files={"src/auth/login.py"},
            rag_file_paths=set(),
            analysis_keywords=set(),
        )
        scores = {rel: score for score, rel in result}
        # session.py is in the same directory as login.py — should get neighbor boost
        assert scores["src/auth/session.py"] > scores["src/db/pool.py"]

    def test_custom_config_changes_weights(self):
        config = FileScorerConfig(mentioned_weight=100, rag_weight=0)
        scorer = FileScorer(config=config)
        files = ["src/a.py", "src/b.py"]
        result = scorer.score_files(
            all_files=files,
            task_title="",
            task_description="",
            mentioned_files={"a.py"},
            rag_file_paths={"src/b.py"},
            analysis_keywords=set(),
        )
        scores = {rel: score for score, rel in result}
        # With rag_weight=0, b.py should not get a boost from RAG
        # With mentioned_weight=100, a.py should dominate
        assert scores["src/a.py"] > scores["src/b.py"]

    def test_empty_files_list(self):
        scorer = FileScorer()
        result = scorer.score_files(
            all_files=[],
            task_title="",
            task_description="",
            mentioned_files=set(),
            rag_file_paths=set(),
            analysis_keywords=set(),
        )
        assert result == []

    def test_readme_anywhere_gets_boost(self):
        scorer = FileScorer()
        files = ["docs/README.md", "src/random.py"]
        result = scorer.score_files(
            all_files=files,
            task_title="Generic task",
            task_description="",
            mentioned_files=set(),
            rag_file_paths=set(),
            analysis_keywords=set(),
        )
        scores = {rel: score for score, rel in result}
        assert scores["docs/README.md"] > scores["src/random.py"]
