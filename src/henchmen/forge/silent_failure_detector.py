"""Detects silent failure patterns in code diffs."""

import re
from dataclasses import dataclass
from typing import Any


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

    # Patterns to detect in added lines (lines starting with +)
    PATTERNS: list[dict[str, Any]] = [
        {
            "name": "empty_catch",
            "regex": r"catch\s*\([^)]*\)\s*\{\s*\}",
            "severity": "critical",
            "description": "Empty catch block — errors are silently swallowed",
        },
        {
            "name": "catch_pass",
            "regex": r"except\s*.*:\s*\n\s*pass",
            "severity": "critical",
            "description": "Bare except/pass — errors are silently ignored",
        },
        {
            "name": "catch_return_null",
            "regex": r"catch\s*\([^)]*\)\s*\{[^}]*return\s+(null|undefined|None)",
            "severity": "warning",
            "description": "Catch block returns null/None — failure is hidden from caller",
        },
        {
            "name": "no_error_logging",
            "regex": r"catch\s*\([^)]*\)\s*\{(?!.*(?:log|console|print|logger)).*\}",
            "severity": "warning",
            "description": "Catch block without logging — failures will be invisible",
        },
        {
            "name": "retry_no_backoff",
            "regex": r"retry|while.*retry|for.*attempt(?!.*(?:sleep|backoff|delay|wait))",
            "severity": "warning",
            "description": "Retry logic without backoff — may hammer external services",
        },
        {
            "name": "todo_fixme",
            "regex": r"(?:TODO|FIXME|HACK|XXX|TEMP)\b",
            "severity": "info",
            "description": "TODO/FIXME comment — indicates incomplete implementation",
        },
        {
            "name": "hardcoded_secret",
            "regex": r"(?:password|secret|api_key|token)\s*=\s*['\"][^'\"]{8,}['\"]",
            "severity": "critical",
            "description": "Possible hardcoded secret — should use environment variables",
        },
        {
            "name": "noop_change",
            "regex": None,  # Special case — detected by comparing added/removed
            "severity": "warning",
            "description": "File appears to have no meaningful changes (whitespace only or duplicate content)",
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

        for pattern in self.PATTERNS:
            if pattern["regex"] is None:
                continue  # Special cases handled elsewhere

            matches = re.finditer(pattern["regex"], full_text, re.IGNORECASE | re.MULTILINE)
            for match in matches:
                # Find approximate line number
                line_num = full_text[: match.start()].count("\n") + 1
                findings.append(
                    Finding(
                        severity=pattern["severity"],
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
