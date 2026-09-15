"""Tests for the shared default-repository rule (``henchmen.utils.repositories``)."""

from __future__ import annotations

import pytest

from henchmen.config.settings import Settings
from henchmen.utils.repositories import default_repository, is_owner_name, qualify_repo


@pytest.mark.parametrize(
    ("repo", "org", "expected"),
    [
        ("acme/webapp", "acme", "acme/webapp"),
        ("acme/webapp", "", "acme/webapp"),
        ("webapp", "acme", "acme/webapp"),
        ("webapp", "", "webapp"),
        ("", "acme", ""),
        ("  webapp ", " acme ", "acme/webapp"),
    ],
)
def test_qualify_repo(repo: str, org: str, expected: str) -> None:
    assert qualify_repo(repo, org) == expected


@pytest.mark.parametrize(
    ("repo", "expected"),
    [("acme/webapp", True), ("webapp", False), ("", False), ("acme/", False), ("a/b/c", False)],
)
def test_is_owner_name(repo: str, expected: bool) -> None:
    assert is_owner_name(repo) is expected


def test_default_repository_reads_settings() -> None:
    settings = Settings(**{"_env_file": None, "github_default_org": "acme", "github_default_repo": "webapp"})
    assert default_repository(settings) == "acme/webapp"
