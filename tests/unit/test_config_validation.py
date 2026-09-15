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


def test_overrides_only_allow_the_dispatch_api_token_key(tmp_path: Path) -> None:
    """overrides exists solely to validate a pending Dispatch token; nothing else may use it."""
    config = _config(tmp_path, "HENCHMEN_PROVIDER=local\n")
    with pytest.raises(ValueError, match="HENCHMEN_PROVIDER"):
        settings_problems(config, overrides={"HENCHMEN_PROVIDER": "gcp"})


def test_a_seeded_alias_masks_only_its_own_key_not_a_real_bare_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D10: seeding HENCHMEN_GITHUB_TOKEN must not hide a genuine bare GITHUB_TOKEN from the environment."""
    monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", "seeded-default")
    monkeypatch.setenv("GITHUB_TOKEN", "real-bare-token")
    config = _config(tmp_path, "HENCHMEN_PROVIDER=local\n")
    settings, _ = settings_problems(config, seeded_env={"HENCHMEN_GITHUB_TOKEN": "seeded-default"})
    assert settings is not None
    assert settings.github_token == "real-bare-token"


def test_a_seeded_alias_is_still_masked_itself(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", "seeded-default")
    config = _config(tmp_path, "HENCHMEN_PROVIDER=local\nHENCHMEN_GITHUB_TOKEN=from-file\n")
    settings, _ = settings_problems(config, seeded_env={"HENCHMEN_GITHUB_TOKEN": "seeded-default"})
    assert settings is not None and settings.github_token == "from-file"


class TestDesktopDispatchToken:
    """C8: run mode on a desktop install needs a usable Dispatch API token."""

    @pytest.fixture(autouse=True)
    def _desktop(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))

    @pytest.mark.parametrize(
        "line",
        ["", "HENCHMEN_DISPATCH_API_TOKEN=\n", "HENCHMEN_DISPATCH_API_TOKEN=placeholder-replace-with-a-real-value\n"],
    )
    def test_an_empty_or_placeholder_token_is_a_problem_naming_the_fix(self, tmp_path: Path, line: str) -> None:
        settings, problems = settings_problems(_config(tmp_path, "HENCHMEN_PROVIDER=local\n" + line))
        assert settings is not None
        (problem,) = [p for p in problems if "HENCHMEN_DISPATCH_API_TOKEN" in p]
        assert "environment variable" in problem and "apply setup again" in problem

    @pytest.mark.parametrize("value", ["", "placeholder-replace-with-a-real-value"])
    def test_a_blank_or_placeholder_environment_variable_shadowing_the_file_is_a_problem(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
    ) -> None:
        monkeypatch.setenv("HENCHMEN_DISPATCH_API_TOKEN", value)
        config = _config(tmp_path, "HENCHMEN_PROVIDER=local\nHENCHMEN_DISPATCH_API_TOKEN=a-real-token-in-the-file\n")
        _, problems = settings_problems(config)
        assert any("HENCHMEN_DISPATCH_API_TOKEN is empty" in p for p in problems)

    def test_a_usable_token_is_not_a_problem(self, tmp_path: Path) -> None:
        _, problems = settings_problems(
            _config(tmp_path, "HENCHMEN_PROVIDER=local\nHENCHMEN_DISPATCH_API_TOKEN=real\n")
        )
        assert not any("HENCHMEN_DISPATCH_API_TOKEN" in p for p in problems)

    def test_a_pending_override_ranks_like_the_file_below_a_blank_environment_variable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HENCHMEN_DISPATCH_API_TOKEN", "")
        config = _config(tmp_path, "HENCHMEN_PROVIDER=local\n")
        settings, problems = settings_problems(config, overrides={"HENCHMEN_DISPATCH_API_TOKEN": "fresh-token"})
        assert settings is not None and settings.dispatch_api_token == ""
        assert any("HENCHMEN_DISPATCH_API_TOKEN is empty" in p for p in problems)


def test_an_empty_token_off_a_desktop_install_is_not_a_validation_problem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
    _, problems = settings_problems(_config(tmp_path, "HENCHMEN_PROVIDER=local\n"))
    assert not any("HENCHMEN_DISPATCH_API_TOKEN" in p for p in problems)
