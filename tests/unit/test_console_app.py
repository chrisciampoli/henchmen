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
    update = {"current_step": "github", "skipped_steps": ["slack"], "choices": {"llm_provider": "anthropic"}}
    assert client.put("/console/api/setup/state", json=update, headers=ORIGIN).status_code == 200
    loaded = store.load()
    assert loaded.current_step == SetupStep.GITHUB
    assert loaded.skipped_steps == [SetupStep.SLACK]
    assert client.get("/console/api/setup/state").json()["choices"] == {"llm_provider": "anthropic"}


@pytest.mark.parametrize(
    "extra",
    [{"completed": True}, {"completed_steps": ["ai_provider", "github"]}],
    ids=["completed", "completed_steps"],
)
def test_clients_cannot_write_completion(env, extra) -> None:
    client, store, auth, *_ = env
    _signed_in(client, auth)
    update = {"current_step": "welcome", "skipped_steps": [], "choices": {}, **extra}
    assert client.put("/console/api/setup/state", json=update, headers=ORIGIN).status_code == 422
    loaded = store.load()
    assert loaded.completed is False
    assert loaded.completed_steps == []


@pytest.mark.parametrize("step", ["ai_provider", "github", "welcome"])
def test_only_optional_steps_can_be_skipped(env, step) -> None:
    client, store, auth, *_ = env
    _signed_in(client, auth)
    update = {"current_step": "welcome", "skipped_steps": [step]}
    assert client.put("/console/api/setup/state", json=update, headers=ORIGIN).status_code == 422
    assert store.load().skipped_steps == []


@pytest.mark.parametrize(
    "choices",
    [
        {"github_token": "x"},
        {"anthropic_api_key": "x"},
        {"slack_signing_secret": "x"},
        {"note": "sk-ant-" + "a" * 30},
        {"Bad-Key": "x"},
        {"model": "x" * 257},
    ],
    ids=["token-key", "api-key", "secret-key", "secret-value", "bad-name", "too-long"],
)
def test_choices_refuse_anything_secret_or_malformed(env, choices) -> None:
    client, store, auth, *_ = env
    _signed_in(client, auth)
    resp = client.put("/console/api/setup/state", json={"current_step": "welcome", "choices": choices}, headers=ORIGIN)
    assert resp.status_code == 422
    assert store.load().choices == {}


def test_choice_rejection_names_the_key_and_never_the_value(env) -> None:
    """A secret-shaped value must never appear in the 422 body -- only the key name may
    (Ruling 4: the error text names the key, never the value)."""
    client, store, auth, *_ = env
    _signed_in(client, auth)
    distinctive_secret = "ghp_" + "Z9x8W7v6U5t4S3r2Q1p0" * 2
    update = {"current_step": "welcome", "choices": {"github_token": distinctive_secret}}
    resp = client.put("/console/api/setup/state", json=update, headers=ORIGIN)
    assert resp.status_code == 422
    assert distinctive_secret not in resp.text
    assert "github_token" in resp.text
    assert store.load().choices == {}


def test_choice_key_that_looks_like_a_secret_is_rejected_generically(env) -> None:
    """A key that is otherwise a well-formed choice name but is itself token-shaped (Ruling
    4: reject when redact(key) != key) must be refused generically -- the error text must
    not quote the key back, unlike the plain-format-violation case."""
    client, store, auth, *_ = env
    _signed_in(client, auth)
    token_shaped_key = "ghp_" + "a1b2c3d4e5f6g7h8i9j0"  # well-formed choice name, but redact() rewrites it
    update = {"current_step": "welcome", "choices": {token_shaped_key: "whatever"}}
    resp = client.put("/console/api/setup/state", json=update, headers=ORIGIN)
    assert resp.status_code == 422
    assert token_shaped_key not in resp.text
    assert "looks like a secret" in resp.text
    assert store.load().choices == {}


