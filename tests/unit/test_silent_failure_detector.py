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
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@\n"
            "-line_a\n"
            "-line_b\n"
            "+line_b\n"
            "+line_a\n"
        )
        detector = SilentFailureDetector()
        findings = detector.scan_diff(diff)

        noops = [f for f in findings if f.pattern == "noop_change"]
        assert len(noops) == 1
        assert noops[0].severity == "warning"

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
