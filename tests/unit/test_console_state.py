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


def test_record_step_incomplete_removes_only_that_step(tmp_path: Path) -> None:
    store = SetupStateStore(tmp_path / "setup-state.json")
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB], choices={"llm_provider": "x"}))
    returned = store.record_step_incomplete(SetupStep.GITHUB)
    loaded = store.load()
    assert loaded.completed_steps == [SetupStep.AI_PROVIDER]
    assert loaded.choices == {"llm_provider": "x"}
    assert returned.completed_steps == loaded.completed_steps


def test_record_step_incomplete_of_a_step_that_is_not_complete_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "setup-state.json"
    store = SetupStateStore(path)
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER]))
    before = path.read_bytes()
    returned = store.record_step_incomplete(SetupStep.GITHUB)
    assert path.read_bytes() == before
    assert returned.completed_steps == [SetupStep.AI_PROVIDER]
    fresh = SetupStateStore(tmp_path / "other.json")
    assert fresh.record_step_incomplete(SetupStep.GITHUB).completed_steps == []
    assert not (tmp_path / "other.json").exists()


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


def test_update_client_fields_sets_current_step_skips_and_choices(tmp_path: Path) -> None:
    store = SetupStateStore(tmp_path / "setup-state.json")
    result = store.update_client_fields(
        current_step=SetupStep.GITHUB, skipped_steps=[SetupStep.SLACK], choices={"llm_provider": "anthropic"}
    )
    loaded = store.load()
    assert loaded.current_step == SetupStep.GITHUB
    assert loaded.skipped_steps == [SetupStep.SLACK]
    assert loaded.choices == {"llm_provider": "anthropic"}
    assert result.current_step == loaded.current_step


def test_update_client_fields_drops_a_skip_for_a_step_already_completed(tmp_path: Path) -> None:
    """Completion always wins over a (now stale) earlier skip (Ruling 3)."""
    store = SetupStateStore(tmp_path / "setup-state.json")
    store.save(SetupState(completed_steps=[SetupStep.SLACK]))
    result = store.update_client_fields(
        current_step=SetupStep.WELCOME, skipped_steps=[SetupStep.SLACK, SetupStep.JIRA], choices={}
    )
    assert result.skipped_steps == [SetupStep.JIRA]
    assert store.load().skipped_steps == [SetupStep.JIRA]


def test_update_client_fields_does_not_lose_a_completion_recorded_between_load_and_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completion that lands between this method's own load and save must survive --
    never overwritten by a save built from a stale, pre-completion snapshot (Ruling 2:
    put_state must be lock-guarded the same way as record_step_complete). Simulated
    deterministically: `load` is patched so its first call -- the one `update_client_fields`
    itself makes -- also persists a "concurrent" completion via the store's own (lock-free)
    `save`, since going through the locked `record_step_complete` here would try to
    re-enter the lock this call already holds."""
    store = SetupStateStore(tmp_path / "setup-state.json")
    real_load = store.load
    calls = {"n": 0}

    def racing_load() -> SetupState:
        state = real_load()
        calls["n"] += 1
        if calls["n"] == 1:
            state = store.save(state.model_copy(update={"completed_steps": [SetupStep.GITHUB]}))
        return state

    monkeypatch.setattr(store, "load", racing_load)

    result = store.update_client_fields(current_step=SetupStep.WELCOME, skipped_steps=[], choices={"a": "b"})

    assert calls["n"] == 1
    assert result.completed_steps == [SetupStep.GITHUB]
    assert result.current_step == SetupStep.WELCOME
    assert result.choices == {"a": "b"}
