"""Analyzes task descriptions to extract actionable context clues for dossier building."""

import re

from pydantic import BaseModel, Field


class TaskAnalysis(BaseModel):
    """Analyzed task with extracted context clues."""

    task_type: str = Field(default="generic", description="Detected task type")
    mentioned_files: list[str] = Field(default_factory=list, description="File paths mentioned in the task")
    mentioned_errors: list[str] = Field(default_factory=list, description="Error patterns mentioned")
    keywords: list[str] = Field(default_factory=list, description="Important keywords for file search")
    ci_related: bool = Field(default=False, description="Whether this involves CI/test failures")
    specific_file_target: str | None = Field(default=None, description="If a specific file is targeted")


class TaskAnalyzer:
    """Analyzes task descriptions to extract actionable context."""

    # Patterns for file paths
    FILE_PATTERNS = [
        r"[\w\-/]+/[\w\-]+\.(?:py|ts|tsx|js|jsx|css|html|yaml|yml|json|md|toml)",  # path/to/file.ext
        r"[\w\-]+\.(?:py|ts|tsx|js|jsx|css|html|yaml|yml|json|md|toml)",  # filename.ext
    ]

    # Error patterns
    ERROR_PATTERNS = [
        r"(?:TypeError|ReferenceError|SyntaxError|ValueError|KeyError|ImportError|AttributeError)",
        r"(?:500|404|403|401)\s*(?:error|status)",
        r"(?:failed|failing|broken|crash)",
    ]

    # Task type detection keywords (checked in order, first match wins)
    # Feature must come before test_fix so "add unit tests" is a feature, not a test fix
    TASK_TYPE_KEYWORDS: dict[str, list[str]] = {
        "feature": ["feature", "implement", "build", "create new", "add new", "add:", "add (", "add a ", "add the "],
        "bug_fix": ["bug", "fix", "error", "crash", "broken", "issue", "defect"],
        "test_fix": ["failing test", "test failure", "test fail", "tests failing", "spec fail"],
        "refactor": ["refactor", "clean up", "simplify", "reorganize", "restructure"],
    }

    # CI-related keywords — specific phrases, not bare "test"
    CI_KEYWORDS = [
        "ci fail",
        "ci error",
        "lint fail",
        "build fail",
        "pipeline fail",
        "workflow fail",
        "actions fail",
        "failing test",
        "test failure",
        "tests failing",
        "test fail",
    ]

    # Words to exclude from keyword extraction
    STOP_WORDS = frozenset(
        {
            "the",
            "a",
            "an",
            "in",
            "on",
            "at",
            "to",
            "for",
            "of",
            "with",
            "and",
            "or",
            "fix",
            "add",
            "bug",
            "this",
            "that",
            "from",
            "into",
            "when",
            "what",
            "which",
            "should",
            "could",
            "would",
            "have",
            "been",
            "some",
            "more",
            "also",
            "just",
            "please",
            "need",
            "needs",
            "make",
            "making",
        }
    )

    def analyze(self, title: str, description: str) -> TaskAnalysis:
        """Analyze a task and extract context clues."""
        combined = f"{title} {description}"
        text_lower = combined.lower()

        # Extract mentioned files
        mentioned_files = self._extract_files(combined)

        # Extract error patterns
        mentioned_errors = self._extract_errors(text_lower)

        # Detect task type
        task_type = self._detect_task_type(text_lower)

        # CI related?
        ci_related = any(kw in text_lower for kw in self.CI_KEYWORDS)

        # Specific file target
        specific_file = mentioned_files[0] if mentioned_files else None

        # Extract important keywords (nouns that help find relevant files)
        keywords = [w for w in text_lower.split() if len(w) > 3 and w not in self.STOP_WORDS][:10]

        return TaskAnalysis(
            task_type=task_type,
            mentioned_files=mentioned_files,
            mentioned_errors=mentioned_errors,
            keywords=keywords,
            ci_related=ci_related,
            specific_file_target=specific_file,
        )

    def _extract_files(self, text: str) -> list[str]:
        """Extract file path references from task text."""
        files: list[str] = []
        seen: set[str] = set()
        for pattern in self.FILE_PATTERNS:
            for match in re.findall(pattern, text, re.IGNORECASE):
                if match not in seen:
                    seen.add(match)
                    files.append(match)
        return files

    def _extract_errors(self, text_lower: str) -> list[str]:
        """Extract error pattern references from task text."""
        errors: list[str] = []
        for pattern in self.ERROR_PATTERNS:
            errors.extend(re.findall(pattern, text_lower, re.IGNORECASE))
        return errors

    def _detect_task_type(self, text_lower: str) -> str:
        """Determine the task type from keywords."""
        for task_type, keywords in self.TASK_TYPE_KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                return task_type
        return "generic"
