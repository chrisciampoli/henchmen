"""Tests for Console setup-state persistence."""

import json
import threading
from pathlib import Path

import pytest

from henchmen.console.state import OPTIONAL_STEPS, REQUIRED_STEPS, SetupState, SetupStateStore, SetupStep


def test_missing_file_loads_a_fresh_state(tmp_path: Path) -> None:
    state = SetupStateStore(tmp_path / "setup-state.json").load()
    assert state.current_step == SetupStep.WELCOME
    assert state.completed_steps == []
    assert state.completed is False


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    store = SetupStateStore(tmp_path / "setup-state.json")
    store.save(
        SetupState(
            current_step=SetupStep.GITHUB,
            completed_steps=[SetupStep.WELCOME, SetupStep.AI_PROVIDER],
            choices={"llm_provider": "anthropic"},
        )
    )
    loaded = store.load()
    assert loaded.current_step == SetupStep.GITHUB
    assert loaded.completed_steps == [SetupStep.WELCOME, SetupStep.AI_PROVIDER]
    assert loaded.choices == {"llm_provider": "anthropic"}


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    store = SetupStateStore(tmp_path / "nested" / "setup-state.json")
    store.save(SetupState())
    assert [p.name for p in (tmp_path / "nested").iterdir()] == ["setup-state.json"]


def test_corrupt_file_fails_closed_instead_of_restarting_setup(tmp_path: Path) -> None:
    path = tmp_path / "setup-state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="setup-state.json"):
        SetupStateStore(path).load()


def test_required_steps_are_ai_provider_and_github() -> None:
    assert {SetupStep.AI_PROVIDER, SetupStep.GITHUB} == REQUIRED_STEPS


def test_missing_required_steps_ignores_optional_ones() -> None:
    state = SetupState(completed_steps=[SetupStep.AI_PROVIDER], skipped_steps=[SetupStep.SLACK, SetupStep.JIRA])
    assert state.missing_required_steps() == [SetupStep.GITHUB]


def test_mark_completed_refuses_while_required_steps_are_missing(tmp_path: Path) -> None:
    store = SetupStateStore(tmp_path / "setup-state.json")
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER]))
    with pytest.raises(ValueError, match="github"):
        store.mark_completed()
    assert store.load().completed is False


def test_mark_completed_persists(tmp_path: Path) -> None:
    store = SetupStateStore(tmp_path / "setup-state.json")
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB]))
    assert store.mark_completed().completed is True
    assert json.loads((tmp_path / "setup-state.json").read_text(encoding="utf-8"))["completed"] is True


def test_optional_steps_are_slack_jira_and_first_task() -> None:
    assert {SetupStep.SLACK, SetupStep.JIRA, SetupStep.FIRST_TASK} == OPTIONAL_STEPS
    assert not OPTIONAL_STEPS & REQUIRED_STEPS


def test_record_step_complete_adds_once_in_guide_order_and_unskips(tmp_path: Path) -> None:
    store = SetupStateStore(tmp_path / "setup-state.json")
    store.save(SetupState(completed_steps=[SetupStep.GITHUB], skipped_steps=[SetupStep.SLACK]))
    store.record_step_complete(SetupStep.SLACK)
    store.record_step_complete(SetupStep.AI_PROVIDER)
    returned = store.record_step_complete(SetupStep.AI_PROVIDER)
    loaded = store.load()
    assert loaded.completed_steps == [SetupStep.AI_PROVIDER, SetupStep.GITHUB, SetupStep.SLACK]
    assert loaded.skipped_steps == []
    assert returned.completed_steps == loaded.completed_steps


def test_set_server_choices_merges_and_persists(tmp_path: Path) -> None:
    store = SetupStateStore(tmp_path / "setup-state.json")
    store.set_server_choices({"github_app_slug": "henchmen-laptop"})
    returned = store.set_server_choices({"github_account": "acme", "github_app_slug": "henchmen-laptop-2"})
    loaded = store.load()
    assert loaded.server_choices == {"github_app_slug": "henchmen-laptop-2", "github_account": "acme"}
    assert returned.server_choices == loaded.server_choices
    assert loaded.choices == {}


def test_state_files_written_before_server_choices_still_load(tmp_path: Path) -> None:
    path = tmp_path / "setup-state.json"
    path.write_text('{"current_step": "github", "completed_steps": ["ai_provider"]}', encoding="utf-8")
    assert SetupStateStore(path).load().server_choices == {}


def test_concurrent_record_step_complete_and_set_server_choices_both_persist(tmp_path: Path) -> None:
    """Neither write may be lost when both race on the same store instance (P7 concurrency ruling)."""
    store = SetupStateStore(tmp_path / "setup-state.json")
    barrier = threading.Barrier(2)

    def record() -> None:
        barrier.wait()
        for step in (SetupStep.AI_PROVIDER, SetupStep.GITHUB, SetupStep.SLACK, SetupStep.JIRA):
            store.record_step_complete(step)

    def choices() -> None:
        barrier.wait()
        for i in range(20):
            store.set_server_choices({f"key_{i}": f"value_{i}"})

    threads = [threading.Thread(target=record), threading.Thread(target=choices)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    loaded = store.load()
    assert set(loaded.completed_steps) == {SetupStep.AI_PROVIDER, SetupStep.GITHUB, SetupStep.SLACK, SetupStep.JIRA}
    assert loaded.server_choices == {f"key_{i}": f"value_{i}" for i in range(20)}
