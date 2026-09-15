"""Tests for the Console's GitHub step: manifest flow, installation and repository choice."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from henchmen.console import deps
from henchmen.console.callback_state import CallbackStateStore
from henchmen.console.github_app import INSTALL_PURPOSE, MANIFEST_PURPOSE
from henchmen.console.state import SetupStep
from henchmen.console.steps.github import PUBLIC_ROUTE_PATHS
from tests.unit.console_harness import ConsoleHarness, make_harness
from tests.unit.github_fakes import FakeGitHub, app_key_pair

BASE = "/console/api/steps/github"
SLUG = "henchmen-chris-ab12"
API_URL = "https://api.github.com"
WEB_URL = "https://github.com"
CALLBACK = f"{BASE}/manifest-callback"


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Anything reading the data directory reads only this test's files."""
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


def _harness(tmp_path: Path, github: FakeGitHub, *, signed_in: bool = True) -> ConsoleHarness:
    return make_harness(tmp_path, handler=github, signed_in=signed_in)


def _issue_manifest_state(harness: ConsoleHarness, *, api_url: str = API_URL, web_url: str = WEB_URL) -> str:
    return harness.app.state.callback_states.issue(
        MANIFEST_PURPOSE,
        {"account_type": "personal", "organization": "", "api_url": api_url, "web_url": web_url},
    )


def _add_conversion(github: FakeGitHub, code: str = "code123", **overrides: object) -> None:
    body = FakeGitHub.conversion(github.app_id, SLUG, app_key_pair()[0].decode())
    body.update(overrides)
    github.conversions[code] = body


def _key_file(tmp_path: Path, app_id: str) -> Path:
    return tmp_path / "secrets" / f"github-app-{app_id}.pem"


def _secret_texts() -> list[str]:
    pem = app_key_pair()[0].decode()
    return ["PRIVATE KEY", "whsec-fake", pem.splitlines()[1]]


def _write_config(tmp_path: Path, text: str) -> None:
    (tmp_path / "henchmen.env").write_text(text, encoding="utf-8")


# -- routing and the manifest ---------------------------------------------------


def test_callbacks_are_public_and_nothing_else_in_the_step_is(tmp_path: Path, github: FakeGitHub) -> None:
    assert frozenset({f"{BASE}/manifest-callback", f"{BASE}/installed"}) == PUBLIC_ROUTE_PATHS
    anonymous = _harness(tmp_path, github, signed_in=False)
    assert anonymous.post(f"{BASE}/manifest", {"account_type": "personal"}).status_code == 401
    assert anonymous.get(BASE).status_code == 401
    assert anonymous.get(CALLBACK, code="c", state="s").status_code == 303
    # Exact-path matching: nothing below or beside a public path is public.
    assert anonymous.get(f"{CALLBACK}/x").status_code == 401
    assert anonymous.get(f"{CALLBACK}x").status_code == 401


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
        "api_url": API_URL,
        "web_url": WEB_URL,
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
    refused = harness.post(f"{BASE}/manifest", {"account_type": "organization", "organization": "-bad-"})
    assert refused.status_code == 422


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


# -- endpoint resolution --------------------------------------------------------


def test_manifest_uses_the_configured_fake_even_when_unrelated_settings_are_invalid(
    tmp_path: Path, github: FakeGitHub
) -> None:
    _write_config(
        tmp_path,
        "HENCHMEN_PROVIDER=gcp\n"  # no project id: the full Settings cannot be built
        "HENCHMEN_LAIR_DEFAULT_TIMEOUT=not-a-number\n"
        "HENCHMEN_GITHUB_API_URL=http://fakes:9000/api/\n"
        "HENCHMEN_GITHUB_WEB_URL=http://fakes:9000/web\n",
    )
    harness = _harness(tmp_path, github)
    details = harness.post(f"{BASE}/manifest", {"account_type": "personal"}).json()["details"]
    assert details["form_action"].startswith("http://fakes:9000/web/settings/apps/new?state=")
    data = harness.app.state.callback_states.consume(MANIFEST_PURPOSE, details["state"])
    assert data is not None
    assert (data["api_url"], data["web_url"]) == ("http://fakes:9000/api", "http://fakes:9000/web")


def test_an_edit_to_the_configuration_is_picked_up_on_the_next_call(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    first = harness.post(f"{BASE}/manifest", {"account_type": "personal"}).json()["details"]
    assert first["form_action"].startswith("https://github.com/")
    _write_config(tmp_path, "HENCHMEN_GITHUB_WEB_URL=https://ghe.example.test\n")
    second = harness.post(f"{BASE}/manifest", {"account_type": "personal"}).json()["details"]
    assert second["form_action"].startswith("https://ghe.example.test/settings/apps/new?state=")


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ("HENCHMEN_GITHUB_WEB_URL=http://github.example.com", "github_web_url"),
        ("HENCHMEN_GITHUB_API_URL=https://user:hunter2-pw@api.github.com", "github_api_url"),
    ],
)
def test_an_invalid_github_url_fails_the_manifest_closed(
    tmp_path: Path, github: FakeGitHub, line: str, field: str
) -> None:
    _write_config(tmp_path, line + "\n")
    harness = _harness(tmp_path, github)
    response = harness.post(f"{BASE}/manifest", {"account_type": "personal"})
    body = response.json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == field
    assert "hunter2" not in response.text
    assert "github.example.com" not in response.text
    assert not harness.app.state.callback_states.path.exists()


def test_the_environment_outranks_the_file_for_github_urls(
    tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, "HENCHMEN_GITHUB_WEB_URL=https://file.example.test\n")
    monkeypatch.setenv("HENCHMEN_GITHUB_WEB_URL", "https://env.example.test")
    details = _harness(tmp_path, github).post(f"{BASE}/manifest", {"account_type": "personal"}).json()["details"]
    assert details["form_action"].startswith("https://env.example.test/")


