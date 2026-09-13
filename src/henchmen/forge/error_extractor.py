"""CI Error Extractor — fetches GitHub check run annotations and formats them for operatives."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

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
_PER_PAGE = 100
_MAX_PAGES = 10
_AUTH_STATUSES = {401, 403, 404}


class CIErrorExtractionError(RuntimeError):
    """Raised when GitHub check-run data could not be retrieved.

    An empty ``[]`` from :func:`extract_ci_errors` means "the suite reported no
    errors". A failure to *reach* GitHub (bad credentials, rate limit, deleted
    repo, transport error) must never be squashed into that same empty list, or
    a token misconfiguration silently disables the whole CI fix loop.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _raise_for_status(resp: httpx.Response, what: str) -> None:
    """Turn a non-2xx GitHub response into an explicit extraction error."""
    if resp.status_code < 400:
        return
    body = (resp.text or "")[:200]
    if resp.status_code in _AUTH_STATUSES:
        raise CIErrorExtractionError(
            f"GitHub denied access while fetching {what} (HTTP {resp.status_code}). "
            f"Check that the GitHub token is set and has repo scope: {body}",
            status_code=resp.status_code,
        )
    raise CIErrorExtractionError(
        f"GitHub returned HTTP {resp.status_code} while fetching {what}: {body}",
        status_code=resp.status_code,
    )


async def extract_ci_errors(repo: str, check_suite_id: int, github_token: str) -> list[CIError]:
    """Fetch GitHub check runs + annotations for a check suite.

    Returns a list of CIError objects (empty when the suite reports no errors).
    Falls back to ``output.text`` when a run has no annotations.

    Raises:
        CIErrorExtractionError: if the token is missing or GitHub could not be
            queried. Callers must treat this as "unknown", not as "clean".
    """
    if not github_token:
        raise CIErrorExtractionError("No GitHub token configured; cannot read CI check runs.")

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            # 1. Get all check runs for the suite
            runs_url = f"{_GITHUB_API}/repos/{repo}/check-suites/{check_suite_id}/check-runs"
            check_runs = await _paginate(client, runs_url, "check_runs", f"check runs for suite {check_suite_id}")

            errors: list[CIError] = []

            for run in check_runs:
                conclusion = run.get("conclusion") or ""
                if conclusion not in _FAILING_CONCLUSIONS:
                    continue  # skip passing / neutral checks

                run_id = run["id"]
                run_name = run["name"]
                output = run.get("output") or {}

                # 2. Fetch annotations for this run
                ann_url = f"{_GITHUB_API}/repos/{repo}/check-runs/{run_id}/annotations"
                annotations = await _paginate(client, ann_url, None, f"annotations for check run {run_id}")

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

    except CIErrorExtractionError:
        raise
    except httpx.HTTPError as exc:
        raise CIErrorExtractionError(f"Could not reach GitHub for suite {check_suite_id}: {exc}") from exc


async def _paginate(
    client: httpx.AsyncClient,
    url: str,
    list_key: str | None,
    what: str,
) -> list[dict[str, Any]]:
    """Collect every page of a GitHub list endpoint.

    *list_key* names the field holding the items for object responses (e.g.
    ``check_runs``); pass ``None`` when the endpoint returns a bare JSON array.
    """
    items: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        resp = await client.get(url, params={"per_page": _PER_PAGE, "page": page})
        _raise_for_status(resp, what)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise CIErrorExtractionError(f"GitHub returned a non-JSON body for {what}: {exc}") from exc

        batch = payload.get(list_key, []) if list_key else payload
        if not isinstance(batch, list):
            raise CIErrorExtractionError(f"GitHub returned an unexpected payload for {what}: {type(batch).__name__}")

        items.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < _PER_PAGE:
            break
    else:
        logger.warning("Stopped paginating %s after %s pages", what, _MAX_PAGES)

    return items


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
