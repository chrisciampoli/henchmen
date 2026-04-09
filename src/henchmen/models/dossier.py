"""Dossier models - context packages assembled for operative consumption."""

from __future__ import annotations

from pydantic import Field

from henchmen.models._base import StrictBase


class RuleFile(StrictBase):
    """A repository rule or instruction file (e.g. CLAUDE.md, .cursorrules)."""

    path: str = Field(..., description="Path to the rule file within the repository")
    scope: str = Field(..., description="Directory scope this rule file applies to")
    content: str = Field(..., description="Full text content of the rule file")


class RelatedPR(StrictBase):
    """A pull request related to the task."""

    number: int = Field(..., description="PR number")
    title: str = Field(..., description="PR title")
    url: str = Field(..., description="URL to the PR")
    state: str = Field(..., description="PR state (open, closed, merged)")
    files_changed: list[str] = Field(default_factory=list, description="Files changed in this PR")


class RelatedIssue(StrictBase):
    """An issue or ticket related to the task."""

    number: int = Field(..., description="Issue number")
    title: str = Field(..., description="Issue title")
    url: str = Field(..., description="URL to the issue")
    state: str = Field(..., description="Issue state (open, closed)")
    labels: list[str] = Field(default_factory=list, description="Issue labels")


class CodeSearchResult(StrictBase):
    """A code search match from the repository."""

    file_path: str = Field(..., description="Path to the matching file")
    matches: list[str] = Field(default_factory=list, description="Matching lines or snippets")
    context: str = Field(default="", description="Surrounding context for the match")


class SemanticChunk(StrictBase):
    """A code chunk retrieved via semantic search from the vector index."""

    file_path: str = Field(..., description="Relative path to the source file")
    start_line: int = Field(..., description="Start line number in the source file")
    end_line: int = Field(..., description="End line number in the source file")
    symbol_name: str | None = Field(default=None, description="Function/class name if AST-chunked")
    language: str = Field(..., description="Programming language of the chunk")
    content: str = Field(..., description="The actual code text")
    relevance_score: float = Field(..., description="Similarity score from vector search (0-1)")


class Dossier(StrictBase):
    """Complete context package assembled for an operative prior to execution."""

    task_id: str = Field(..., description="ID of the parent HenchmenTask")
    rule_files: list[RuleFile] = Field(default_factory=list, description="Repo rule files relevant to this task")
    relevant_files: list[str] = Field(default_factory=list, description="File paths identified as relevant to the task")
    related_prs: list[RelatedPR] = Field(
        default_factory=list, description="Related pull requests from the source repository"
    )
    related_issues: list[RelatedIssue] = Field(
        default_factory=list, description="Related issues or tickets from source tracking systems"
    )
    code_search_results: list[CodeSearchResult] = Field(
        default_factory=list, description="Symbol and pattern search results from the codebase"
    )
    semantic_code_chunks: list[SemanticChunk] = Field(
        default_factory=list, description="Code chunks retrieved via semantic search, ranked by relevance"
    )
    task_analysis: TaskAnalysis | None = Field(default=None, description="Analyzed task context from TaskAnalyzer")
    repo_structure: str = Field(default="", description="Condensed directory tree of the repository")
    artifact_uri: str | None = Field(default=None, description="GCS URI if this dossier has been serialized to storage")


# Import here to avoid circular dependency — TaskAnalysis is used in Dossier annotation above
from henchmen.dossier.task_analyzer import TaskAnalysis  # noqa: E402, F811

Dossier.model_rebuild()
