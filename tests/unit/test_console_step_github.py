"""Tests for the Console's GitHub step: manifest flow, installation and repository choice."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from henchmen.console import deps
from henchmen.console.github_app import INSTALL_PURPOSE, MANIFEST_PURPOSE
from henchmen.console.state import SetupStep
from henchmen.console.steps.github import PUBLIC_ROUTE_PATHS
from tests.unit.console_harness import ConsoleHarness, make_harness
from tests.unit.github_fakes import FakeGitHub, app_key_pair

BASE = "/console/api/steps/github"
SLUG = "henchmen-chris-ab12"


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Settings (for the GitHub endpoints) read only this test's config file."""
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


def _harness(tmp_path: Path, github: FakeGitHub, *, signed_in: bool = True) -> ConsoleHarness:
    return make_harness(tmp_path, handler=github, signed_in=signed_in)


def _issue_manifest_state(harness: ConsoleHarness) -> str:
    return harness.app.state.callback_states.issue(MANIFEST_PURPOSE, {"account_type": "personal", "organization": ""})


def _add_conversion(github: FakeGitHub, code: str = "code123") -> None:
    github.conversions[code] = FakeGitHub.conversion(github.app_id, SLUG, app_key_pair()[0].decode())


def _secret_texts() -> list[str]:
    pem = app_key_pair()[0].decode()
    return ["PRIVATE KEY", "whsec-fake", pem.splitlines()[1]]


def test_callbacks_are_public_and_nothing_else_in_the_step_is(tmp_path: Path, github: FakeGitHub) -> None:
    assert frozenset({f"{BASE}/manifest-callback", f"{BASE}/installed"}) == PUBLIC_ROUTE_PATHS
    anonymous = _harness(tmp_path, github, signed_in=False)
    assert anonymous.post(f"{BASE}/manifest", {"account_type": "personal"}).status_code == 401
    assert anonymous.get(BASE).status_code == 401
    assert anonymous.get(f"{BASE}/manifest-callback", code="c", state="s").status_code == 303
    # Exact-path matching: nothing below or beside a public path is public.
    assert anonymous.get(f"{BASE}/manifest-callback/x").status_code == 401
    assert anonymous.get(f"{BASE}/manifest-callbackx").status_code == 401


