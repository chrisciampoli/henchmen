"""Apply-time validation sees the configuration run mode will see (D-P8, amendment A5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from henchmen.config.validation import settings_problems

SEEDED = {"HENCHMEN_PROVIDER": "local"}


def _config(tmp_path: Path, text: str) -> tuple[str]:
    path = tmp_path / "henchmen.env"
    path.write_text(text, encoding="utf-8")
    return (str(path),)


def test_a_seeded_default_does_not_hide_a_provider_the_file_sets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HENCHMEN_PROVIDER", "local")  # what _serve seeded at startup
    settings, problems = settings_problems(_config(tmp_path, "HENCHMEN_PROVIDER=gcp\n"), seeded_env=SEEDED)
    assert settings is None
    assert any("HENCHMEN_GCP_PROJECT_ID" in problem for problem in problems)


def test_without_masking_the_seeded_environment_would_win(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Documents the bug D-P8 fixes."""
    monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
    settings, problems = settings_problems(_config(tmp_path, "HENCHMEN_PROVIDER=gcp\n"))
    assert settings is not None and settings.provider == "local"
    assert problems == []


def test_the_seeded_default_applies_when_the_file_leaves_the_key_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
    settings, problems = settings_problems(_config(tmp_path, "HENCHMEN_LLM_PROVIDER=local\n"), seeded_env=SEEDED)
    assert settings is not None and settings.provider == "local"
    assert problems == []


def test_environment_values_that_were_not_seeded_still_apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
    monkeypatch.setenv("HENCHMEN_LOCAL_SERVE_PORT", "9001")  # e.g. docker run -e
    settings, _ = settings_problems(_config(tmp_path, "HENCHMEN_LOCAL_SERVE_PORT=9555\n"), seeded_env=SEEDED)
    assert settings is not None and settings.local_serve_port == 9001


def test_runtime_problems_are_returned_with_the_settings(tmp_path: Path) -> None:
    config = _config(tmp_path, "HENCHMEN_PROVIDER=local\nHENCHMEN_LLM_PROVIDER=anthropic\n")
    settings, problems = settings_problems(config, seeded_env=SEEDED)
    assert settings is not None
    assert any("HENCHMEN_ANTHROPIC_API_KEY" in problem for problem in problems)


def test_problems_never_echo_the_rejected_value(tmp_path: Path) -> None:
    config = _config(tmp_path, "HENCHMEN_PROVIDER=local\nHENCHMEN_OPERATIVE_TASK_COST_CEILING_USD=sk-ant-supersecret\n")
    settings, problems = settings_problems(config)
    assert settings is None
    assert problems and all("supersecret" not in problem for problem in problems)
    assert any("operative_task_cost_ceiling_usd" in problem for problem in problems)


def test_overrides_are_applied_regardless_of_the_file_or_seeded_env(tmp_path: Path) -> None:
    """A pending Dispatch API token is validated as if it were already written (ruling P6)."""
    config = _config(tmp_path, "HENCHMEN_PROVIDER=local\nHENCHMEN_DISPATCH_API_TOKEN=stale\n")
    settings, problems = settings_problems(config, overrides={"HENCHMEN_DISPATCH_API_TOKEN": "fresh-token"})
    assert settings is not None
    assert settings.dispatch_api_token == "fresh-token"
    assert problems == []
