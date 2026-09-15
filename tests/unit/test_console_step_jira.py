"""Tests for the Console's Jira step."""

from __future__ import annotations

from pathlib import Path

import pytest

from henchmen.cli import checks
from henchmen.cli.checks import CheckResult, CheckStatus, JiraField, JiraIdentity, JiraProject, JiraProjectListing
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
DEFAULT_IDENTITY = JiraIdentity(account_id="acc-1", display_name="Pat Manager")


class FakeJira:
    def __init__(self) -> None:
        self.ok = True
        self.calls: list[tuple[str, ...]] = []
        self.identity: JiraIdentity | None = DEFAULT_IDENTITY
        self.projects: list[JiraProject] = [JiraProject(key="API", name="API"), JiraProject(key="WEB", name="Web app")]
        self.truncated = False
        self.fields: list[JiraField] = [
            JiraField(id="customfield_10043", name="Branch", custom=True),
            JiraField(id="customfield_10042", name="Repository", custom=True),
            JiraField(id="summary", name="Summary", custom=False),
        ]
        self.unlisted_project: JiraProject | None = None
        self.listing_unreachable = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def check(base_url: str, email: str, api_token: str, *, timeout: float = checks.DEFAULT_TIMEOUT) -> CheckResult:
            self.calls.append(("check", base_url, email, api_token))
            if self.ok:
                return CheckResult("Jira", CheckStatus.OK, "authenticated as Pat Manager")
            return CheckResult(
                "Jira",
                CheckStatus.FAIL,
                f"{base_url}: the email or API token is wrong",
                hint="Create a new API token at id.atlassian.com/manage-profile/security/api-tokens",
            )

        def identity(
            base_url: str, email: str, api_token: str, *, timeout: float = checks.DEFAULT_TIMEOUT
        ) -> JiraIdentity | None:
            self.calls.append(("identity", base_url, email, api_token))
            return self.identity

        def projects(
            base_url: str, email: str, api_token: str, *, timeout: float = checks.DEFAULT_TIMEOUT
        ) -> JiraProjectListing:
            if self.listing_unreachable:
                raise checks.JiraUnreachableError("simulated 500")
            return JiraProjectListing(projects=list(self.projects), truncated=self.truncated)

        def fields(
            base_url: str, email: str, api_token: str, *, timeout: float = checks.DEFAULT_TIMEOUT
        ) -> list[JiraField]:
            if self.listing_unreachable:
                raise checks.JiraUnreachableError("simulated 500")
            return list(self.fields)

        def get_project(
            base_url: str, email: str, api_token: str, project_key: str, *, timeout: float = checks.DEFAULT_TIMEOUT
        ) -> JiraProject | None:
            self.calls.append(("get_project", base_url, email, project_key))
            if self.unlisted_project is not None and self.unlisted_project.key == project_key:
                return self.unlisted_project
            return None

        monkeypatch.setattr(checks, "check_jira", check)
        monkeypatch.setattr(checks, "jira_identity", identity)
        monkeypatch.setattr(checks, "list_jira_projects", projects)
        monkeypatch.setattr(checks, "list_jira_fields", fields)
        monkeypatch.setattr(checks, "get_jira_project", get_project)


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


def _connect_and_complete(harness: ConsoleHarness) -> None:
    """Go through the real ``/credentials`` route (not the ``_connected`` bypass) so a genuine
    account fingerprint is recorded -- required by any test that exercises the reopen-on-change
    logic, which compares against the *previously recorded* fingerprint.
    """
    assert harness.post(f"{BASE}/credentials", CREDENTIALS).json()["ok"] is True
    assert harness.post(BASE, CHOICES).json()["ok"] is True


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
            "truncated": False,
        },
    }
    assert TOKEN not in response.text
    assert jira.calls == [
        ("check", "https://acme.atlassian.net", "pm@acme.com", TOKEN),
        ("identity", "https://acme.atlassian.net", "pm@acme.com", TOKEN),
    ]
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
    assert not any(name == "identity" for name, *_ in jira.calls)


def test_missing_token_asks_for_one(harness: ConsoleHarness, jira: FakeJira) -> None:
    body = harness.post(f"{BASE}/credentials", {**CREDENTIALS, "api_token": ""}).json()
    assert body["problems"][0]["field"] == "api_token"
    assert jira.calls == []


