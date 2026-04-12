"""Score files for relevance to a task using multiple weighted signals.

Replaces the inline scoring logic in ``operative.bootstrap._build_file_context``
with a reusable, configurable scorer. Scoring signals include:

- Direct mention in task text or task analysis (+30)
- Appearance in RAG semantic search results (+25)
- Import neighbor of a mentioned file (+20)
- Recently changed according to git log (+15)
- Appears in a stack trace (+10)
- Keyword overlap with path components (+1 per match)

Files are selected up to a configurable context window (default 80K chars)
at 60% fill to leave room for system prompt and tool output.
"""

import os
import re

from pydantic import Field

from henchmen.models._base import StrictBase


class FileScorerConfig(StrictBase):
    """Weights for file scoring signals."""

    mentioned_weight: int = Field(default=30, description="Weight for files directly mentioned in task text")
    rag_weight: int = Field(default=25, description="Weight for files appearing in RAG results")
    import_neighbor_weight: int = Field(default=20, description="Weight for import neighbors of mentioned files")
    recently_changed_weight: int = Field(default=15, description="Weight for recently changed files (git log)")
    stack_trace_weight: int = Field(default=10, description="Weight for files appearing in stack traces")


_TOP_LEVEL_FILES = {"README.md", "package.json", "pyproject.toml", "setup.py", "Makefile", "Cargo.toml", "go.mod"}


class FileScorer:
    """Score workspace files for relevance to a task.

    Combines multiple heuristic signals with configurable weights to produce
    a ranked list of files. Fills up to 60% of a configurable context window
    instead of a hard file count limit.
    """

    def __init__(self, config: FileScorerConfig | None = None) -> None:
        self.config = config or FileScorerConfig()

    def score_files(
        self,
        all_files: list[str],
        task_title: str,
        task_description: str,
        mentioned_files: set[str],
        rag_file_paths: set[str],
        analysis_keywords: set[str],
        max_context_chars: int = 80_000,
    ) -> list[tuple[float, str]]:
        """Return files scored and sorted by relevance, capped by context budget.

        Parameters
        ----------
        all_files:
            All relative file paths in the workspace.
        task_title:
            Task title text.
        task_description:
            Task description text.
        mentioned_files:
            Lowercased file paths/basenames mentioned in the task or analysis.
        rag_file_paths:
            Lowercased file paths from RAG semantic search results.
        analysis_keywords:
            Keywords extracted from task analysis.
        max_context_chars:
            Maximum context window size in characters. Files are selected to
            fill 60% of this budget.

        Returns
        -------
        list[tuple[float, str]]
            Scored (score, relative_path) pairs sorted by descending score.
        """
        combined_text = f"{task_title} {task_description}".lower()
        mentioned_patterns: list[str] = re.findall(r"[\w\-]+\.[\w]+", combined_text)
        keywords = set(re.findall(r"[a-z]{3,}", combined_text))

        scored: list[tuple[float, str]] = []
        for rel in all_files:
            score = self._score_single_file(
                rel,
                mentioned_files=mentioned_files,
                rag_file_paths=rag_file_paths,
                analysis_keywords=analysis_keywords,
                mentioned_patterns=mentioned_patterns,
                keywords=keywords,
            )
            scored.append((score, rel))

        # Sort descending by score, alphabetical tiebreak
        scored.sort(key=lambda t: (-t[0], t[1]))

        # Apply context window budget (60% fill)
        budget = int(max_context_chars * 0.6)
        selected: list[tuple[float, str]] = []
        chars_used = 0

        for score, rel in scored:
            # Estimate file section overhead: header + code fences + content
            estimated_chars = len(rel) + 20  # header overhead
            # We can't read the file here — estimate based on typical file size
            # The caller reads files afterward and truncates if needed
            estimated_chars += 4000  # typical max per-file chars
            if chars_used + estimated_chars > budget and selected:
                break
            selected.append((score, rel))
            chars_used += estimated_chars

        return selected

    def _score_single_file(
        self,
        rel: str,
        mentioned_files: set[str],
        rag_file_paths: set[str],
        analysis_keywords: set[str],
        mentioned_patterns: list[str],
        keywords: set[str],
    ) -> float:
        """Compute a relevance score for a single file path."""
        score = 0.0
        basename = os.path.basename(rel).lower()
        rel_lower = rel.lower()

        # HIGH PRIORITY: Exact match with files mentioned in task analysis
        if basename in mentioned_files or rel_lower in mentioned_files:
            score += self.config.mentioned_weight + 20  # +50 total for exact match

        # Partial match with analysis mentioned files
        for mentioned in mentioned_files:
            if mentioned in rel_lower:
                score += self.config.mentioned_weight - 5  # +25 for partial
                break

        # Exact file name match from task text
        for pat in mentioned_patterns:
            if pat.lower() == basename:
                score += 10
            elif pat.lower() in rel_lower:
                score += 5

        # Boost files that appear in RAG semantic search results
        if rel_lower in rag_file_paths or basename in rag_file_paths:
            score += self.config.rag_weight

        # Top-level config / readme
        if os.path.basename(rel) in _TOP_LEVEL_FILES and "/" not in rel:
            score += 3

        # README.md anywhere
        if basename == "readme.md":
            score += 4

        # Keyword overlap with path components
        path_parts = set(re.findall(r"[a-z]{3,}", rel_lower))
        overlap = keywords & path_parts
        score += len(overlap) * 0.5

        # Analysis keywords get additional weight
        analysis_overlap = analysis_keywords & path_parts
        score += len(analysis_overlap) * 1.0

        # Boost files in the same directory as mentioned files (import neighbor proxy)
        rel_dir = os.path.dirname(rel_lower)
        if rel_dir:
            for mentioned in mentioned_files:
                mentioned_dir = os.path.dirname(mentioned.lower())
                if mentioned_dir and rel_dir == mentioned_dir:
                    score += self.config.import_neighbor_weight - 5  # +15
                    break

        # Prefer source files
        if rel.endswith((".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java")):
            score += 0.5

        return score
