"""Tests for the uncached GitHub endpoint resolver (``henchmen.utils.endpoints``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from henchmen.utils.endpoints import EndpointError, GitHubEndpoints, resolve_github_endpoints


@pytest.fixture(autouse=True)
def _no_ambient_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HENCHMEN_GITHUB_API_URL", raising=False)
    monkeypatch.delenv("HENCHMEN_GITHUB_WEB_URL", raising=False)


def _config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "henchmen.env"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_a_file(tmp_path: Path) -> None:
    expected = GitHubEndpoints(api_url="https://api.github.com", web_url="https://github.com")
    assert resolve_github_endpoints(tmp_path / "missing.env") == expected
    assert resolve_github_endpoints(None) == expected


def test_blank_values_mean_the_default(tmp_path: Path) -> None:
    config = _config(tmp_path, "HENCHMEN_GITHUB_API_URL=\nHENCHMEN_GITHUB_WEB_URL=   \n")
    assert resolve_github_endpoints(config) == GitHubEndpoints(
        api_url="https://api.github.com", web_url="https://github.com"
    )


def test_file_values_are_read_and_trailing_slashes_dropped(tmp_path: Path) -> None:
    config = _config(
        tmp_path, "HENCHMEN_GITHUB_API_URL=http://fakes:9000/api/\nHENCHMEN_GITHUB_WEB_URL=http://127.0.0.1:9/\n"
    )
    assert resolve_github_endpoints(config) == GitHubEndpoints(
        api_url="http://fakes:9000/api", web_url="http://127.0.0.1:9"
    )


def test_unrelated_invalid_settings_do_not_matter(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "HENCHMEN_PROVIDER=gcp\nHENCHMEN_LOCAL_SERVE_PORT=not-a-port\nHENCHMEN_GITHUB_WEB_URL=https://ghe.example.test\n",
    )
    assert resolve_github_endpoints(config).web_url == "https://ghe.example.test"


def test_each_call_reads_the_file_again(tmp_path: Path) -> None:
    config = _config(tmp_path, "HENCHMEN_GITHUB_WEB_URL=https://one.example.test\n")
    assert resolve_github_endpoints(config).web_url == "https://one.example.test"
    config.write_text("HENCHMEN_GITHUB_WEB_URL=https://two.example.test\n", encoding="utf-8")
    assert resolve_github_endpoints(config).web_url == "https://two.example.test"


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("HENCHMEN_GITHUB_API_URL=http://api.github.com\n", "github_api_url"),
        ("HENCHMEN_GITHUB_WEB_URL=http://10.0.0.5\n", "github_web_url"),
        ("HENCHMEN_GITHUB_WEB_URL=https://chris:hunter2-pw@github.com\n", "github_web_url"),
        ("HENCHMEN_GITHUB_API_URL=ftp://api.github.com\n", "github_api_url"),
    ],
)
def test_an_invalid_url_raises_naming_the_field_but_not_the_value(tmp_path: Path, text: str, field: str) -> None:
    with pytest.raises(EndpointError) as exc_info:
        resolve_github_endpoints(_config(tmp_path, text))
    assert exc_info.value.field == field
    value = text.split("=", 1)[1].strip()
    assert value not in str(exc_info.value)
    assert "hunter2" not in str(exc_info.value)


def test_the_environment_outranks_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, "HENCHMEN_GITHUB_WEB_URL=https://file.example.test\n")
    monkeypatch.setenv("HENCHMEN_GITHUB_WEB_URL", "https://env.example.test")
    assert resolve_github_endpoints(config).web_url == "https://env.example.test"


def test_a_seeded_environment_value_yields_to_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HENCHMEN_GITHUB_WEB_URL", "https://seeded.example.test")
    seeded = {"HENCHMEN_GITHUB_WEB_URL": "https://seeded.example.test"}
    with_file = _config(tmp_path, "HENCHMEN_GITHUB_WEB_URL=https://file.example.test\n")
    assert resolve_github_endpoints(with_file, seeded_env=seeded).web_url == "https://file.example.test"
    without_key = tmp_path / "other.env"
    without_key.write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")
    assert resolve_github_endpoints(without_key, seeded_env=seeded).web_url == "https://seeded.example.test"


def test_an_invalid_environment_value_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HENCHMEN_GITHUB_API_URL", "http://api.github.com")
    with pytest.raises(EndpointError, match="github_api_url"):
        resolve_github_endpoints(_config(tmp_path, "HENCHMEN_GITHUB_API_URL=https://api.github.com\n"))


def test_an_unreadable_config_file_fails_closed_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from henchmen.utils import endpoints

    config = _config(tmp_path, "HENCHMEN_GITHUB_WEB_URL=https://ghe.example.test\n")

    def denied(*args: object, **kwargs: object) -> dict[str, str]:
        raise PermissionError(13, "Permission denied", str(config))

    monkeypatch.setattr(endpoints, "dotenv_values", denied)
    with pytest.raises(EndpointError) as exc_info:
        resolve_github_endpoints(config)
    assert exc_info.value.field == "github_api_url"
    assert "PermissionError" in str(exc_info.value)
    assert exc_info.value.__cause__ is None