def test_the_callback_uses_the_state_bound_urls_even_if_the_configuration_changed(
    tmp_path: Path, github: FakeGitHub
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = _issue_manifest_state(
        harness, api_url="https://api.bound.example.test", web_url="https://bound.example.test"
    )
    _write_config(
        tmp_path,
        "HENCHMEN_GITHUB_API_URL=https://api.changed.example.test\nHENCHMEN_GITHUB_WEB_URL=https://changed.example.test\n",
    )
    response = harness.get(CALLBACK, code="code123", state=state)
    assert [str(request.url) for request in github.requests] == [
        "https://api.bound.example.test/app-manifests/code123/conversions"
    ]
    location = response.headers["location"]
    assert location.startswith(f"https://bound.example.test/apps/{SLUG}/installations/new?state=")
    install_state = parse_qs(urlsplit(location).query)["state"][0]
    assert harness.app.state.callback_states.consume(INSTALL_PURPOSE, install_state) == {
        "slug": SLUG,
        "api_url": "https://api.bound.example.test",
        "web_url": "https://bound.example.test",
    }


@pytest.mark.parametrize(
    "data",
    [
        {"account_type": "personal"},
        {"api_url": "http://api.github.com", "web_url": WEB_URL},
        {"api_url": API_URL, "web_url": "https://user:pw@github.com"},
    ],
)
def test_a_state_without_usable_urls_never_calls_github(
    tmp_path: Path, github: FakeGitHub, data: dict[str, str]
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = harness.app.state.callback_states.issue(MANIFEST_PURPOSE, data)
    response = harness.get(CALLBACK, code="code123", state=state)
    assert response.headers["location"] == "/?step=github&github_error=configuration"
    assert github.requests == []
    assert not harness.config_store.config_file.exists()


# -- the manifest callback ------------------------------------------------------


def test_manifest_callback_stores_the_app_and_sends_the_browser_to_install(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    harness.config_store.update({"HENCHMEN_GITHUB_APP_INSTALLATION_ID": "1"}, section="GitHub")
    _add_conversion(github)

    response = harness.get(CALLBACK, code="code123", state=_issue_manifest_state(harness))

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"https://github.com/apps/{SLUG}/installations/new?state=")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    install_state = parse_qs(urlsplit(location).query)["state"][0]
    assert harness.app.state.callback_states.consume(INSTALL_PURPOSE, install_state) == {
        "slug": SLUG,
        "api_url": API_URL,
        "web_url": WEB_URL,
    }

    config = harness.config_store
    assert config.get("HENCHMEN_GITHUB_APP_ID") == github.app_id
    assert config.get("HENCHMEN_GITHUB_WEBHOOK_SECRET") == "whsec-fake"
    assert config.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == ""
    assert "HENCHMEN_GITHUB_APP_INSTALLATION_ID" not in config.config_file.read_text(encoding="utf-8")
    key_path = Path(config.get("HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH"))
    assert key_path == _key_file(tmp_path, github.app_id)
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
    assert [str(request.url) for request in github.requests] == [f"{API_URL}/app-manifests/code123/conversions"]
    assert "authorization" not in github.requests[0].headers


def test_a_reconnect_never_touches_the_key_the_running_app_uses(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    old_key = harness.config_store.write_secret_file("github-app-1111.pem", b"the old app's key")
    harness.config_store.update(
        {
            "HENCHMEN_GITHUB_APP_ID": "1111",
            "HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH": str(old_key),
            "HENCHMEN_GITHUB_APP_INSTALLATION_ID": "9",
        },
        section="GitHub",
    )
    _add_conversion(github)

    response = harness.get(CALLBACK, code="code123", state=_issue_manifest_state(harness))

    assert response.headers["location"].startswith(f"https://github.com/apps/{SLUG}/")
    assert old_key.read_bytes() == b"the old app's key"
    assert Path(harness.config_store.get("HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH")) == _key_file(tmp_path, github.app_id)
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_ID") == github.app_id


def test_a_failed_configuration_write_keeps_the_old_app_whole_and_removes_the_new_key(
    tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    old_key = harness.config_store.write_secret_file("github-app-1111.pem", b"the old app's key")
    harness.config_store.update(
        {"HENCHMEN_GITHUB_APP_ID": "1111", "HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH": str(old_key)}, section="GitHub"
    )
    config_before = harness.config_store.config_file.read_bytes()
    _add_conversion(github)
    state = _issue_manifest_state(harness)

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(harness.config_store, "update", fail)
    response = harness.get(CALLBACK, code="code123", state=state)

    assert response.headers["location"] == "/?step=github&github_error=storage"
    assert harness.config_store.config_file.read_bytes() == config_before
    assert old_key.read_bytes() == b"the old app's key"
    assert not _key_file(tmp_path, github.app_id).exists()
    assert harness.setup_store.load().server_choices == {}


def test_an_empty_webhook_secret_removes_the_old_one(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    harness.config_store.update({"HENCHMEN_GITHUB_WEBHOOK_SECRET": "old-app-secret"}, section="GitHub")
    _add_conversion(github, webhook_secret=None)
    harness.get(CALLBACK, code="code123", state=_issue_manifest_state(harness))
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_ID") == github.app_id
    assert "HENCHMEN_GITHUB_WEBHOOK_SECRET" not in harness.config_store.config_file.read_text(encoding="utf-8")


def test_manifest_callback_writes_one_backup_with_the_whole_previous_file(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    harness.config_store.update({"HENCHMEN_GITHUB_APP_INSTALLATION_ID": "1"}, section="GitHub")
    previous = harness.config_store.config_file.read_bytes()
    _add_conversion(github)
    harness.get(CALLBACK, code="code123", state=_issue_manifest_state(harness))
    backup = harness.config_store.config_file.with_name("henchmen.env.bak")
    assert backup.read_bytes() == previous


def test_an_install_state_that_cannot_be_saved_writes_nothing(
    tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = _issue_manifest_state(harness)
    store: CallbackStateStore = harness.app.state.callback_states

    def fail(purpose: str, data: object) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(store, "issue", fail)
    response = harness.get(CALLBACK, code="code123", state=state)
    assert response.headers["location"] == "/?step=github&github_error=storage"
    assert not harness.config_store.config_file.exists()
    assert not _key_file(tmp_path, github.app_id).exists()


def test_a_key_file_that_cannot_be_saved_writes_no_configuration(
    tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = _issue_manifest_state(harness)

    def fail(name: str, data: bytes) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(harness.config_store, "write_secret_file", fail)
    response = harness.get(CALLBACK, code="code123", state=state)
    assert response.status_code == 303
    assert response.headers["location"] == "/?step=github&github_error=storage"
    assert not harness.config_store.config_file.exists()
    assert harness.setup_store.load().server_choices == {}


def test_a_failed_setup_state_write_still_sends_the_browser_to_install(
    tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = _issue_manifest_state(harness)

    def fail(values: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(harness.setup_store, "set_server_choices", fail)
    with caplog.at_level(logging.WARNING):
        response = harness.get(CALLBACK, code="code123", state=state)
    assert response.headers["location"].startswith(f"https://github.com/apps/{SLUG}/installations/new?state=")
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_ID") == github.app_id
    assert any("Could not record the name" in record.getMessage() for record in caplog.records)


# -- state checks ---------------------------------------------------------------


def test_manifest_callback_state_is_single_use(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = _issue_manifest_state(harness)
    assert harness.get(CALLBACK, code="code123", state=state).status_code == 303
    requests_before = len(github.requests)
    config_before = harness.config_store.config_file.read_bytes()

    replay = harness.get(CALLBACK, code="code123", state=state)

    assert replay.status_code == 303
    assert replay.headers["location"] == "/?step=github&github_error=expired"
    assert len(github.requests) == requests_before
    assert harness.config_store.config_file.read_bytes() == config_before


@pytest.mark.asyncio
async def test_concurrent_replays_of_one_state_create_the_app_once(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = _issue_manifest_state(harness)
    transport = httpx.ASGITransport(app=harness.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        responses = await asyncio.gather(
            *(client.get(CALLBACK, params={"code": "code123", "state": state}) for _ in range(6))
        )
    locations = [response.headers["location"] for response in responses]
    installs = [location for location in locations if location.startswith("https://github.com/apps/")]
    assert len(installs) == 1
    assert locations.count("/?step=github&github_error=expired") == 5
    assert len(github.requests) == 1


def test_an_install_state_cannot_complete_the_manifest_callback(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = harness.app.state.callback_states.issue(
        INSTALL_PURPOSE, {"slug": SLUG, "api_url": API_URL, "web_url": WEB_URL}
    )
    response = harness.get(CALLBACK, code="code123", state=state)
    assert response.headers["location"] == "/?step=github&github_error=expired"
    assert github.requests == []
    assert not harness.config_store.config_file.exists()


def test_an_expired_state_never_calls_github(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    clock = [1_000_000.0]
    harness.app.state.callback_states = CallbackStateStore(
        harness.app.state.callback_states.path, ttl_seconds=60, clock=lambda: clock[0]
    )
    state = _issue_manifest_state(harness)
    clock[0] += 61
    response = harness.get(CALLBACK, code="code123", state=state)
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
    response = harness.get(CALLBACK, **params)
    assert response.status_code == 303
    assert response.headers["location"] == "/?step=github&github_error=expired"
    assert response.text == ""
    assert github.requests == []
    assert not harness.config_store.config_file.exists()
    assert not (tmp_path / "secrets").exists() or not list((tmp_path / "secrets").glob("github-app*.pem"))


def test_failed_conversion_writes_nothing(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    response = harness.get(CALLBACK, code="expired-code", state=_issue_manifest_state(harness))
    assert response.headers["location"] == "/?step=github&github_error=conversion"
    assert not harness.config_store.config_file.exists()
    assert not list((tmp_path / "secrets").glob("github-app*.pem"))
    assert harness.setup_store.load().server_choices == {}


def test_a_malformed_code_is_never_sent_to_github(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    response = harness.get(CALLBACK, code="../../app", state=_issue_manifest_state(harness))
    assert response.headers["location"] == "/?step=github&github_error=conversion"
    assert github.requests == []


# -- logs -----------------------------------------------------------------------


def test_conversion_failure_with_a_private_key_in_the_body_is_never_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pem = app_key_pair()[0].decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": f"conversion exploded: {pem}", "pem": pem})

    harness = make_harness(tmp_path, handler=handler, signed_in=False)
    with caplog.at_level(logging.DEBUG):
        response = harness.get(CALLBACK, code="code123", state=_issue_manifest_state(harness))

    assert response.headers["location"] == "/?step=github&github_error=conversion"
    assert "PRIVATE KEY" not in response.text
    assert "PRIVATE KEY" not in response.headers["location"]
    assert caplog.records, "the failed conversion should be logged"
    for record in caplog.records:
        assert "PRIVATE KEY" not in record.getMessage()
        assert pem.splitlines()[1] not in record.getMessage()
    assert "PRIVATE KEY" not in caplog.text
    assert not harness.config_store.config_file.exists()


def test_a_successful_callback_logs_no_secret_code_or_state(
    tmp_path: Path, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    from henchmen.utils.redaction import install_secret_redaction

    original_factory = logging.getLogRecordFactory()
    install_secret_redaction()
    try:
        harness = _harness(tmp_path, github, signed_in=False)
        _add_conversion(github, code="c0de-4-manifest")
        state = _issue_manifest_state(harness)
        with caplog.at_level(logging.DEBUG):
            response = harness.get(CALLBACK, code="c0de-4-manifest", state=state)
    finally:
        logging.setLogRecordFactory(original_factory)
    assert response.status_code == 303
    install_state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    assert caplog.records
    # Every record, including httpx's own request lines for the test client and the conversion call.
    for secret in [*_secret_texts(), state, install_state, "c0de-4-manifest"]:
        assert secret not in caplog.text


# -- misc -----------------------------------------------------------------------


def test_the_default_http_client_ignores_the_environment_and_has_a_timeout() -> None:
    client = deps.default_http_client()
    try:
        assert client.trust_env is False
        assert client.timeout.read == deps.DEFAULT_HTTP_TIMEOUT
    finally:
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
    harness.get(CALLBACK, code="code123", state=_issue_manifest_state(harness))
    response = harness.get(BASE)
    details = response.json()["details"]
    assert details["app_created"] is True
    assert details["app_slug"] == SLUG
    assert details["installed"] is False
    assert details["private_key"] == "configured"
    for secret in _secret_texts():
        assert secret not in response.text


def test_an_unreadable_config_file_fails_the_manifest_instead_of_a_500(
    tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from henchmen.utils import endpoints

    _write_config(tmp_path, "HENCHMEN_GITHUB_WEB_URL=https://ghe.example.test\n")
    harness = _harness(tmp_path, github)

    def denied(*args: object, **kwargs: object) -> dict[str, str]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(endpoints, "dotenv_values", denied)
    response = harness.post(f"{BASE}/manifest", {"account_type": "personal"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "github_api_url"
    assert not harness.app.state.callback_states.path.exists()


# ---------------------------------------------------------------------------
# Installation and repository choice (Task 6)
# ---------------------------------------------------------------------------

REPO = "acme/webapp"
INSTALLED = f"{BASE}/installed"


def _created_app(harness: ConsoleHarness, github: FakeGitHub, *, installation_id: str = "") -> Path:
    key_path = harness.config_store.write_secret_file(f"github-app-{github.app_id}.pem", app_key_pair()[0])
    values = {"HENCHMEN_GITHUB_APP_ID": github.app_id, "HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH": str(key_path)}
    if installation_id:
        values["HENCHMEN_GITHUB_APP_INSTALLATION_ID"] = installation_id
    harness.config_store.update(values, section="GitHub")
    harness.setup_store.set_server_choices({"github_app_slug": github.app_slug})
    return key_path


def _install_state(
    harness: ConsoleHarness, github: FakeGitHub, *, api_url: str = API_URL, web_url: str = WEB_URL
) -> str:
    return harness.app.state.callback_states.issue(
        INSTALL_PURPOSE, {"slug": github.app_slug, "api_url": api_url, "web_url": web_url}
    )


def _installable(harness: ConsoleHarness, github: FakeGitHub, installation_id: str = "77") -> None:
    _created_app(harness, github)
    github.installations[installation_id] = FakeGitHub.installation(installation_id, "acme")


def _append_config(harness: ConsoleHarness, tmp_path: Path, text: str) -> None:
    _write_config(tmp_path, harness.config_store.config_file.read_text(encoding="utf-8") + text)


def _ready_to_choose(harness: ConsoleHarness, github: FakeGitHub, *, bot_id: int = 1, **installation: object) -> None:
    _created_app(harness, github, installation_id="77")
    github.installations["77"] = FakeGitHub.installation("77", "acme", **installation)
    github.repositories = [FakeGitHub.repository(REPO)]
    github.users[f"{github.app_slug}[bot]"] = {"id": bot_id, "login": f"{github.app_slug}[bot]"}


# -- the public installed callback ------------------------------------------------


def test_installed_callback_stores_the_installation(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)

    response = harness.get(
        INSTALLED, installation_id="77", setup_action="install", state=_install_state(harness, github)
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?step=github&github=installed"
    assert response.headers["cache-control"] == "no-store"
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == "77"
    assert harness.setup_store.load().server_choices["github_account"] == "acme"
    assert SetupStep.GITHUB not in harness.setup_store.load().completed_steps
    # Verified with this App's JWT against the installation endpoint.
    assert [str(request.url) for request in github.requests] == [f"{API_URL}/app/installations/77"]
    assert github.requests[0].headers["authorization"].startswith("Bearer ")


def test_installed_callback_waits_for_admin_approval(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _created_app(harness, github)
    response = harness.get(INSTALLED, setup_action="request", state=_install_state(harness, github))
    assert response.headers["location"] == "/?step=github&github=requested"
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == ""
    assert github.requests == []


def test_an_approval_request_never_saves_a_query_installation_id(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    response = harness.get(
        INSTALLED, installation_id="77", setup_action="request", state=_install_state(harness, github)
    )
    assert response.headers["location"] == "/?step=github&github=requested"
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == ""
    assert github.requests == []


@pytest.mark.parametrize(
    "params",
    [
        {"installation_id": "77", "setup_action": "install", "state": "forged"},
        {"installation_id": "77", "setup_action": "install"},
        {"installation_id": "77", "setup_action": "request"},
        {},
        {"installation_id": "77", "state": "forged", "redirect": "https://evil.example", "next": "//evil.example"},
    ],
)
def test_installed_callback_refuses_a_bad_state(tmp_path: Path, github: FakeGitHub, params: dict[str, str]) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    config_before = harness.config_store.config_file.read_bytes()
    response = harness.get(INSTALLED, **params)
    assert response.status_code == 303
    assert response.headers["location"] == "/?step=github&github_error=expired"
    assert response.text == ""
    assert github.requests == []
    assert harness.config_store.config_file.read_bytes() == config_before


def test_redirect_parameters_never_choose_the_target(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    response = harness.get(
        INSTALLED,
        installation_id="77",
        state=_install_state(harness, github),
        redirect="https://evil.example",
        next="//evil.example",
        return_to="https://evil.example",
    )
    assert response.headers["location"] == "/?step=github&github=installed"


def test_installed_callback_state_is_single_use(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    state = _install_state(harness, github)
    first = harness.get(INSTALLED, installation_id="77", state=state)
    assert first.headers["location"] == "/?step=github&github=installed"
    harness.config_store.unset(["HENCHMEN_GITHUB_APP_INSTALLATION_ID"])
    requests_before = len(github.requests)

    replay = harness.get(INSTALLED, installation_id="77", state=state)

    assert replay.headers["location"] == "/?step=github&github_error=expired"
    assert len(github.requests) == requests_before
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == ""


def test_an_expired_install_state_never_calls_github(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    clock = [1_000_000.0]
    harness.app.state.callback_states = CallbackStateStore(
        harness.app.state.callback_states.path, ttl_seconds=60, clock=lambda: clock[0]
    )
    state = _install_state(harness, github)
    clock[0] += 61
    response = harness.get(INSTALLED, installation_id="77", state=state)
    assert response.headers["location"] == "/?step=github&github_error=expired"
    assert github.requests == []
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == ""


def test_a_manifest_state_cannot_complete_the_installation(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    response = harness.get(INSTALLED, installation_id="77", state=_issue_manifest_state(harness))
    assert response.headers["location"] == "/?step=github&github_error=expired"
    assert github.requests == []
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == ""


def test_installed_callback_refuses_an_installation_github_does_not_know(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _created_app(harness, github)
    response = harness.get(
        INSTALLED, installation_id="12345", setup_action="install", state=_install_state(harness, github)
    )
    assert response.headers["location"] == "/?step=github&github_error=installation"
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == ""


@pytest.mark.parametrize(
    "identity",
    [
        {"app_slug": "someone-elses-app", "app_id": "999"},
        {"app_slug": "henchmen-test", "app_id": "999"},
    ],
)
def test_installed_callback_refuses_an_installation_of_another_app(
    tmp_path: Path, github: FakeGitHub, identity: dict[str, str]
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _created_app(harness, github)
    github.installations["77"] = FakeGitHub.installation("77", "mallory", **identity)
    response = harness.get(
        INSTALLED, installation_id="77", setup_action="install", state=_install_state(harness, github)
    )
    assert response.headers["location"] == "/?step=github&github_error=installation"
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == ""
    assert "github_account" not in harness.setup_store.load().server_choices


@pytest.mark.parametrize("installation_id", ["", "abc", "../app", "1" * 21, "７７"])
def test_a_malformed_installation_id_is_never_sent_to_github(
    tmp_path: Path, github: FakeGitHub, installation_id: str
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    response = harness.get(INSTALLED, installation_id=installation_id, state=_install_state(harness, github))
    assert response.headers["location"] == "/?step=github&github_error=installation"
    assert github.requests == []


def test_installed_callback_uses_the_state_bound_api_url(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    state = _install_state(
        harness, github, api_url="https://api.bound.example.test", web_url="https://bound.example.test"
    )
    _append_config(harness, tmp_path, "HENCHMEN_GITHUB_API_URL=https://api.changed.example.test\n")
    response = harness.get(INSTALLED, installation_id="77", state=state)
    assert response.headers["location"] == "/?step=github&github=installed"
    assert [str(request.url) for request in github.requests] == ["https://api.bound.example.test/app/installations/77"]


def test_an_install_state_without_usable_urls_never_calls_github(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    state = harness.app.state.callback_states.issue(INSTALL_PURPOSE, {"slug": github.app_slug})
    response = harness.get(INSTALLED, installation_id="77", state=state)
    assert response.headers["location"] == "/?step=github&github_error=configuration"
    assert github.requests == []
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == ""


def test_installed_callback_without_a_usable_app_saves_nothing(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    github.installations["77"] = FakeGitHub.installation("77", "acme")
    response = harness.get(INSTALLED, installation_id="77", state=_install_state(harness, github))
    assert response.headers["location"] == "/?step=github&github_error=installation"
    assert github.requests == []
    assert not harness.config_store.config_file.exists()


def test_installed_callback_reports_a_failed_write(
    tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    state = _install_state(harness, github)
    config_before = harness.config_store.config_file.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(harness.config_store, "update", fail)
    response = harness.get(INSTALLED, installation_id="77", state=state)
    assert response.headers["location"] == "/?step=github&github_error=storage"
    assert harness.config_store.config_file.read_bytes() == config_before
    assert "github_account" not in harness.setup_store.load().server_choices


def test_installed_callback_logs_no_state_or_app_jwt(
    tmp_path: Path, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    from henchmen.utils.redaction import install_secret_redaction

    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    state = _install_state(harness, github)
    original_factory = logging.getLogRecordFactory()
    # As in production: the test client's own request line carries the state, like any access log.
    install_secret_redaction()
    try:
        with caplog.at_level(logging.DEBUG):
            response = harness.get(INSTALLED, installation_id="77", state=state)
    finally:
        logging.setLogRecordFactory(original_factory)
    assert response.headers["location"] == "/?step=github&github=installed"
    assert not any(state in record.getMessage() for record in caplog.records if record.name.startswith("henchmen"))
    assert caplog.records
    app_jwt = github.requests[0].headers["authorization"].removeprefix("Bearer ")
    for secret in [state, app_jwt, *_secret_texts()]:
        assert secret not in caplog.text
        assert secret not in response.headers["location"]


# -- the install link (C9) --------------------------------------------------------


def test_installation_link_issues_a_fresh_single_use_state(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _created_app(harness, github)

    first = harness.post(f"{BASE}/installation/link").json()
    second = harness.post(f"{BASE}/installation/link").json()

    assert first["ok"] is True
    url = first["details"]["install_url"]
    assert url.startswith(f"https://github.com/apps/{github.app_slug}/installations/new?state=")
    state = parse_qs(urlsplit(url).query)["state"][0]
    other = parse_qs(urlsplit(second["details"]["install_url"]).query)["state"][0]
    assert state != other
    states: CallbackStateStore = harness.app.state.callback_states
    assert states.consume(INSTALL_PURPOSE, state) == {"slug": github.app_slug, "api_url": API_URL, "web_url": WEB_URL}
    assert states.consume(INSTALL_PURPOSE, state) is None
    assert github.requests == []


def test_installation_link_state_completes_the_installation(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _installable(harness, github)
    url = harness.post(f"{BASE}/installation/link").json()["details"]["install_url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    harness.client.cookies.clear()
    response = harness.get(INSTALLED, installation_id="77", setup_action="install", state=state)
    assert response.headers["location"] == "/?step=github&github=installed"
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == "77"


def test_installation_link_uses_the_configured_github_urls(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _created_app(harness, github)
    _append_config(
        harness,
        tmp_path,
        "HENCHMEN_GITHUB_API_URL=https://api.ghe.example.test\nHENCHMEN_GITHUB_WEB_URL=https://ghe.example.test\n",
    )
    url = harness.post(f"{BASE}/installation/link").json()["details"]["install_url"]
    assert url.startswith(f"https://ghe.example.test/apps/{github.app_slug}/installations/new?state=")
    state = parse_qs(urlsplit(url).query)["state"][0]
    assert harness.app.state.callback_states.consume(INSTALL_PURPOSE, state) == {
        "slug": github.app_slug,
        "api_url": "https://api.ghe.example.test",
        "web_url": "https://ghe.example.test",
    }


def test_installation_link_without_an_app(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    body = harness.post(f"{BASE}/installation/link").json()
    assert body["ok"] is False
    assert "not been created" in body["problems"][0]["message"]
    assert not harness.app.state.callback_states.path.exists()


def test_installation_link_with_an_invalid_github_url_names_the_field(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _created_app(harness, github)
    _append_config(harness, tmp_path, "HENCHMEN_GITHUB_WEB_URL=http://github.example.com\n")
    response = harness.post(f"{BASE}/installation/link")
    body = response.json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "github_web_url"
    assert "github.example.com" not in response.text
    assert not harness.app.state.callback_states.path.exists()


def test_installation_link_requires_a_session(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    assert harness.post(f"{BASE}/installation/link").status_code == 401
    assert harness.post(f"{BASE}/installation/check").status_code == 401
    assert harness.get(f"{BASE}/repositories").status_code == 401
    assert harness.post(f"{BASE}/repository", {"repo": REPO}).status_code == 401
    assert not harness.app.state.callback_states.path.exists()
    assert github.requests == []


# -- Check again --------------------------------------------------------------------


def test_check_again_finds_the_single_installation(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _installable(harness, github)
    body = harness.post(f"{BASE}/installation/check").json()
    assert body == {"ok": True, "step": "github", "details": {"installation_id": "77", "account": "acme"}}
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == "77"
    assert SetupStep.GITHUB not in harness.setup_store.load().completed_steps


def test_check_again_before_approval_offers_an_admin_request(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _created_app(harness, github)
    body = harness.post(f"{BASE}/installation/check").json()
    assert body["ok"] is False
    action = body["problems"][0]["action"]
    assert f"https://github.com/apps/{github.app_slug}/installations/new" in action
    assert "Check again" in action


def test_check_again_ignores_installations_of_another_app(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _created_app(harness, github)
    github.installations["88"] = FakeGitHub.installation("88", "mallory", app_slug="other-app", app_id="999")
    body = harness.post(f"{BASE}/installation/check").json()
    assert body["ok"] is False
    assert "not installed" in body["problems"][0]["message"]
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == ""


def test_check_again_with_two_accounts_asks_for_one(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _created_app(harness, github)
    github.installations["77"] = FakeGitHub.installation("77", "acme")
    github.installations["78"] = FakeGitHub.installation("78", "globex")
    body = harness.post(f"{BASE}/installation/check").json()
    assert body["ok"] is False
    assert "more than one account" in body["problems"][0]["message"]
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == ""


def test_check_again_without_an_app(tmp_path: Path, github: FakeGitHub) -> None:
    body = _harness(tmp_path, github).post(f"{BASE}/installation/check").json()
    assert body["ok"] is False
    assert "not been created" in body["problems"][0]["message"]
    assert github.requests == []


def test_check_again_with_an_invalid_github_url_calls_nothing(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _installable(harness, github)
    _append_config(harness, tmp_path, "HENCHMEN_GITHUB_API_URL=https://user:hunter2-pw@api.github.com\n")
    response = harness.post(f"{BASE}/installation/check")
    assert response.json()["problems"][0]["field"] == "github_api_url"
    assert "hunter2" not in response.text
    assert github.requests == []


# -- repositories and the default repository ------------------------------------------


def test_repositories_lists_what_the_installation_can_see(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _created_app(harness, github, installation_id="77")
    github.installations["77"] = FakeGitHub.installation("77", "acme")
    github.repositories = [FakeGitHub.repository(REPO), FakeGitHub.repository("acme/api", default_branch="develop")]

    response = harness.get(f"{BASE}/repositories")

    assert response.json()["details"]["repositories"] == [
        {"full_name": "acme/api", "default_branch": "develop", "private": True},
        {"full_name": REPO, "default_branch": "main", "private": True},
    ]
    assert response.json()["details"]["truncated"] is False
    assert "ghs_" not in response.text
    # Listed with an installation token minted for the listing, never the app JWT.
    listing = [request for request in github.requests if request.url.path == "/installation/repositories"]
    assert listing
    assert listing[0].headers["authorization"] == f"Bearer {github.minted[0]['token']}"


def test_repositories_before_installation(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _created_app(harness, github)
    body = harness.get(f"{BASE}/repositories").json()
    assert body["ok"] is False
    assert "Install the GitHub App" in body["problems"][0]["message"]
    assert github.requests == []


def test_repositories_when_github_refuses_a_token(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _created_app(harness, github, installation_id="77")
    github.installations["77"] = FakeGitHub.installation("77", "acme")
    github.token_status = 401
    body = harness.get(f"{BASE}/repositories").json()
    assert body["ok"] is False
    assert "refused" in body["problems"][0]["message"]


def test_choosing_a_repository_completes_the_step(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _ready_to_choose(harness, github, bot_id=987654)

    response = harness.post(f"{BASE}/repository", {"repo": "Acme/WebApp"})

    body = response.json()
    assert body["ok"] is True
    assert body["details"] == {"default_repo": REPO, "default_branch": "main", "git_author": "henchmen-test[bot]"}
    config = harness.config_store
    assert config.get("HENCHMEN_GITHUB_DEFAULT_REPO") == REPO
    assert config.get("HENCHMEN_GITHUB_DEFAULT_ORG") == "acme"
    assert config.get("HENCHMEN_GIT_AUTHOR_NAME") == "henchmen-test[bot]"
    assert config.get("HENCHMEN_GIT_AUTHOR_EMAIL") == "987654+henchmen-test[bot]@users.noreply.github.com"
    assert SetupStep.GITHUB in harness.setup_store.load().completed_steps
    assert "ghs_" not in response.text
    details = harness.get(BASE).json()["details"]
    assert (details["installed"], details["default_repo"], details["completed"]) == (True, REPO, True)


def test_repository_outside_the_installation_is_refused(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _ready_to_choose(harness, github)
    github.repositories = [FakeGitHub.repository("acme/api")]

    body = harness.post(f"{BASE}/repository", {"repo": REPO}).json()

    assert body["ok"] is False
    problem = body["problems"][0]
    assert problem["field"] == "repo"
    assert "https://github.com/organizations/acme/settings/installations/77" in problem["action"]
    assert harness.config_store.get("HENCHMEN_GITHUB_DEFAULT_REPO") == ""
    assert SetupStep.GITHUB not in harness.setup_store.load().completed_steps


def test_missing_write_permissions_are_explained(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _ready_to_choose(harness, github, permissions={"contents": "write", "pull_requests": "read", "metadata": "read"})

    body = harness.post(f"{BASE}/repository", {"repo": REPO}).json()

    assert body["ok"] is False
    assert "open pull requests" in body["problems"][0]["message"]
    assert harness.config_store.get("HENCHMEN_GITHUB_DEFAULT_REPO") == ""
    assert SetupStep.GITHUB not in harness.setup_store.load().completed_steps


def test_a_saved_installation_of_another_app_cannot_complete_the_step(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _ready_to_choose(harness, github, app_slug="other-app", app_id="999")

    body = harness.post(f"{BASE}/repository", {"repo": REPO}).json()

    assert body["ok"] is False
    assert "no longer installed" in body["problems"][0]["message"]
    assert harness.config_store.get("HENCHMEN_GITHUB_DEFAULT_REPO") == ""
    assert SetupStep.GITHUB not in harness.setup_store.load().completed_steps


def test_a_failed_repository_write_does_not_complete_the_step(
    tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, github)
    _ready_to_choose(harness, github)

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(harness.config_store, "update", fail)
    body = harness.post(f"{BASE}/repository", {"repo": REPO}).json()
    assert body["ok"] is False
    assert "writable" in body["problems"][0]["action"]
    assert SetupStep.GITHUB not in harness.setup_store.load().completed_steps


def test_repository_must_be_owner_slash_name(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    assert harness.post(f"{BASE}/repository", {"repo": "webapp"}).status_code == 422
    assert harness.post(f"{BASE}/repository", {"repo": REPO, "extra": "x"}).status_code == 422
    assert github.requests == []


# -- review fixes: completion invalidation, authoritative app id, truncation, bounds ---------


def _complete_the_step(harness: ConsoleHarness, github: FakeGitHub) -> None:
    _ready_to_choose(harness, github)
    assert harness.post(f"{BASE}/repository", {"repo": REPO}).json()["ok"] is True
    assert SetupStep.GITHUB in harness.setup_store.load().completed_steps


def test_recreating_the_app_reopens_a_completed_step(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _complete_the_step(harness, github)
    _add_conversion(github)

    response = harness.get(CALLBACK, code="code123", state=_issue_manifest_state(harness))

    assert response.headers["location"].startswith(f"https://github.com/apps/{SLUG}/installations/new?state=")
    assert SetupStep.GITHUB not in harness.setup_store.load().completed_steps
    assert harness.get(BASE).json()["details"]["completed"] is False


def test_a_failed_reopen_after_recreating_the_app_fails_closed(
    tmp_path: Path, github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _add_conversion(github)
    state = _issue_manifest_state(harness)

    def fail(step: SetupStep) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(harness.setup_store, "record_step_incomplete", fail)
    response = harness.get(CALLBACK, code="code123", state=state)
    assert response.headers["location"] == "/?step=github&github_error=storage"


def test_a_different_installation_from_the_callback_reopens_the_step(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _complete_the_step(harness, github)
    github.installations["78"] = FakeGitHub.installation("78", "acme")
    harness.client.cookies.clear()

    same = harness.get(INSTALLED, installation_id="77", state=_install_state(harness, github))
    assert same.headers["location"] == "/?step=github&github=installed"
    assert SetupStep.GITHUB in harness.setup_store.load().completed_steps

    changed = harness.get(INSTALLED, installation_id="78", state=_install_state(harness, github))
    assert changed.headers["location"] == "/?step=github&github=installed"
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == "78"
    assert SetupStep.GITHUB not in harness.setup_store.load().completed_steps


def test_a_different_installation_from_check_again_reopens_the_step(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _complete_the_step(harness, github)

    assert harness.post(f"{BASE}/installation/check").json()["details"]["installation_id"] == "77"
    assert SetupStep.GITHUB in harness.setup_store.load().completed_steps

    del github.installations["77"]
    github.installations["79"] = FakeGitHub.installation("79", "acme")
    body = harness.post(f"{BASE}/installation/check").json()
    assert body["details"]["installation_id"] == "79"
    assert SetupStep.GITHUB not in harness.setup_store.load().completed_steps
    assert harness.get(BASE).json()["details"]["completed"] is False


def test_status_completed_needs_an_installation_and_a_default_repo(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    harness.setup_store.record_step_complete(SetupStep.GITHUB)
    assert harness.get(BASE).json()["details"]["completed"] is False
    harness.config_store.update({"HENCHMEN_GITHUB_APP_INSTALLATION_ID": "77"}, section="GitHub")
    assert harness.get(BASE).json()["details"]["completed"] is False
    harness.config_store.update({"HENCHMEN_GITHUB_DEFAULT_REPO": REPO}, section="GitHub")
    assert harness.get(BASE).json()["details"]["completed"] is True


def test_a_stale_saved_slug_never_refuses_the_right_app(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _created_app(harness, github)
    # The saved slug (and the one bound into the state) is App A's; GitHub says App B -- ours by app_id.
    harness.setup_store.set_server_choices({"github_app_slug": "henchmen-old-name"})
    github.installations["77"] = FakeGitHub.installation("77", "acme", app_slug="henchmen-new-name")
    state = harness.app.state.callback_states.issue(
        INSTALL_PURPOSE, {"slug": "henchmen-old-name", "api_url": API_URL, "web_url": WEB_URL}
    )

    response = harness.get(INSTALLED, installation_id="77", setup_action="install", state=state)

    assert response.headers["location"] == "/?step=github&github=installed"
    assert harness.config_store.get("HENCHMEN_GITHUB_APP_INSTALLATION_ID") == "77"
    (tmp_path / "second").mkdir()
    signed_in = _harness(tmp_path / "second", github)
    _created_app(signed_in, github)
    signed_in.setup_store.set_server_choices({"github_app_slug": "henchmen-old-name"})
    assert signed_in.post(f"{BASE}/installation/check").json()["details"]["installation_id"] == "77"


@pytest.mark.parametrize(
    "params",
    [
        {"installation_id": "77", "state": "x" * 513},
        {"installation_id": "7" * 33, "state": "s"},
        {"installation_id": "77", "setup_action": "i" * 33, "state": "s"},
    ],
)
def test_installed_callback_parameters_are_length_bounded(
    tmp_path: Path, github: FakeGitHub, params: dict[str, str]
) -> None:
    harness = _harness(tmp_path, github, signed_in=False)
    _installable(harness, github)
    config_before = harness.config_store.config_file.read_bytes()
    response = harness.get(INSTALLED, **params)
    assert response.status_code == 422
    assert "location" not in response.headers
    assert github.requests == []
    assert harness.config_store.config_file.read_bytes() == config_before


def test_repositories_report_a_truncated_listing(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _ready_to_choose(harness, github)
    details = harness.get(f"{BASE}/repositories").json()["details"]
    assert details["truncated"] is False
    github.repository_total_count = 5000
    details = harness.get(f"{BASE}/repositories").json()["details"]
    assert details["truncated"] is True
    assert [item["full_name"] for item in details["repositories"]] == [REPO]


def test_a_repository_beyond_a_truncated_listing_is_confirmed_with_github(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _ready_to_choose(harness, github)
    github.repository_total_count = 5000
    github.unlisted_repositories = [FakeGitHub.repository("acme/far-away", default_branch="trunk")]

    response = harness.post(f"{BASE}/repository", {"repo": "ACME/far-away"})

    body = response.json()
    assert body["ok"] is True
    assert body["details"]["default_repo"] == "acme/far-away"
    assert body["details"]["default_branch"] == "trunk"
    assert harness.config_store.get("HENCHMEN_GITHUB_DEFAULT_REPO") == "acme/far-away"
    lookups = [request for request in github.requests if request.url.path.startswith("/repos/")]
    assert len(lookups) == 1
    assert lookups[0].headers["authorization"].startswith("Bearer ghs_")
    assert "ghs_" not in response.text
    assert SetupStep.GITHUB in harness.setup_store.load().completed_steps


@pytest.mark.parametrize("unlisted", [[], [{"full_name": "globex/far-away"}]])
def test_a_repository_github_does_not_confirm_is_refused(
    tmp_path: Path, github: FakeGitHub, unlisted: list[dict[str, object]]
) -> None:
    harness = _harness(tmp_path, github)
    _ready_to_choose(harness, github)
    github.repository_total_count = 5000
    github.unlisted_repositories = unlisted
    repo = "globex/far-away" if unlisted else "acme/far-away"

    body = harness.post(f"{BASE}/repository", {"repo": repo}).json()

    assert body["ok"] is False
    assert body["problems"][0]["field"] == "repo"
    assert harness.config_store.get("HENCHMEN_GITHUB_DEFAULT_REPO") == ""
    assert SetupStep.GITHUB not in harness.setup_store.load().completed_steps


def test_an_untruncated_listing_is_never_second_guessed(tmp_path: Path, github: FakeGitHub) -> None:
    harness = _harness(tmp_path, github)
    _ready_to_choose(harness, github)
    github.unlisted_repositories = [FakeGitHub.repository("acme/far-away")]
    body = harness.post(f"{BASE}/repository", {"repo": "acme/far-away"}).json()
    assert body["ok"] is False
    assert not [request for request in github.requests if request.url.path.startswith("/repos/")]
