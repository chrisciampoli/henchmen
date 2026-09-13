"""RuleFileLoader – discovers and loads .cursorrules / CLAUDE.md style rule files."""

import asyncio
import logging
from pathlib import Path

from henchmen.models.dossier import RuleFile

logger = logging.getLogger(__name__)

# Rule file text is injected verbatim into the operative's system prompt and
# counted by the dispatch cost estimator, so it must be bounded. Without a cap
# a single oversized rules.md can push the estimate past the task cost ceiling
# (blocking dispatch) or crowd out file context.
MAX_RULE_FILE_CHARS: int = 32_000
MAX_TOTAL_RULE_CHARS: int = 128_000
_TRUNCATION_MARKER = "\n\n[truncated by Henchmen: rule file exceeds the per-file size cap]"


class RuleFileLoader:
    """Loads .cursorrules / CLAUDE.md style rule files scoped to directories."""

    RULE_FILE_NAMES: list[str] = [".cursorrules", "CLAUDE.md", ".clinerules", "rules.md"]

    @staticmethod
    async def load_rules(repo_dir: str, target_paths: list[str] | None = None) -> list[RuleFile]:
        """Load rule files from the repo, scoped to target directories.

        Walks from the repository root down to each target path, collecting
        rule files found at each directory level.  Global rules at the root
        are always included; deeper rules extend/override them.

        Args:
            repo_dir: Absolute path to the repository checkout.
            target_paths: File paths (relative to repo_dir) whose ancestor
                directories should be searched for rule files.  If None or
                empty, only the root is searched.

        Returns:
            List of RuleFile objects ordered from shallowest scope (root) to
            deepest (closest to the target).
        """
        root = Path(repo_dir).resolve()

        # Collect the set of directories to scan, from root inward.
        dirs_to_scan: list[Path] = [root]

        if target_paths:
            for rel_path in target_paths:
                abs_path = (root / rel_path).resolve()
                # Walk from root to the target's parent directory
                try:
                    rel = abs_path.relative_to(root)
                except ValueError:
                    # Path escapes repo_dir; skip
                    continue

                current = root
                for part in rel.parts[:-1]:  # Exclude the file itself
                    current = current / part
                    if current not in dirs_to_scan:
                        dirs_to_scan.append(current)

        # Deduplicate while preserving order (shallowest first)
        seen: set[Path] = set()
        ordered_dirs: list[Path] = []
        for d in dirs_to_scan:
            if d not in seen:
                seen.add(d)
                ordered_dirs.append(d)

        rule_files: list[RuleFile] = []
        total_chars = 0
        for directory in ordered_dirs:
            for name in RuleFileLoader.RULE_FILE_NAMES:
                candidate = directory / name
                if not candidate.is_file():
                    continue
                rel_path = str(candidate.relative_to(root))
                if total_chars >= MAX_TOTAL_RULE_CHARS:
                    logger.warning(
                        "Rule file budget of %d chars exhausted; skipping %s", MAX_TOTAL_RULE_CHARS, rel_path
                    )
                    continue
                try:
                    content = await _read_file(candidate)
                except (OSError, UnicodeDecodeError) as exc:
                    logger.warning("Could not read rule file %s: %s", rel_path, exc)
                    continue
                if len(content) > MAX_RULE_FILE_CHARS:
                    logger.warning(
                        "Rule file %s is %d chars; truncating to %d", rel_path, len(content), MAX_RULE_FILE_CHARS
                    )
                    content = content[:MAX_RULE_FILE_CHARS] + _TRUNCATION_MARKER
                total_chars += len(content)
                scope = str(directory.relative_to(root)) if directory != root else "/"
                rule_files.append(RuleFile(path=rel_path, scope=scope, content=content))

        return rule_files

    @staticmethod
    async def load_global_rules(repo_dir: str) -> list[RuleFile]:
        """Load only top-level (global) rule files from the repository root."""
        return await RuleFileLoader.load_rules(repo_dir, target_paths=None)

    @staticmethod
    def merge_rules(rules: list[RuleFile]) -> str:
        """Merge multiple rule files into a single context string.

        Rules are ordered by scope depth (shallowest / most global first so
        that deeper, more specific rules appear later and can add context).
        """
        if not rules:
            return ""

        def _depth(scope: str) -> int:
            """Return directory depth: root ('/') is 0, 'src' is 1, 'src/api' is 2, etc."""
            if scope in ("/", ".", ""):
                return 0
            return scope.replace("\\", "/").strip("/").count("/") + 1

        sorted_rules = sorted(rules, key=lambda r: _depth(r.scope))

        sections: list[str] = []
        for rule in sorted_rules:
            header = f"# Rules from: {rule.path} (scope: {rule.scope})"
            sections.append(f"{header}\n{rule.content}")

        return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _read_file(path: Path) -> str:
    """Asynchronously read a text file, returning its contents."""
    return await asyncio.to_thread(path.read_text, encoding="utf-8")
