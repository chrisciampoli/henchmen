"""CI Error Extractor — fetches GitHub check run annotations and formats them for operatives."""

from __future__ import annotations

import logging
from collections import defaultdict

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CIError(BaseModel):
    """A single CI error extracted from a GitHub check run."""

    check_name: str
    file_path: str
    line: int | None
    message: str
    severity: str


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_GITHUB_API = "https://api.github.com"
_FAILING_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required"}


async def extract_ci_errors(repo: str, check_suite_id: int, github_token: str) -> list[CIError]:
    """Fetch GitHub check runs + annotations for a check suite.

    Returns a list of CIError objects. Falls back to output.text when no
    annotations are available. Returns [] on any error.
    """
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            # 1. Get all check runs for the suite
            runs_url = f"{_GITHUB_API}/repos/{repo}/check-suites/{check_suite_id}/check-runs"
            runs_resp = await client.get(runs_url)
            runs_data = runs_resp.json()

            errors: list[CIError] = []

            for run in runs_data.get("check_runs", []):
                conclusion = run.get("conclusion") or ""
                if conclusion not in _FAILING_CONCLUSIONS:
                    continue  # skip passing / neutral checks

                run_id = run["id"]
                run_name = run["name"]
                output = run.get("output") or {}

                # 2. Fetch annotations for this run
                ann_url = f"{_GITHUB_API}/repos/{repo}/check-runs/{run_id}/annotations"
                ann_resp = await client.get(ann_url)
                annotations = ann_resp.json()

                if annotations:
                    for ann in annotations:
                        errors.append(
                            CIError(
                                check_name=run_name,
                                file_path=ann.get("path") or "",
                                line=ann.get("start_line"),
                                message=ann.get("message") or "",
                                severity=ann.get("annotation_level") or "failure",
                            )
                        )
                else:
                    # Fallback: use output.text if available
                    text = (output.get("text") or "").strip()
                    if text:
                        errors.append(
                            CIError(
                                check_name=run_name,
                                file_path="",
                                line=None,
                                message=text,
                                severity="failure",
                            )
                        )

            return errors

    except Exception as exc:
        logger.warning("Failed to extract CI errors for suite %s: %s", check_suite_id, exc)
        return []


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_errors_for_operative(errors: list[CIError]) -> str:
    """Group errors by check_name and format as markdown with file:line references.

    Returns an empty string for an empty list.
    """
    if not errors:
        return ""

    grouped: dict[str, list[CIError]] = defaultdict(list)
    for error in errors:
        grouped[error.check_name].append(error)

    lines: list[str] = []
    for check_name, check_errors in grouped.items():
        lines.append(f"## {check_name}")
        for err in check_errors:
            if err.file_path and err.line is not None:
                location = f"`{err.file_path}:{err.line}`"
            elif err.file_path:
                location = f"`{err.file_path}`"
            else:
                location = ""

            if location:
                lines.append(f"- {location}: {err.message}")
            else:
                lines.append(f"- {err.message}")
        lines.append("")

    return "\n".join(lines).rstrip()
