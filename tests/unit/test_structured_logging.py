"""Unit tests for the Cloud Logging structured metric emitters."""

import json

import pytest

from henchmen.observability.structured_logging import (
    emit_cost_exceeded,
    emit_metric,
    emit_operative_status,
    emit_task_completed,
    emit_watchdog_event,
)


def _entries(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    """Every JSON line written to stdout since the last capture."""
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


class TestEmitMetric:
    def test_writes_one_cloud_logging_json_line(self, capsys: pytest.CaptureFixture[str]):
        emit_metric("task.completed", {"task_id": "t-1"}, value=0.35, severity="warning")

        (entry,) = _entries(capsys)
        assert entry == {
            "severity": "WARNING",
            "message": "metric:task.completed",
            "metric_name": "task.completed",
            "metric_value": 0.35,
            "metric_labels": {"task_id": "t-1"},
        }

    def test_labels_are_stringified(self, capsys: pytest.CaptureFixture[str]):
        """Log-based metric extractors reject non-string label values."""
        emit_metric("watchdog.sweep", {"recovered": 2, "ceiling": 6.0, "flag": True})

        (entry,) = _entries(capsys)
        assert entry["metric_labels"] == {"recovered": "2", "ceiling": "6.0", "flag": "True"}

    def test_unknown_severity_defaults_to_info(self, capsys: pytest.CaptureFixture[str]):
        emit_metric("x", severity="loud")

        (entry,) = _entries(capsys)
        assert entry["severity"] == "INFO"
        assert entry["metric_labels"] == {}
        assert entry["metric_value"] == 1.0

    def test_emission_failure_never_raises(self, monkeypatch: pytest.MonkeyPatch):
        def _broken_print(*_args: object, **_kwargs: object) -> None:
            raise OSError("stdout closed")

        monkeypatch.setattr("builtins.print", _broken_print)
        emit_metric("task.completed")  # must not raise


class TestEmitters:
    def test_task_completed_carries_model_and_duration(self, capsys: pytest.CaptureFixture[str]):
        emit_task_completed(
            task_id="t-1",
            scheme_id="bugfix_standard",
            final_status="pr_created",
            cost_usd=1.5,
            wall_clock_seconds=42.0,
            model_name="gemini-2.5-pro",
        )

        completed, duration = _entries(capsys)
        assert completed["metric_name"] == "task.completed"
        assert completed["metric_value"] == 1.5
        assert completed["metric_labels"] == {
            "task_id": "t-1",
            "scheme_id": "bugfix_standard",
            "final_status": "pr_created",
            "model_name": "gemini-2.5-pro",
        }
        assert duration["metric_name"] == "task.duration_seconds"
        assert duration["metric_value"] == 42.0

    def test_cost_exceeded_is_a_warning(self, capsys: pytest.CaptureFixture[str]):
        emit_cost_exceeded("t-1", estimated_cost=7.25, ceiling=6.0)

        (entry,) = _entries(capsys)
        assert entry["severity"] == "WARNING"
        assert entry["metric_value"] == 7.25
        assert entry["metric_labels"] == {"task_id": "t-1", "ceiling_usd": "6.0"}

    def test_operative_status_emits_tokens_only_when_present(self, capsys: pytest.CaptureFixture[str]):
        emit_operative_status("t-1", "implement_fix", "completed")
        assert [e["metric_name"] for e in _entries(capsys)] == ["operative.status"]

        emit_operative_status("t-1", "implement_fix", "completed", input_tokens=100, output_tokens=20)
        status, tokens = _entries(capsys)
        assert status["metric_name"] == "operative.status"
        assert tokens["metric_name"] == "operative.tokens"
        assert tokens["metric_value"] == 120.0

    @pytest.mark.parametrize(("stalled", "severity"), [(0, "INFO"), (3, "WARNING")])
    def test_watchdog_severity_tracks_stalled_count(
        self, capsys: pytest.CaptureFixture[str], stalled: int, severity: str
    ):
        emit_watchdog_event(stalled_count=stalled, recovered=1, escalated=0)

        (entry,) = _entries(capsys)
        assert entry["severity"] == severity
        assert entry["metric_value"] == float(stalled)
