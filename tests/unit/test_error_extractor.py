"""Unit tests for the CI error extractor."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_annotation(
    check_name="type_check", path="src/foo.py", start_line=42, message="Incompatible types", annotation_level="failure"
):
    return {
        "path": path,
        "start_line": start_line,
        "message": message,
        "annotation_level": annotation_level,
    }


def _make_check_run(name="type_check", conclusion="failure", run_id=101, text="Error details here"):
    return {
        "id": run_id,
        "name": name,
        "conclusion": conclusion,
        "output": {
            "text": text,
            "annotations_count": 0,
        },
    }


def _make_async_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


# ---------------------------------------------------------------------------
# extract_ci_errors
# ---------------------------------------------------------------------------


class TestExtractCIErrors:
    @pytest.mark.asyncio
    async def test_annotation_extraction(self):
        """Annotations from a failing check run are returned as CIErrors."""
        from henchmen.forge.error_extractor import extract_ci_errors

        check_runs_resp = _make_async_response(
            {"check_runs": [_make_check_run(name="type_check", conclusion="failure", run_id=1)]}
        )
        annotations_resp = _make_async_response(
            [_make_annotation(check_name="type_check", path="src/foo.py", start_line=42, message="Incompatible types")]
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[check_runs_resp, annotations_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("henchmen.forge.error_extractor.httpx.AsyncClient", return_value=mock_client):
            errors = await extract_ci_errors("org/repo", 999, "gh-token")

        assert len(errors) == 1
        assert errors[0].check_name == "type_check"
        assert errors[0].file_path == "src/foo.py"
        assert errors[0].line == 42
        assert errors[0].message == "Incompatible types"

    @pytest.mark.asyncio
    async def test_fallback_to_output_text(self):
        """When annotations list is empty, falls back to output.text."""
        from henchmen.forge.error_extractor import extract_ci_errors

        check_runs_resp = _make_async_response(
            {
                "check_runs": [
                    _make_check_run(name="lint", conclusion="failure", run_id=2, text="E302 expected 2 blank lines")
                ]
            }
        )
        empty_annotations = _make_async_response([])

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[check_runs_resp, empty_annotations])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("henchmen.forge.error_extractor.httpx.AsyncClient", return_value=mock_client):
            errors = await extract_ci_errors("org/repo", 999, "gh-token")

        assert len(errors) == 1
        assert errors[0].check_name == "lint"
        assert errors[0].file_path == ""
        assert errors[0].line is None
        assert "E302" in errors[0].message

    @pytest.mark.asyncio
    async def test_skips_passing_checks(self):
        """Check runs with conclusion 'success' are skipped."""
        from henchmen.forge.error_extractor import extract_ci_errors

        check_runs_resp = _make_async_response(
            {
                "check_runs": [
                    _make_check_run(name="tests", conclusion="success", run_id=3),
                    _make_check_run(name="lint", conclusion="failure", run_id=4, text="Lint error"),
                ]
            }
        )
        empty_annotations = _make_async_response([])

        mock_client = AsyncMock()
        # Only one annotation call because "tests" is passing
        mock_client.get = AsyncMock(side_effect=[check_runs_resp, empty_annotations])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("henchmen.forge.error_extractor.httpx.AsyncClient", return_value=mock_client):
            errors = await extract_ci_errors("org/repo", 999, "gh-token")

        # Only errors from "lint", not "tests"
        assert all(e.check_name == "lint" for e in errors)

    @pytest.mark.asyncio
    async def test_api_error_returns_empty(self):
        """Any exception returns an empty list."""
        from henchmen.forge.error_extractor import extract_ci_errors

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network error"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("henchmen.forge.error_extractor.httpx.AsyncClient", return_value=mock_client):
            errors = await extract_ci_errors("org/repo", 999, "gh-token")

        assert errors == []

    @pytest.mark.asyncio
    async def test_no_line_number(self):
        """Annotations without a start_line set line to None."""
        from henchmen.forge.error_extractor import extract_ci_errors

        annotation = _make_annotation(path="README.md", start_line=None, message="Spelling error")
        check_runs_resp = _make_async_response(
            {"check_runs": [_make_check_run(name="spell_check", conclusion="failure", run_id=5)]}
        )
        annotations_resp = _make_async_response([annotation])

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[check_runs_resp, annotations_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("henchmen.forge.error_extractor.httpx.AsyncClient", return_value=mock_client):
            errors = await extract_ci_errors("org/repo", 999, "gh-token")

        assert len(errors) == 1
        assert errors[0].line is None


# ---------------------------------------------------------------------------
# format_errors_for_operative
# ---------------------------------------------------------------------------


class TestFormatErrorsForOperative:
    def test_empty_list_returns_empty_string(self):
        from henchmen.forge.error_extractor import format_errors_for_operative

        assert format_errors_for_operative([]) == ""

    def test_groups_by_check_name(self):
        from henchmen.forge.error_extractor import CIError, format_errors_for_operative

        errors = [
            CIError(check_name="type_check", file_path="src/a.py", line=10, message="Type error A", severity="failure"),
            CIError(check_name="lint", file_path="src/b.py", line=20, message="Lint error B", severity="failure"),
            CIError(check_name="type_check", file_path="src/c.py", line=30, message="Type error C", severity="failure"),
        ]
        result = format_errors_for_operative(errors)

        # Both check names appear as headings
        assert "type_check" in result
        assert "lint" in result

        # File:line references are present
        assert "src/a.py:10" in result
        assert "src/b.py:20" in result
        assert "src/c.py:30" in result

    def test_error_without_line_number(self):
        from henchmen.forge.error_extractor import CIError, format_errors_for_operative

        errors = [
            CIError(
                check_name="lint", file_path="src/foo.py", line=None, message="Missing newline", severity="warning"
            ),
        ]
        result = format_errors_for_operative(errors)
        assert "src/foo.py" in result
        assert "Missing newline" in result