def test_non_secret_choices_such_as_a_jira_project_key_are_accepted(env) -> None:
    client, store, auth, *_ = env
    _signed_in(client, auth)
    choices = {"jira_project_key": "HEN", "llm_provider": "anthropic"}
    resp = client.put("/console/api/setup/state", json={"current_step": "jira", "choices": choices}, headers=ORIGIN)
    assert resp.status_code == 200
    assert store.load().choices == choices


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


def test_apply_generates_the_dispatch_api_token_once(env) -> None:
    from henchmen.cli.envfile import EnvFile

    client, store, auth, applied, config = env
    _signed_in(client, auth)
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB]))
    config.write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")
    assert client.post("/console/api/apply", headers=ORIGIN).status_code == 202
    token = EnvFile.load(config).get("HENCHMEN_DISPATCH_API_TOKEN")
    assert len(token) >= 40
    assert client.post("/console/api/apply", headers=ORIGIN).status_code == 202
    assert EnvFile.load(config).get("HENCHMEN_DISPATCH_API_TOKEN") == token


def test_apply_validates_the_file_not_the_seeded_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HENCHMEN_PROVIDER", "local")  # seeded by _serve at startup
    store = SetupStateStore(tmp_path / "setup-state.json")
    auth = ConsoleAuth(setup_token="tok", signing_key=b"k" * 32)
    config = tmp_path / "henchmen.env"
    app = create_console_app(
        mode=ConsoleMode.SETUP,
        store=store,
        auth=auth,
        config_file=config,
        on_apply=lambda: None,
        seeded_env={"HENCHMEN_PROVIDER": "local"},
    )
    client = _signed_in(TestClient(app, base_url=LOCAL), auth)
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB]))
    config.write_text("HENCHMEN_PROVIDER=gcp\n", encoding="utf-8")
    response = client.post("/console/api/apply", headers=ORIGIN)
    assert response.status_code == 409
    assert any("HENCHMEN_GCP_PROJECT_ID" in p for p in response.json()["detail"]["problems"])
    assert store.load().completed is False


def test_apply_refuses_a_configuration_that_cannot_start_leaves_the_file_untouched(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling P6: a refused apply must not write the config file or generate a .bak."""
    client, store, auth, applied, config = env
    monkeypatch.delenv("HENCHMEN_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("HENCHMEN_ANTHROPIC_API_KEY", raising=False)
    _signed_in(client, auth)
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB]))
    text = "HENCHMEN_PROVIDER=local\nHENCHMEN_LLM_PROVIDER=anthropic\n"
    config.write_text(text, encoding="utf-8")
    before = config.read_bytes()
    response = client.post("/console/api/apply", headers=ORIGIN)
    assert response.status_code == 409
    assert config.read_bytes() == before
    assert not config.with_name(config.name + ".bak").exists()
    assert applied.calls == 0


def test_apply_returns_500_and_does_not_restart_when_the_token_write_fails(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write (permissions, full disk) must never mark setup complete or restart."""
    from henchmen.console.config_store import ConfigStore

    client, store, auth, applied, config = env
    _signed_in(client, auth)
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB]))
    config.write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")

    def _boom(self: ConfigStore, token: str) -> None:
        raise PermissionError("disk full")

    monkeypatch.setattr(ConfigStore, "write_dispatch_api_token", _boom)

    response = client.post("/console/api/apply", headers=ORIGIN)

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "Dispatch API token" in detail
    assert "permissions" in detail.lower()
    assert "free space" in detail.lower()
    assert store.load().completed is False
    assert applied.calls == 0


