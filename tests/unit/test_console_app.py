"""Tests for the Console HTTP routes."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from henchmen.console.app import ConsoleMode, create_console_app
from henchmen.console.auth import SESSION_COOKIE, ConsoleAuth
from henchmen.console.state import SetupState, SetupStateStore, SetupStep

LOCAL = "http://127.0.0.1:8000"
ORIGIN = {"origin": LOCAL}


class _ApplyRecorder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


@pytest.fixture
def env(tmp_path: Path):
    store = SetupStateStore(tmp_path / "setup-state.json")
    auth = ConsoleAuth(setup_token="tok", signing_key=b"k" * 32)
    applied = _ApplyRecorder()
    config = tmp_path / "henchmen.env"
    app = create_console_app(mode=ConsoleMode.SETUP, store=store, auth=auth, config_file=config, on_apply=applied)
    client = TestClient(app, base_url=LOCAL, follow_redirects=False)
    return client, store, auth, applied, config


def _signed_in(client: TestClient, auth: ConsoleAuth) -> TestClient:
    client.cookies.set(SESSION_COOKIE, auth.issue_session())
    return client


def test_status_is_public(env) -> None:
    client, *_ = env
    body = client.get("/console/api/status").json()
    assert body["mode"] == "setup"
    assert body["setup_completed"] is False
    assert body["version"]


def test_session_exchange_sets_a_strict_http_only_cookie(env) -> None:
    client, _, auth, *_ = env
    response = client.get("/console/session", params={"setup_token": "tok"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie.replace("samesite", "SameSite")
    value = cookie.split(f"{SESSION_COOKIE}=", 1)[1].split(";", 1)[0]
    assert auth.verify_session(value)


def test_session_cookie_persists_for_the_accepted_session_lifetime(env) -> None:
    # Without Max-Age the cookie dies with the browser even though the server would still accept it.
    client, _, auth, *_ = env
    response = client.get("/console/session", params={"setup_token": "tok"})
    assert f"Max-Age={auth.max_age_seconds}" in response.headers["set-cookie"]


def test_session_exchange_rejects_a_wrong_token(env) -> None:
    client, *_ = env
    response = client.get("/console/session", params={"setup_token": "nope"})
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_state_requires_a_session(env) -> None:
    client, *_ = env
    assert client.get("/console/api/setup/state").status_code == 401


def test_state_round_trip(env) -> None:
    client, store, auth, *_ = env
    _signed_in(client, auth)
    update = {
        "current_step": "github",
        "completed_steps": ["welcome", "ai_provider"],
        "skipped_steps": [],
        "choices": {"llm_provider": "anthropic"},
    }
    assert client.put("/console/api/setup/state", json=update, headers=ORIGIN).status_code == 200
    assert store.load().current_step == SetupStep.GITHUB
    assert client.get("/console/api/setup/state").json()["choices"] == {"llm_provider": "anthropic"}


def test_state_update_cannot_mark_setup_completed(env) -> None:
    client, store, auth, *_ = env
    _signed_in(client, auth)
    update = {"current_step": "welcome", "completed_steps": [], "skipped_steps": [], "choices": {}, "completed": True}
    response = client.put("/console/api/setup/state", json=update, headers=ORIGIN)
    assert response.status_code == 422
    assert store.load().completed is False


def test_apply_refuses_without_required_steps(env) -> None:
    client, _, auth, applied, config = env
    _signed_in(client, auth)
    config.write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")
    response = client.post("/console/api/apply", headers=ORIGIN)
    assert response.status_code == 409
    assert "github" in response.json()["detail"]
    assert applied.calls == 0


def test_apply_refuses_without_a_config_file(env) -> None:
    client, store, auth, applied, _ = env
    _signed_in(client, auth)
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB]))
    response = client.post("/console/api/apply", headers=ORIGIN)
    assert response.status_code == 409
    assert applied.calls == 0


def test_apply_marks_completed_and_requests_a_restart(env) -> None:
    client, store, auth, applied, config = env
    _signed_in(client, auth)
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB]))
    config.write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")
    response = client.post("/console/api/apply", headers=ORIGIN)
    assert response.status_code == 202
    assert response.json() == {"restarting": True}
    assert store.load().completed is True
    assert applied.calls == 1


def test_apply_refuses_a_configuration_that_cannot_start(env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, auth, applied, config = env
    # conftest strips HENCHMEN_* already; the file alone decides the LLM provider and its key.
    monkeypatch.delenv("HENCHMEN_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("HENCHMEN_ANTHROPIC_API_KEY", raising=False)
    _signed_in(client, auth)
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB]))
    config.write_text("HENCHMEN_PROVIDER=local\nHENCHMEN_LLM_PROVIDER=anthropic\n", encoding="utf-8")
    response = client.post("/console/api/apply", headers=ORIGIN)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert any("HENCHMEN_ANTHROPIC_API_KEY" in problem for problem in detail["problems"])
    assert store.load().completed is False
    assert applied.calls == 0


def test_apply_refuses_a_configuration_that_does_not_parse(env) -> None:
    client, store, auth, applied, config = env
    _signed_in(client, auth)
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB]))
    config.write_text("HENCHMEN_PROVIDER=local\nHENCHMEN_LOCAL_SERVE_PORT=not-a-port\n", encoding="utf-8")
    response = client.post("/console/api/apply", headers=ORIGIN)
    assert response.status_code == 409
    problems = response.json()["detail"]["problems"]
    assert problems and any("local_serve_port" in problem for problem in problems)
    assert store.load().completed is False
    assert applied.calls == 0


def test_root_serves_the_console_page(env) -> None:
    client, *_ = env
    response = client.get("/")
    assert response.status_code == 200
    assert "Henchmen" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_non_local_host_is_refused_everywhere(env) -> None:
    client, *_ = env
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403


def test_session_exchange_is_one_time(env) -> None:
    client, *_ = env
    assert client.get("/console/session", params={"setup_token": "tok"}).status_code == 303
    client.cookies.clear()
    assert client.get("/console/session", params={"setup_token": "tok"}).status_code == 403


def test_signed_in_browser_opening_an_old_link_is_redirected_without_error(env) -> None:
    client, _, auth, *_ = env
    _signed_in(client, auth)
    response = client.get("/console/session", params={"setup_token": "already-used"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert auth.setup_token == "tok", "a signed-in visit must not burn the current token"
