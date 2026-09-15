"""Tests for the Console's Jira step."""

from __future__ import annotations

from pathlib import Path

import pytest

from henchmen.cli import checks
from henchmen.cli.checks import CheckResult, CheckStatus, JiraField, JiraProject
from henchmen.config.settings import Settings
from henchmen.console.state import SetupStep
from tests.unit.console_harness import ConsoleHarness, make_harness

BASE = "/console/api/steps/jira"
TOKEN = "ATATT3x-secret-jira-token"
CREDENTIALS = {"base_url": "https://acme.atlassian.net/", "email": "pm@acme.com", "api_token": TOKEN}
CHOICES = {
    "project_key": "WEB",
    "repo_field": "customfield_10042",
    "branch_field": "customfield_10043",
    "intake_label": "henchmen",
}


class FakeJira:
    def __init__(self) -> None:
        self.ok = True
        self.calls: list[tuple[str, str, str, str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def check(base_url: str, email: str, api_token: str, *, timeout: float = checks.DEFAULT_TIMEOUT) -> CheckResult:
            self.calls.append(("check", base_url, email, api_token))
            if self.ok:
                return CheckResult("Jira", CheckStatus.OK, "authenticated as Pat Manager")
            return CheckResult(
                "Jira",
                CheckStatus.FAIL,
                f"{base_url} returned HTTP 401",
                hint="Create an API token at id.atlassian.com/manage-profile/security/api-tokens",
            )

        def projects(
            base_url: str, email: str, api_token: str, *, timeout: float = checks.DEFAULT_TIMEOUT
        ) -> list[JiraProject]:
            return [JiraProject(key="API", name="API"), JiraProject(key="WEB", name="Web app")]

        def fields(
            base_url: str, email: str, api_token: str, *, timeout: float = checks.DEFAULT_TIMEOUT
        ) -> list[JiraField]:
            return [
                JiraField(id="customfield_10043", name="Branch", custom=True),
                JiraField(id="customfield_10042", name="Repository", custom=True),
                JiraField(id="summary", name="Summary", custom=False),
            ]

        monkeypatch.setattr(checks, "check_jira", check)
        monkeypatch.setattr(checks, "list_jira_projects", projects)
        monkeypatch.setattr(checks, "list_jira_fields", fields)


@pytest.fixture
def jira(monkeypatch: pytest.MonkeyPatch) -> FakeJira:
    fake = FakeJira()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def harness(tmp_path: Path) -> ConsoleHarness:
    return make_harness(tmp_path)


def _connected(harness: ConsoleHarness) -> None:
    harness.config_store.update(
        {
            "HENCHMEN_JIRA_BASE_URL": "https://acme.atlassian.net",
            "HENCHMEN_JIRA_EMAIL": "pm@acme.com",
            "HENCHMEN_JIRA_API_TOKEN": TOKEN,
        },
        section="Jira",
    )


def test_intake_label_setting_defaults_to_henchmen() -> None:
    assert Settings(**{"_env_file": None}).jira_intake_label == "henchmen"


def test_requires_a_session(tmp_path: Path) -> None:
    assert make_harness(tmp_path, signed_in=False).get(BASE).status_code == 401


def test_connect_saves_credentials_and_offers_projects_and_custom_fields(
    harness: ConsoleHarness, jira: FakeJira
) -> None:
    response = harness.post(f"{BASE}/credentials", CREDENTIALS)
    body = response.json()
    assert body == {
        "ok": True,
        "step": "jira",
        "details": {
            "account": "authenticated as Pat Manager",
            "projects": [{"key": "API", "name": "API"}, {"key": "WEB", "name": "Web app"}],
            "fields": [
                {"id": "customfield_10043", "name": "Branch"},
                {"id": "customfield_10042", "name": "Repository"},
            ],
        },
    }
    assert TOKEN not in response.text
    assert jira.calls == [("check", "https://acme.atlassian.net", "pm@acme.com", TOKEN)]
    assert harness.config_store.get("HENCHMEN_JIRA_BASE_URL") == "https://acme.atlassian.net"
    assert harness.config_store.get("HENCHMEN_JIRA_API_TOKEN") == TOKEN
    assert SetupStep.JIRA not in harness.setup_store.load().completed_steps


def test_rejected_credentials_save_nothing(harness: ConsoleHarness, jira: FakeJira) -> None:
    jira.ok = False
    body = harness.post(f"{BASE}/credentials", CREDENTIALS).json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "api_token"
    assert "api-tokens" in body["problems"][0]["action"]
    assert not harness.config_store.config_file.exists()


def test_missing_token_asks_for_one(harness: ConsoleHarness, jira: FakeJira) -> None:
    body = harness.post(f"{BASE}/credentials", {**CREDENTIALS, "api_token": ""}).json()
    assert body["problems"][0]["field"] == "api_token"
    assert jira.calls == []


@pytest.mark.parametrize("bad", [{"base_url": "acme.atlassian.net"}, {"email": "not-an-email"}])
def test_malformed_credentials_are_422(harness: ConsoleHarness, jira: FakeJira, bad: dict[str, str]) -> None:
    assert harness.post(f"{BASE}/credentials", {**CREDENTIALS, **bad}).status_code == 422


def test_options_need_saved_credentials(harness: ConsoleHarness, jira: FakeJira) -> None:
    assert harness.get(f"{BASE}/options").json()["ok"] is False
    _connected(harness)
    assert harness.get(f"{BASE}/options").json()["details"]["projects"][1]["key"] == "WEB"


def test_save_choices_completes_the_step(harness: ConsoleHarness, jira: FakeJira) -> None:
    _connected(harness)
    body = harness.post(BASE, CHOICES).json()
    assert body["ok"] is True
    assert body["details"]["api_token"] == "configured"
    config = harness.config_store
    assert config.get("HENCHMEN_JIRA_PROJECT_KEY") == "WEB"
    assert config.get("HENCHMEN_JIRA_REPO_FIELD") == "customfield_10042"
    assert config.get("HENCHMEN_JIRA_BRANCH_FIELD") == "customfield_10043"
    assert config.get("HENCHMEN_JIRA_INTAKE_LABEL") == "henchmen"
    assert SetupStep.JIRA in harness.setup_store.load().completed_steps


def test_fields_are_optional(harness: ConsoleHarness, jira: FakeJira) -> None:
    _connected(harness)
    body = harness.post(BASE, {"project_key": "WEB", "repo_field": "", "branch_field": ""}).json()
    assert body["ok"] is True
    assert harness.config_store.get("HENCHMEN_JIRA_INTAKE_LABEL") == "henchmen"


def test_choices_are_checked_against_the_site(harness: ConsoleHarness, jira: FakeJira) -> None:
    _connected(harness)
    body = harness.post(
        BASE,
        {"project_key": "OPS", "repo_field": "customfield_99999", "branch_field": "customfield_99999"},
    ).json()
    assert body["ok"] is False
    assert [problem["field"] for problem in body["problems"]] == [
        "project_key",
        "repo_field",
        "branch_field",
        "branch_field",
    ]
    assert SetupStep.JIRA not in harness.setup_store.load().completed_steps


def test_labels_cannot_contain_spaces(harness: ConsoleHarness, jira: FakeJira) -> None:
    _connected(harness)
    assert harness.post(BASE, {**CHOICES, "intake_label": "hench men"}).status_code == 422


def test_save_before_connecting(harness: ConsoleHarness, jira: FakeJira) -> None:
    assert harness.post(BASE, CHOICES).json()["ok"] is False


def test_status_masks_the_token(harness: ConsoleHarness, jira: FakeJira) -> None:
    _connected(harness)
    response = harness.get(BASE)
    details = response.json()["details"]
    assert details["api_token"] == "configured"
    assert details["email"] == "pm@acme.com"
    assert details["intake_label"] == "henchmen"
    assert TOKEN not in response.text


def test_reconnecting_a_different_site_reopens_the_step(harness: ConsoleHarness, jira: FakeJira) -> None:
    """Fingerprint by the site/account visible in the credentials themselves (no accountId lookup)."""
    _connected(harness)
    harness.post(BASE, CHOICES)
    assert SetupStep.JIRA in harness.setup_store.load().completed_steps

    response = harness.post(f"{BASE}/credentials", {**CREDENTIALS, "base_url": "https://other.atlassian.net/"})
    assert response.json()["ok"] is True
    assert SetupStep.JIRA not in harness.setup_store.load().completed_steps
    assert harness.config_store.get("HENCHMEN_JIRA_PROJECT_KEY") == ""
    assert harness.config_store.get("HENCHMEN_JIRA_REPO_FIELD") == ""
    assert harness.config_store.get("HENCHMEN_JIRA_BRANCH_FIELD") == ""
    assert harness.config_store.get("HENCHMEN_JIRA_BASE_URL") == "https://other.atlassian.net"


def test_reconnecting_the_same_site_keeps_the_step_complete(harness: ConsoleHarness, jira: FakeJira) -> None:
    _connected(harness)
    harness.post(BASE, CHOICES)
    assert SetupStep.JIRA in harness.setup_store.load().completed_steps

    response = harness.post(f"{BASE}/credentials", CREDENTIALS)
    assert response.json()["ok"] is True
    assert SetupStep.JIRA in harness.setup_store.load().completed_steps
    assert harness.config_store.get("HENCHMEN_JIRA_PROJECT_KEY") == "WEB"


def test_save_rechecks_credentials_under_lock(
    harness: ConsoleHarness, jira: FakeJira, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credentials change mid-flight (a second browser tab) must not attribute the old choice to the new site."""
    _connected(harness)
    original_check = checks.check_jira

    def check_and_mutate(
        base_url: str, email: str, api_token: str, *, timeout: float = checks.DEFAULT_TIMEOUT
    ) -> CheckResult:
        harness.config_store.update({"HENCHMEN_JIRA_API_TOKEN": "ATATT3x-a-different-token"}, section="Jira")
        return original_check(base_url, email, api_token, timeout=timeout)

    monkeypatch.setattr(checks, "check_jira", check_and_mutate)
    body = harness.post(BASE, CHOICES).json()
    assert body["ok"] is False
    assert SetupStep.JIRA not in harness.setup_store.load().completed_steps
