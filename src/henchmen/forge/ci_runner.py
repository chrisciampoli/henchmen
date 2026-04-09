"""CI Runner - executes lint and test checks on a cloned repository."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class CIRunner:
    """Runs CI checks (lint + tests) on a cloned repository."""

    async def run(self, workspace_dir: str) -> dict[str, Any]:
        """Run all CI checks and return aggregated results."""
        results: list[dict[str, Any]] = []

        # 1. Run ruff lint
        lint_result = await self._run_lint(workspace_dir)
        results.append(lint_result)

        # 2. Run tests (if applicable)
        test_result = await self._run_tests(workspace_dir)
        if test_result is not None:
            results.append(test_result)

        # 3. Silent failure detection
        scan_result = await self._run_silent_failure_scan(workspace_dir)
        results.append(scan_result)

        overall_pass = all(r["passed"] for r in results)
        return {
            "passed": overall_pass,
            "checks": results,
            "summary": self._build_summary(results),
        }

    async def _run_lint(self, workspace_dir: str) -> dict[str, Any]:
        """Run ``ruff check`` and return the result."""
        proc = await asyncio.create_subprocess_exec(
            "python",
            "-m",
            "ruff",
            "check",
            ".",
            cwd=workspace_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return {
            "name": "lint",
            "passed": proc.returncode == 0,
            "output": stdout.decode(errors="replace")[:5000],
            "error": stderr.decode(errors="replace")[:2000] if proc.returncode != 0 else "",
        }

    async def _run_tests(self, workspace_dir: str) -> dict[str, Any] | None:
        """Run pytest if tests directory exists; skip for Node.js projects."""
        # Check for common test locations
        test_dirs = ["tests", "test", "apps/api/test"]
        has_tests = any(os.path.isdir(os.path.join(workspace_dir, d)) for d in test_dirs)

        if not has_tests:
            return None

        # Node.js project detection -- skip pytest for JS/TS repos
        package_json = os.path.join(workspace_dir, "package.json")
        if os.path.exists(package_json):
            return {
                "name": "tests",
                "passed": True,
                "output": "Skipped (Node.js project — use npm test manually)",
                "error": "",
            }

        proc = await asyncio.create_subprocess_exec(
            "python",
            "-m",
            "pytest",
            "--tb=short",
            "-q",
            cwd=workspace_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return {
            "name": "tests",
            "passed": proc.returncode == 0,
            "output": stdout.decode(errors="replace")[:5000],
            "error": stderr.decode(errors="replace")[:2000] if proc.returncode != 0 else "",
        }

    async def _run_silent_failure_scan(self, workspace_dir: str) -> dict[str, Any]:
        """Scan the diff for silent failure patterns."""
        from henchmen.forge.silent_failure_detector import SilentFailureDetector

        # Get the diff
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "HEAD~1",
            cwd=workspace_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        diff_text = stdout.decode("utf-8", errors="replace")

        if not diff_text:
            return {
                "name": "silent_failure_scan",
                "passed": True,
                "output": "No diff to scan",
                "error": "",
            }

        detector = SilentFailureDetector()
        findings = detector.scan_diff(diff_text)

        critical_count = sum(1 for f in findings if f.severity == "critical")

        return {
            "name": "silent_failure_scan",
            "passed": critical_count == 0,  # Fail only on critical findings
            "output": detector.format_findings(findings),
            "error": "",
            "findings_count": len(findings),
            "critical_count": critical_count,
        }

    def _build_summary(self, results: list[dict[str, Any]]) -> str:
        """Build a human-readable summary of all check results."""
        lines: list[str] = []
        for r in results:
            status = "PASS" if r["passed"] else "FAIL"
            lines.append(f"{status}: {r['name']}")
            if not r["passed"] and r.get("output"):
                lines.append(f"  Output: {r['output'][:500]}")
        return "\n".join(lines)
