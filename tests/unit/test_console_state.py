"""Tests for Console setup-state persistence."""

import json
from pathlib import Path

import pytest

from henchmen.console.state import REQUIRED_STEPS, SetupState, SetupStateStore, SetupStep


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