@pytest.mark.parametrize(
    "bad",
    [
        {"base_url": "acme.atlassian.net"},
        {"email": "not-an-email"},
        {"base_url": "https://acme.atlassian.net?x=1"},
        {"base_url": "https://acme.atlassian.net#frag"},
        {"base_url": "http://acme.atlassian.net"},
        {"base_url": "https://user:pass@acme.atlassian.net"},
    ],
)
def test_malformed_credentials_are_422(harness: ConsoleHarness, jira: FakeJira, bad: dict[str, str]) -> None:
    assert harness.post(f"{BASE}/credentials", {**CREDENTIALS, **bad}).status_code == 422


def test_loopback_http_base_url_is_accepted(harness: ConsoleHarness, jira: FakeJira) -> None:
    """F6: loopback plain http is still fine (local dev Jira), unlike a real hostname."""
    body = harness.post(f"{BASE}/credentials", {**CREDENTIALS, "base_url": "http://localhost:2990"}).json()
    assert body["ok"] is True


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
    _connect_and_complete(harness)
    assert SetupStep.JIRA in harness.setup_store.load().completed_steps

    response = harness.post(f"{BASE}/credentials", {**CREDENTIALS, "base_url": "https://other.atlassian.net/"})
    assert response.json()["ok"] is True
    assert SetupStep.JIRA not in harness.setup_store.load().completed_steps
    assert harness.config_store.get("HENCHMEN_JIRA_PROJECT_KEY") == ""
    assert harness.config_store.get("HENCHMEN_JIRA_REPO_FIELD") == ""
    assert harness.config_store.get("HENCHMEN_JIRA_BRANCH_FIELD") == ""
    assert harness.config_store.get("HENCHMEN_JIRA_BASE_URL") == "https://other.atlassian.net"


def test_reconnecting_the_same_site_keeps_the_step_complete(harness: ConsoleHarness, jira: FakeJira) -> None:
    _connect_and_complete(harness)
    assert SetupStep.JIRA in harness.setup_store.load().completed_steps

    response = harness.post(f"{BASE}/credentials", CREDENTIALS)
    assert response.json()["ok"] is True
    assert SetupStep.JIRA in harness.setup_store.load().completed_steps
    assert harness.config_store.get("HENCHMEN_JIRA_PROJECT_KEY") == "WEB"


def test_new_account_id_at_the_same_site_reopens_the_step(harness: ConsoleHarness, jira: FakeJira) -> None:
    """Identity ruling: fingerprint on the Atlassian account id, not just the (base_url, email) text."""
    _connect_and_complete(harness)
    assert SetupStep.JIRA in harness.setup_store.load().completed_steps

    jira.identity = JiraIdentity(account_id="acc-2", display_name="Pat Manager")
    response = harness.post(f"{BASE}/credentials", CREDENTIALS)
    assert response.json()["ok"] is True
    assert SetupStep.JIRA not in harness.setup_store.load().completed_steps
    assert harness.config_store.get("HENCHMEN_JIRA_PROJECT_KEY") == ""


def test_missing_identity_fails_closed_and_reopens_the_step(harness: ConsoleHarness, jira: FakeJira) -> None:
    """When the account id can't be confirmed, treat it as a change rather than trust it."""
    _connect_and_complete(harness)
    assert SetupStep.JIRA in harness.setup_store.load().completed_steps

    jira.identity = None
    body = harness.post(f"{BASE}/credentials", CREDENTIALS).json()
    assert body["ok"] is True
    assert SetupStep.JIRA not in harness.setup_store.load().completed_steps
    assert harness.config_store.get("HENCHMEN_JIRA_PROJECT_KEY") == ""


def test_intake_label_survives_a_reopening_reconnect(harness: ConsoleHarness, jira: FakeJira) -> None:
    """F8: the intake label is site-agnostic, so a reopening reconnect must not clear it."""
    _connect_and_complete(harness)

    jira.identity = JiraIdentity(account_id="acc-2", display_name="Pat Manager")
    harness.post(f"{BASE}/credentials", CREDENTIALS)
    assert harness.config_store.get("HENCHMEN_JIRA_PROJECT_KEY") == ""
    assert harness.config_store.get("HENCHMEN_JIRA_INTAKE_LABEL") == "henchmen"


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


