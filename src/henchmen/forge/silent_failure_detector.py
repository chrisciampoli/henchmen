"""Detects silent failure patterns in code diffs."""

import re
from dataclasses import dataclass
from typing import Any

# Files whose "secrets" are almost always fixtures, not credentials.
_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|fixtures)/|(^|/)conftest\.py$|(^|/)test_[^/]*\.py$|"
    r"[^/]*_test\.py$|[^/]*\.(test|spec)\.[jt]sx?$",
    re.IGNORECASE,
)

# Values that are obviously not real credentials.
_PLACEHOLDER_RE = re.compile(
    r"test|dummy|placeholder|example|sample|fake|changeme|xxx+|<[^>]+>|x-access-token",
    re.IGNORECASE,
)

_SCAN_FLAGS = re.IGNORECASE | re.MULTILINE | re.DOTALL


@dataclass
class Finding:
    """A detected silent failure pattern."""

    severity: str  # "critical", "warning", "info"
    pattern: str  # Name of the pattern detected
    description: str  # Human-readable description
    file: str  # File where found
    line_hint: str  # Approximate location


class SilentFailureDetector:
    """Scans diffs for patterns that indicate silent failures."""

    # Patterns to detect in added lines (lines starting with +).
    #
    # Every regex is anchored so that an unrelated identifier cannot trip it:
    # ``pass``/``retry`` carry word boundaries, and ``catch`` bodies are matched
    # with an explicit brace-bounded group rather than ``.*`` so that multi-line
    # blocks are covered. ``body_excludes`` marks patterns whose ``body`` group
    # is re-checked: a match is dropped when the body contains any of the tokens.
    PATTERNS: list[dict[str, Any]] = [
        {
            "name": "empty_catch",
            "regex": r"catch\s*\([^)]*\)\s*\{\s*\}",
            "severity": "critical",
            "description": "Empty catch block — errors are silently swallowed",
        },
        {
            "name": "catch_pass",
            "regex": r"except\b[^\n]*?:\s*(?:\n\s*)?pass\b",
            "severity": "critical",
            "description": "Bare except/pass — errors are silently ignored",
        },
        {
            "name": "catch_return_null",
            "regex": r"catch\s*\([^)]*\)\s*\{[^}]*return\s+(?:null|undefined|None)\b",
            "severity": "warning",
            "description": "Catch block returns null/None — failure is hidden from caller",
        },
        {
            "name": "except_return_none",
            "regex": r"except\b[^\n]*?:\s*(?:\n\s*)?return\s+None\b",
            "severity": "warning",
            "description": "except block returns None — failure is hidden from caller",
        },
        {
            "name": "no_error_logging",
            "regex": r"catch\s*\([^)]*\)\s*\{(?P<body>[^{}]*)\}",
            "body_excludes": ("log", "console", "print", "throw", "raise", "reject", "report"),
            "severity": "warning",
            "description": "Catch block without logging — failures will be invisible",
        },
        {
            "name": "retry_no_backoff",
            "regex": (
                r"\b(?:while|for)\b[^\n]*\b(?:retry|retries|attempt|attempts)\w*"
                r"(?![^\n]*(?:sleep|backoff|delay|wait))"
            ),
            "severity": "warning",
            "description": "Retry logic without backoff — may hammer external services",
        },
        {
            "name": "todo_fixme",
            # Leading and trailing word boundaries: the scan is case-insensitive,
            # so without them ``contemp`` / ``hackathon``-style substrings match.
            "regex": r"\b(?:TODO|FIXME|HACK|XXX|TEMP)\b",
            "severity": "info",
            "description": "TODO/FIXME comment — indicates incomplete implementation",
        },
        {
            "name": "hardcoded_secret",
            "regex": r"(?:password|secret|api_key|token)\s*=\s*['\"][^'\"]{8,}['\"]",
            "severity": "critical",
            "description": "Possible hardcoded secret — should use environment variables",
        },
    ]

    def scan_diff(self, diff_text: str) -> list[Finding]:
        """Scan a unified diff for silent failure patterns."""
        findings: list[Finding] = []

        if not diff_text:
            return findings

        current_file = ""
        added_lines: list[str] = []

        for line in diff_text.splitlines():
            # Track current file
            if line.startswith("+++ b/"):
                current_file = line[6:]
                added_lines = []
                continue

            # Collect added lines
            if line.startswith("+") and not line.startswith("+++"):
                added_lines.append(line[1:])  # Remove the + prefix

            # At end of file diff or at new file, scan collected lines
            if (line.startswith("diff --git") or line.startswith("+++ b/")) and added_lines and current_file:
                findings.extend(self._scan_lines(current_file, added_lines))
                added_lines = []

        # Scan remaining lines
        if added_lines and current_file:
            findings.extend(self._scan_lines(current_file, added_lines))

        # Check for noop changes
        findings.extend(self._check_noop(diff_text))

        return findings

    def _scan_lines(self, file: str, lines: list[str]) -> list[Finding]:
        """Scan a set of added lines for patterns."""
        findings = []
        full_text = "\n".join(lines)
        is_test_file = bool(_TEST_PATH_RE.search(file))

        for pattern in self.PATTERNS:
            excludes: tuple[str, ...] = pattern.get("body_excludes", ())

            for match in re.finditer(pattern["regex"], full_text, _SCAN_FLAGS):
                if excludes:
                    body = match.groupdict().get("body") or ""
                    if not body.strip():
                        continue  # An empty body is already reported by ``empty_catch``.
                    if any(token in body.lower() for token in excludes):
                        continue

                severity = pattern["severity"]
                if pattern["name"] == "hardcoded_secret":
                    if is_test_file:
                        continue  # Test fixtures are not production credentials.
                    if _PLACEHOLDER_RE.search(match.group(0)):
                        severity = "warning"

                # Find approximate line number
                line_num = full_text[: match.start()].count("\n") + 1
                findings.append(
                    Finding(
                        severity=severity,
                        pattern=pattern["name"],
                        description=pattern["description"],
                        file=file,
                        line_hint=f"~line {line_num} in added content",
                    )
                )

        return findings

    def _check_noop(self, diff_text: str) -> list[Finding]:
        """Check if the diff contains only whitespace/noop changes."""
        findings = []
        added = [
            line[1:].strip() for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")
        ]
        removed = [
            line[1:].strip() for line in diff_text.splitlines() if line.startswith("-") and not line.startswith("---")
        ]

        # If all added lines are duplicates of removed lines (just reordered), it's a noop
        if added and removed and set(added) == set(removed):
            findings.append(
                Finding(
                    severity="warning",
                    pattern="noop_change",
                    description="Changes appear to be whitespace-only or reordered — no functional change detected",
                    file="(entire diff)",
                    line_hint="",
                )
            )

        return findings

    def format_findings(self, findings: list[Finding]) -> str:
        """Format findings as a human-readable report."""
        if not findings:
            return "No silent failure patterns detected."

        lines = [f"Found {len(findings)} potential issue(s):\n"]

        tag_by_severity = {"critical": "[CRITICAL]", "warning": "[WARN]", "info": "[INFO]"}
        for f in findings:
            tag = tag_by_severity.get(f.severity, "[UNKNOWN]")
            lines.append(f"{tag} **{f.severity.upper()}**: {f.pattern}")
            lines.append(f"   {f.description}")
            lines.append(f"   File: `{f.file}` {f.line_hint}")
            lines.append("")

        return "\n".join(lines)
