"""Unit tests for ``henchmen.forge.silent_failure_detector``.

Covers the hot-path pattern detection in ``SilentFailureDetector.scan_diff``.
Previously untested; see expert-panel finding R6.
"""

from henchmen.forge.silent_failure_detector import Finding, SilentFailureDetector


class TestSilentFailureDetector:
    """Behavioural tests for ``SilentFailureDetector``."""

    def test_construction_returns_empty_findings_for_empty_diff(self):
        """A freshly constructed detector must report no findings on an empty diff."""
        detector = SilentFailureDetector()
        assert detector.scan_diff("") == []

    def test_detects_empty_catch_block_as_critical(self):
        """A known-bad diff introducing ``catch (e) {}`` is flagged critical."""
        diff = (
            "diff --git a/src/foo.ts b/src/foo.ts\n"
            "--- a/src/foo.ts\n"
            "+++ b/src/foo.ts\n"
            "@@\n"
            "+try { doThing(); } catch (e) {}\n"
        )
        detector = SilentFailureDetector()
        findings = detector.scan_diff(diff)

        empty_catch = [f for f in findings if f.pattern == "empty_catch"]
        assert len(empty_catch) >= 1, f"Expected empty_catch finding, got: {[f.pattern for f in findings]}"
        assert empty_catch[0].severity == "critical"
        assert empty_catch[0].file == "src/foo.ts"

    def test_detects_bare_except_pass_as_critical(self):
        """Python ``except: pass`` pattern is flagged critical."""
        diff = (
            "diff --git a/src/foo.py b/src/foo.py\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@\n"
            "+try:\n"
            "+    do_thing()\n"
            "+except Exception:\n"
            "+    pass\n"
        )
        detector = SilentFailureDetector()
        findings = detector.scan_diff(diff)

        catch_pass = [f for f in findings if f.pattern == "catch_pass"]
        assert len(catch_pass) >= 1
        assert catch_pass[0].severity == "critical"

    def test_clean_diff_reports_no_critical_findings(self):
        """A well-formed diff with no silent-failure patterns is clean."""
        diff = (
            "diff --git a/src/util.py b/src/util.py\n"
            "--- a/src/util.py\n"
            "+++ b/src/util.py\n"
            "@@\n"
            "+def add(a: int, b: int) -> int:\n"
            "+    return a + b\n"
        )
        detector = SilentFailureDetector()
        findings = detector.scan_diff(diff)

        criticals = [f for f in findings if f.severity == "critical"]
        assert criticals == [], f"Expected no critical findings, got: {criticals}"

    def test_noop_change_detected_when_added_equals_removed(self):
        """A diff that only reorders identical lines is flagged as a noop."""
        diff = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@\n-line_a\n-line_b\n+line_b\n+line_a\n"
        detector = SilentFailureDetector()
        findings = detector.scan_diff(diff)

        noops = [f for f in findings if f.pattern == "noop_change"]
        assert len(noops) == 1
        assert noops[0].severity == "warning"

    def test_password_assignment_after_except_is_not_catch_pass(self):
        """``password = None`` after an ``except`` line must not read as ``pass``."""
        diff = (
            "diff --git a/src/foo.py b/src/foo.py\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@\n"
            "+try:\n"
            "+    load()\n"
            "+except ValueError:\n"
            "+    password = None\n"
            "+    logger.warning('bad value')\n"
        )
        findings = SilentFailureDetector().scan_diff(diff)

        assert [f.pattern for f in findings if f.pattern == "catch_pass"] == []

    def test_passthrough_call_after_except_is_not_catch_pass(self):
        diff = (
            "diff --git a/src/foo.py b/src/foo.py\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@\n"
            "+except KeyError:\n"
            "+    passthrough(x)\n"
        )
        findings = SilentFailureDetector().scan_diff(diff)

        assert [f.pattern for f in findings if f.pattern == "catch_pass"] == []

    def test_inline_except_pass_is_still_detected(self):
        diff = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@\n+except Exception: pass\n"
        findings = SilentFailureDetector().scan_diff(diff)

        assert any(f.pattern == "catch_pass" and f.severity == "critical" for f in findings)

    def test_multiline_catch_without_logging_is_detected(self):
        """Real catch blocks span lines; the pattern must not need a one-liner."""
        diff = (
            "diff --git a/src/foo.ts b/src/foo.ts\n"
            "--- a/src/foo.ts\n"
            "+++ b/src/foo.ts\n"
            "@@\n"
            "+try {\n"
            "+  a();\n"
            "+} catch (e) {\n"
            "+  x = 1;\n"
            "+}\n"
        )
        findings = SilentFailureDetector().scan_diff(diff)

        assert any(f.pattern == "no_error_logging" for f in findings)

    def test_multiline_catch_with_logging_is_not_flagged(self):
        diff = (
            "diff --git a/src/foo.ts b/src/foo.ts\n"
            "--- a/src/foo.ts\n"
            "+++ b/src/foo.ts\n"
            "@@\n"
            "+try {\n"
            "+  a();\n"
            "+} catch (e) {\n"
            "+  console.error(e);\n"
            "+}\n"
        )
        findings = SilentFailureDetector().scan_diff(diff)

        assert [f for f in findings if f.pattern == "no_error_logging"] == []

    def test_retry_mention_with_backoff_is_not_flagged(self):
        """A bare mention of retries must not produce a 'no backoff' warning."""
        diff = (
            "diff --git a/src/client.py b/src/client.py\n"
            "--- a/src/client.py\n"
            "+++ b/src/client.py\n"
            "@@\n"
            "+max_retries = 3  # retry with exponential backoff via tenacity\n"
        )
        findings = SilentFailureDetector().scan_diff(diff)

        assert [f for f in findings if f.pattern == "retry_no_backoff"] == []

    def test_retry_loop_without_backoff_is_flagged(self):
        diff = (
            "diff --git a/src/client.py b/src/client.py\n"
            "--- a/src/client.py\n"
            "+++ b/src/client.py\n"
            "@@\n"
            "+while retry_count < 5:\n"
            "+    call_api()\n"
        )
        findings = SilentFailureDetector().scan_diff(diff)

        assert any(f.pattern == "retry_no_backoff" for f in findings)

    def test_test_fixture_credentials_are_not_critical(self):
        """A placeholder credential in a test file must not hard-fail CI."""
        diff = (
            "diff --git a/tests/test_auth.py b/tests/test_auth.py\n"
            "--- a/tests/test_auth.py\n"
            "+++ b/tests/test_auth.py\n"
            '@@\n+api_key = "test-api-key-1234"\n'
        )
        findings = SilentFailureDetector().scan_diff(diff)

        assert [f for f in findings if f.pattern == "hardcoded_secret"] == []

    def test_placeholder_secret_in_source_is_downgraded_to_warning(self):
        diff = (
            "diff --git a/src/client.py b/src/client.py\n"
            "--- a/src/client.py\n"
            "+++ b/src/client.py\n"
            '@@\n+token = "x-access-token"\n'
        )
        findings = SilentFailureDetector().scan_diff(diff)

        secrets = [f for f in findings if f.pattern == "hardcoded_secret"]
        assert len(secrets) == 1
        assert secrets[0].severity == "warning"

    def test_real_looking_secret_in_source_is_still_critical(self):
        diff = (
            "diff --git a/src/client.py b/src/client.py\n"
            "--- a/src/client.py\n"
            "+++ b/src/client.py\n"
            '@@\n+api_key = "ghp_9fA2kQ1zLp0RtYuIoP3s"\n'
        )
        findings = SilentFailureDetector().scan_diff(diff)

        assert any(f.pattern == "hardcoded_secret" and f.severity == "critical" for f in findings)

    def test_patterns_have_no_dead_entries(self):
        """Every PATTERNS entry must carry a usable regex (the noop_change stub is gone)."""
        for pattern in SilentFailureDetector.PATTERNS:
            assert isinstance(pattern["regex"], str) and pattern["regex"]
        assert "noop_change" not in {p["name"] for p in SilentFailureDetector.PATTERNS}

    def test_format_findings_empty_list(self):
        """format_findings returns a recognisable string for an empty list."""
        detector = SilentFailureDetector()
        text = detector.format_findings([])
        assert "No silent failure patterns" in text

    def test_format_findings_includes_pattern_name(self):
        """format_findings renders each finding's pattern name."""
        detector = SilentFailureDetector()
        finding = Finding(
            severity="critical",
            pattern="empty_catch",
            description="Empty catch block — errors are silently swallowed",
            file="src/foo.ts",
            line_hint="~line 1 in added content",
        )
        text = detector.format_findings([finding])
        assert "empty_catch" in text
        assert "src/foo.ts" in text