def test_connect_surfaces_a_listing_failure_as_a_problem(harness: ConsoleHarness, jira: FakeJira) -> None:
    """F4: a listing failure after credentials check out must not read as ok:true with an empty list."""
    jira.listing_unreachable = True
    body = harness.post(f"{BASE}/credentials", CREDENTIALS).json()
    assert body["ok"] is False
    assert "could not list Jira projects" in body["problems"][0]["message"]
    assert not harness.config_store.config_file.exists()


def test_options_surfaces_a_listing_failure_as_a_problem(harness: ConsoleHarness, jira: FakeJira) -> None:
    _connected(harness)
    jira.listing_unreachable = True
    body = harness.get(f"{BASE}/options").json()
    assert body["ok"] is False
    assert "could not list Jira projects" in body["problems"][0]["message"]


def test_save_surfaces_a_listing_failure_as_a_problem(harness: ConsoleHarness, jira: FakeJira) -> None:
    _connected(harness)
    jira.listing_unreachable = True
    body = harness.post(BASE, CHOICES).json()
    assert body["ok"] is False
    assert "could not list Jira projects" in body["problems"][0]["message"]
    assert SetupStep.JIRA not in harness.setup_store.load().completed_steps


def test_truncated_flag_is_reported_by_connect_and_options(harness: ConsoleHarness, jira: FakeJira) -> None:
    """F5: implement what the docstring claims -- truncation is surfaced, not silently dropped."""
    jira.truncated = True
    body = harness.post(f"{BASE}/credentials", CREDENTIALS).json()
    assert body["details"]["truncated"] is True

    body2 = harness.get(f"{BASE}/options").json()
    assert body2["details"]["truncated"] is True


def test_untruncated_listing_reports_truncated_false(harness: ConsoleHarness, jira: FakeJira) -> None:
    body = harness.post(f"{BASE}/credentials", CREDENTIALS).json()
    assert body["details"]["truncated"] is False


def test_save_confirms_a_project_beyond_a_truncated_listing_by_key(harness: ConsoleHarness, jira: FakeJira) -> None:
    """F5: a bounded direct lookup, mirroring the Slack step's get_slack_channel pattern."""
    _connected(harness)
    jira.truncated = True
    jira.unlisted_project = JiraProject(key="OPS", name="Operations")
    body = harness.post(BASE, {**CHOICES, "project_key": "OPS"}).json()
    assert body["ok"] is True
    assert harness.config_store.get("HENCHMEN_JIRA_PROJECT_KEY") == "OPS"
    assert ("get_project", "https://acme.atlassian.net", "pm@acme.com", "OPS") in jira.calls


def test_save_fails_closed_for_a_project_beyond_a_truncated_listing_that_does_not_exist(
    harness: ConsoleHarness, jira: FakeJira
) -> None:
    _connected(harness)
    jira.truncated = True
    body = harness.post(BASE, {**CHOICES, "project_key": "OPS"}).json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "project_key"


def test_direct_lookup_is_not_attempted_when_the_listing_was_not_truncated(
    harness: ConsoleHarness, jira: FakeJira
) -> None:
    """A missing project with an untruncated (complete) listing is a definite "no", not a lookup candidate."""
    _connected(harness)
    body = harness.post(BASE, {**CHOICES, "project_key": "OPS"}).json()
    assert body["ok"] is False
    assert not any(name == "get_project" for name, *_ in jira.calls)


def test_distinct_credential_errors_reach_the_step(harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """F3: check_jira's distinct messages must not be collapsed by the step."""

    def check_403(base_url: str, email: str, api_token: str, *, timeout: float = checks.DEFAULT_TIMEOUT) -> CheckResult:
        return CheckResult(
            "Jira",
            CheckStatus.FAIL,
            f"{base_url}: this account is signed in but has no access to this Jira site",
            hint="Ask a Jira admin to grant this account access to the site",
        )

    monkeypatch.setattr(checks, "check_jira", check_403)
    body = harness.post(f"{BASE}/credentials", CREDENTIALS).json()
    assert body["ok"] is False
    assert "no access to this Jira site" in body["problems"][0]["message"]
    assert "admin" in body["problems"][0]["action"]