def test_manifest_for_a_personal_account(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    body = harness.post(f"{BASE}/manifest", {"account_type": "personal", "machine_name": "Chris MacBook"}).json()
    assert body["ok"] is True
    details = body["details"]
    assert details["form_action"] == f"https://github.com/settings/apps/new?state={details['state']}"
    manifest = json.loads(details["manifest_json"])
    assert manifest == details["manifest"]
    assert manifest["name"].startswith("Henchmen (Chris-MacBook-")
    assert manifest["redirect_url"] == "http://127.0.0.1:8000/console/api/steps/github/manifest-callback"
    assert manifest["setup_url"] == "http://127.0.0.1:8000/console/api/steps/github/installed"
    assert manifest["hook_attributes"]["active"] is False
    assert "admin_request_message" not in details
    assert harness.outbound == []
    assert harness.app.state.callback_states.consume(MANIFEST_PURPOSE, details["state"]) == {
        "account_type": "personal",
        "organization": "",
    }


def test_manifest_for_an_organisation(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    missing = harness.post(f"{BASE}/manifest", {"account_type": "organization"}).json()
    assert missing["ok"] is False
    assert missing["problems"][0]["field"] == "organization"

    details = harness.post(f"{BASE}/manifest", {"account_type": "organization", "organization": "acme"}).json()[
        "details"
    ]
    assert details["form_action"].startswith("https://github.com/organizations/acme/settings/apps/new?state=")
    assert "acme" in details["admin_request_message"]
    assert (
        harness.post(f"{BASE}/manifest", {"account_type": "organization", "organization": "-bad-"}).status_code == 422
    )


def test_manifest_reports_a_state_that_cannot_be_saved(
    tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, github)

    def fail(purpose: str, data: object) -> str:
        raise OSError("read-only file system")

    monkeypatch.setattr(harness.app.state.callback_states, "issue", fail)
    body = harness.post(f"{BASE}/manifest", {"account_type": "personal"}).json()
    assert body["ok"] is False
    assert "writable" in body["problems"][0]["action"]


def test_manifest_callback_stores_the_app_and_sends_the_browser_to_install(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    harness.config_store.update({"HENCHMEN_GITHUB_APP_INSTALLATION_ID": "1"}, section="GitHub")
    _add_conversion(github)

    response = harness.get(f"{BASE}/manifest-callback", code="code123", state=_issue_manifest_state(harness))

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"https://github.com/apps/{SLUG}/installations/new?state=")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    install_state = parse_qs(urlsplit(location).query)["state"][0]
    assert harness.app.state.callback_states.consume(INSTALL_PURPOSE, install_state) == {"slug": SLUG}

    config = harness.config_store
    assert config.get("HENCHMEN_GITHUB_APP_ID") == github.app_id
    assert config.get("HENCHMEN_GITHUB_WEBHOOK_SECRET") == "whsec-fake"
    assert config.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == ""
    assert "HENCHMEN_GITHUB_APP_INSTALLATION_ID" not in config.config_file.read_text(encoding="utf-8")
    key_path = Path(config.get("HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH"))
    assert key_path == tmp_path / "secrets" / "github-app.pem"
    assert key_path.read_bytes() == app_key_pair()[0]
    assert "PRIVATE KEY" not in config.config_file.read_text(encoding="utf-8")
    if sys.platform != "win32":
        assert oct(os.stat(key_path).st_mode & 0o777) == "0o600"
    assert harness.setup_store.load().server_choices["github_app_slug"] == SLUG
    assert harness.setup_store.load().server_choices["github_account"] == "chris"
    assert "github_app_slug" not in harness.setup_store.load().choices
    for secret in _secret_texts():
        assert secret not in location
        assert secret not in response.text
    assert SetupStep.GITHUB not in harness.setup_store.load().completed_steps
    # The conversion was the only call to GitHub, unauthenticated, to the API URL.
    assert [str(request.url) for request in github.requests] == [
        "https://api.github.com/app-manifests/code123/conversions"
    ]
    assert "authorization" not in github.requests[0].headers


def test_manifest_callback_writes_one_backup_with_the_whole_previous_file(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    harness.config_store.update({"HENCHMEN_GITHUB_APP_INSTALLATION_ID": "1"}, section="GitHub")
    previous = harness.config_store.config_file.read_bytes()
    _add_conversion(github)
    harness.get(f"{BASE}/manifest-callback", code="code123", state=_issue_manifest_state(harness))
    backup = harness.config_store.config_file.with_name("henchmen.env.bak")
    assert backup.read_bytes() == previous


def test_manifest_callback_state_is_single_use(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = _issue_manifest_state(harness)
    assert harness.get(f"{BASE}/manifest-callback", code="code123", state=state).status_code == 303
    requests_before = len(github.requests)
    config_before = harness.config_store.config_file.read_bytes()

    replay = harness.get(f"{BASE}/manifest-callback", code="code123", state=state)

    assert replay.status_code == 303
    assert replay.headers["location"] == "/?step=github&github_error=expired"
    assert len(github.requests) == requests_before
    assert harness.config_store.config_file.read_bytes() == config_before


@pytest.mark.asyncio
async def test_concurrent_replays_of_one_state_create_the_app_once(tmp_path: Path, github: FakeGitHub) -> None:
    import asyncio

    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = _issue_manifest_state(harness)
    transport = httpx.ASGITransport(app=harness.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        responses = await asyncio.gather(
            *(client.get(f"{BASE}/manifest-callback", params={"code": "code123", "state": state}) for _ in range(6))
        )
    locations = [response.headers["location"] for response in responses]
    installs = [location for location in locations if location.startswith("https://github.com/apps/")]
    assert len(installs) == 1
    assert locations.count("/?step=github&github_error=expired") == 5
    assert len(github.requests) == 1


def test_an_install_state_cannot_complete_the_manifest_callback(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = harness.app.state.callback_states.issue(INSTALL_PURPOSE, {"slug": SLUG})
    response = harness.get(f"{BASE}/manifest-callback", code="code123", state=state)
    assert response.headers["location"] == "/?step=github&github_error=expired"
    assert github.requests == []
    assert not harness.config_store.config_file.exists()


def test_an_expired_state_never_calls_github(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    clock = [1_000_000.0]
    from henchmen.console.callback_state import CallbackStateStore

    harness.app.state.callback_states = CallbackStateStore(
        harness.app.state.callback_states.path, ttl_seconds=60, clock=lambda: clock[0]
    )
    state = _issue_manifest_state(harness)
    clock[0] += 61
    response = harness.get(f"{BASE}/manifest-callback", code="code123", state=state)
    assert response.headers["location"] == "/?step=github&github_error=expired"
    assert github.requests == []
    assert not harness.config_store.config_file.exists()


@pytest.mark.parametrize(
    "params",
    [
        {"code": "code123", "state": "forged"},
        {"code": "code123"},
        {},
        {"code": "code123", "state": "x" * 5000},
        {"code": "code123", "state": "forged", "redirect": "https://evil.example", "next": "//evil.example"},
    ],
)
def test_manifest_callback_without_a_valid_state_never_calls_github(
    tmp_path: Path, github: FakeGitHub, params: dict[str, str]
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    response = harness.get(f"{BASE}/manifest-callback", **params)
    assert response.status_code == 303
    assert response.headers["location"] == "/?step=github&github_error=expired"
    assert response.text == ""
    assert github.requests == []
    assert not harness.config_store.config_file.exists()
    assert not (tmp_path / "secrets" / "github-app.pem").exists()


def test_failed_conversion_writes_nothing(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    response = harness.get(f"{BASE}/manifest-callback", code="expired-code", state=_issue_manifest_state(harness))
    assert response.headers["location"] == "/?step=github&github_error=conversion"
    assert not harness.config_store.config_file.exists()
    assert not (tmp_path / "secrets" / "github-app.pem").exists()
    assert harness.setup_store.load().server_choices == {}


def test_a_malformed_code_is_never_sent_to_github(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    response = harness.get(f"{BASE}/manifest-callback", code="../../app", state=_issue_manifest_state(harness))
    assert response.headers["location"] == "/?step=github&github_error=conversion"
    assert github.requests == []


def test_conversion_failure_with_a_private_key_in_the_body_is_never_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pem = app_key_pair()[0].decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": f"conversion exploded: {pem}", "pem": pem})

    harness = make_harness(tmp_path, handler=handler, signed_in=False)
    with caplog.at_level(logging.DEBUG):
        response = harness.get(f"{BASE}/manifest-callback", code="code123", state=_issue_manifest_state(harness))

    assert response.headers["location"] == "/?step=github&github_error=conversion"
    assert "PRIVATE KEY" not in response.text
    assert "PRIVATE KEY" not in response.headers["location"]
    assert caplog.records, "the failed conversion should be logged"
    for record in caplog.records:
        assert "PRIVATE KEY" not in record.getMessage()
        assert pem.splitlines()[1] not in record.getMessage()
    assert "PRIVATE KEY" not in caplog.text
    assert not harness.config_store.config_file.exists()


def test_a_successful_callback_logs_no_secret(
    tmp_path: Path, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = _issue_manifest_state(harness)
    with caplog.at_level(logging.DEBUG):
        response = harness.get(f"{BASE}/manifest-callback", code="code123", state=state)
    assert response.status_code == 303
    for secret in _secret_texts():
        assert secret not in caplog.text
    # The test client's own httpx request log shows the URL it sent; the server's logs never hold the state.
    server_records = [record.getMessage() for record in caplog.records if record.name.startswith("henchmen")]
    assert server_records
    assert all(state not in message for message in server_records)


def test_storage_failure_redirects_without_marking_anything(
    tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = _issue_manifest_state(harness)

    def fail(name: str, data: bytes) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(harness.config_store, "write_secret_file", fail)
    response = harness.get(f"{BASE}/manifest-callback", code="code123", state=state)
    assert response.status_code == 303
    assert response.headers["location"] == "/?step=github&github_error=storage"
    assert not harness.config_store.config_file.exists()
    assert harness.setup_store.load().server_choices == {}


def test_conversion_goes_to_the_configured_api_url(tmp_path: Path, github: FakeGitHub) -> None:
    (tmp_path / "henchmen.env").write_text(
        "HENCHMEN_PROVIDER=local\n"
        "HENCHMEN_GITHUB_API_URL=https://api.ghe.example.test/\n"
        "HENCHMEN_GITHUB_WEB_URL=https://ghe.example.test\n",
        encoding="utf-8",
    )
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    response = harness.get(f"{BASE}/manifest-callback", code="code123", state=_issue_manifest_state(harness))
    assert [str(request.url) for request in github.requests] == [
        "https://api.ghe.example.test/app-manifests/code123/conversions"
    ]
    assert response.headers["location"].startswith(f"https://ghe.example.test/apps/{SLUG}/installations/new?state=")


def test_the_default_http_client_ignores_the_environment_and_has_a_timeout() -> None:
    client = deps.default_http_client()
    try:
        assert client.trust_env is False
        assert client.timeout.read == deps.DEFAULT_HTTP_TIMEOUT
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_status_before_anything_is_created(tmp_path: Path, github: FakeGitHub) -> None:
    details = _harness(tmp_path, github).get(BASE).json()["details"]
    assert details == {
        "app_created": False,
        "app_slug": "",
        "account": "",
        "installed": False,
        "default_repo": "",
        "private_key": "",
        "completed": False,
    }


def test_status_after_the_app_is_created(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _add_conversion(github)
    harness.get(f"{BASE}/manifest-callback", code="code123", state=_issue_manifest_state(harness))
    response = harness.get(BASE)
    details = response.json()["details"]
    assert details["app_created"] is True
    assert details["app_slug"] == SLUG
    assert details["installed"] is False
    assert details["private_key"] == "configured"
    for secret in _secret_texts():
        assert secret not in response.text