def test_apply_does_not_generate_a_token_when_the_environment_already_provides_one(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment (e.g. a Secret Manager mount) outranks the file, so nothing is generated."""
    client, store, auth, applied, config = env
    monkeypatch.setenv("HENCHMEN_DISPATCH_API_TOKEN", "already-configured-by-the-environment")
    _signed_in(client, auth)
    store.save(SetupState(completed_steps=[SetupStep.AI_PROVIDER, SetupStep.GITHUB]))
    config.write_text("HENCHMEN_PROVIDER=local\n", encoding="utf-8")

    response = client.post("/console/api/apply", headers=ORIGIN)

    assert response.status_code == 202
    assert "HENCHMEN_DISPATCH_API_TOKEN" not in config.read_text(encoding="utf-8")
    assert not config.with_name(config.name + ".bak").exists()
    assert applied.calls == 1


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


def test_session_exchange_with_a_file_backed_token_store_is_one_time(tmp_path: Path) -> None:
    """Same behavior as the in-memory `env` fixture, but through the real file-backed
    ``SetupTokenStore`` that ``ConsoleAuth.load`` builds (Ruling B4 fix round)."""
    auth = ConsoleAuth.load(tmp_path / "secrets", setup_token=None)
    token = auth.setup_token
    store = SetupStateStore(tmp_path / "setup-state.json")
    app = create_console_app(
        mode=ConsoleMode.SETUP,
        store=store,
        auth=auth,
        config_file=tmp_path / "henchmen.env",
        on_apply=lambda: None,
    )
    client = TestClient(app, base_url=LOCAL, follow_redirects=False)

    first = client.get("/console/session", params={"setup_token": token})
    assert first.status_code == 303
    assert first.headers["location"] == "/"

    client.cookies.clear()
    second = client.get("/console/session", params={"setup_token": token})
    assert second.status_code == 403, "no session cookie and an already-spent token must be refused outright"

    # A signed-in browser that opens the same, now-spent link must be redirected, not errored.
    client.cookies.set(SESSION_COOKIE, auth.issue_session())
    third = client.get("/console/session", params={"setup_token": token})
    assert third.status_code == 303
    assert third.headers["location"] == "/"


# ---------------------------------------------------------------------------
# Task 11: needs-attention mode, service status
# ---------------------------------------------------------------------------


def test_status_has_no_problems_and_services_off_outside_run_mode(env) -> None:
    client, *_ = env
    body = client.get("/console/api/status").json()
    assert body["problems"] == []
    assert body["services"] == {"dispatch": "off", "mastermind": "off", "forge": "off"}


def _console(tmp_path: Path, mode: ConsoleMode, **kwargs):
    return create_console_app(
        mode=mode,
        store=SetupStateStore(tmp_path / "setup-state.json"),
        auth=ConsoleAuth(setup_token="tok", signing_key=b"k" * 32),
        config_file=tmp_path / "henchmen.env",
        on_apply=lambda: None,
        **kwargs,
    )


def test_attention_mode_status_carries_redacted_problems(tmp_path: Path) -> None:
    leaked = "sk-ant-" + "x" * 30
    app = _console(tmp_path, ConsoleMode.ATTENTION, problems=["HENCHMEN_ANTHROPIC_API_KEY is empty.", f"boom {leaked}"])
    body = TestClient(app, base_url=LOCAL).get("/console/api/status").json()
    assert body["mode"] == "attention"
    assert body["problems"][0] == "HENCHMEN_ANTHROPIC_API_KEY is empty."
    assert leaked not in body["problems"][1]


def test_problems_are_reported_only_in_attention_mode(tmp_path: Path) -> None:
    app = _console(tmp_path, ConsoleMode.RUN, problems=["stale"])
    assert TestClient(app, base_url=LOCAL).get("/console/api/status").json()["problems"] == []


def test_run_mode_status_reports_the_live_service_snapshot(tmp_path: Path) -> None:
    from henchmen.console.services import ServiceHealth, ServiceState

    health = ServiceHealth()
    app = _console(tmp_path, ConsoleMode.RUN, service_status=health.snapshot)
    client = TestClient(app, base_url=LOCAL)
    health.set_all(ServiceState.RUNNING)
    assert client.get("/console/api/status").json()["services"] == {
        "dispatch": "running",
        "mastermind": "running",
        "forge": "running",
    }


def test_attention_mode_exposes_no_service_routes(tmp_path: Path) -> None:
    """Ruling 2: the attention Console keeps the same guard as setup/run -- it never
    grows service routes, and a non-local Host is refused exactly like every other mode."""
    app = _console(tmp_path, ConsoleMode.ATTENTION, problems=["boom"])
    client = TestClient(app, base_url=LOCAL)
    assert client.get("/console/api/status").status_code == 200
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
