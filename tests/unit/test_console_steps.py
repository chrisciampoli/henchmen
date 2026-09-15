"""The step-router contract between Plans 2A, 2B and 2C."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient

from henchmen.console.app import PUBLIC_PATHS, ConsoleMode, create_console_app
from henchmen.console.auth import SESSION_COOKIE, ConsoleAuth
from henchmen.console.state import SetupStateStore, SetupStep
from henchmen.console.steps import (
    STEP_ROUTE_PREFIX,
    StepFailure,
    StepProblem,
    StepRoutes,
    StepSuccess,
    discover_step_routes,
    get_setup_store,
    step_failed,
    step_succeeded,
    validate_step_routes,
)

LOCAL = "http://127.0.0.1:8000"
ORIGIN = {"origin": LOCAL}


def _github_router() -> APIRouter:
    router = APIRouter()

    @router.post("/check")
    async def check(request: Request, body: dict[str, bool]) -> StepSuccess | StepFailure:
        store = get_setup_store(request)
        if body.get("good"):
            return step_succeeded(store, SetupStep.GITHUB, {"repos": 1})
        problem = StepProblem(field="token", message="GitHub rejected the token.", action="Create a new token.")
        return step_failed(SetupStep.GITHUB, problem)

    @router.get("/callback")
    async def callback() -> dict[str, bool]:
        return {"public": True}

    @router.get("/private")
    async def private() -> dict[str, bool]:
        return {"public": False}

    @router.get("/installed")
    async def installed() -> dict[str, bool]:
        return {"public": True}

    @router.get("/installed-evil")
    async def installed_evil() -> dict[str, bool]:
        return {"public": False}

    @router.get("/installed/x")
    async def installed_sub() -> dict[str, bool]:
        return {"public": False}

    return router


GITHUB_PUBLIC = frozenset(
    {
        "/console/api/steps/github/callback",
        "/console/api/steps/github/installed",
    }
)


@pytest.fixture
def console(tmp_path: Path):
    store = SetupStateStore(tmp_path / "setup-state.json")
    auth = ConsoleAuth(setup_token="tok", signing_key=b"k" * 32)
    app = create_console_app(
        mode=ConsoleMode.SETUP,
        store=store,
        auth=auth,
        config_file=tmp_path / "henchmen.env",
        secrets_dir=tmp_path / "secrets",
        on_apply=lambda: None,
        step_routes={SetupStep.GITHUB: StepRoutes(router=_github_router(), public_paths=GITHUB_PUBLIC)},
    )
    client = TestClient(app, base_url=LOCAL)
    return client, store, auth


def test_step_routes_are_mounted_under_the_step_prefix_behind_the_guard(console) -> None:
    client, store, auth = console
    url = f"{STEP_ROUTE_PREFIX}/github/check"
    assert url == "/console/api/steps/github/check"
    assert client.post(url, json={"good": True}, headers=ORIGIN).status_code == 401
    client.cookies.set(SESSION_COOKIE, auth.issue_session())
    assert client.post(url, json={"good": True}).status_code == 403, "state changes need a matching Origin"
    assert store.load().completed_steps == []


def test_success_has_the_contract_shape_and_records_completion(console) -> None:
    client, store, auth = console
    client.cookies.set(SESSION_COOKIE, auth.issue_session())
    resp = client.post("/console/api/steps/github/check", json={"good": True}, headers=ORIGIN)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "step": "github", "details": {"repos": 1}}
    assert store.load().completed_steps == [SetupStep.GITHUB]


def test_failure_has_the_contract_shape_and_records_nothing(console) -> None:
    client, store, auth = console
    client.cookies.set(SESSION_COOKIE, auth.issue_session())
    resp = client.post("/console/api/steps/github/check", json={"good": False}, headers=ORIGIN)
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": False,
        "step": "github",
        "problems": [{"field": "token", "message": "GitHub rejected the token.", "action": "Create a new token."}],
    }
    assert store.load().completed_steps == []


def test_malformed_step_input_is_422(console) -> None:
    client, _, auth = console
    client.cookies.set(SESSION_COOKIE, auth.issue_session())
    assert client.post("/console/api/steps/github/check", json=["nope"], headers=ORIGIN).status_code == 422


def test_step_failure_needs_at_least_one_problem() -> None:
    with pytest.raises(ValueError):
        StepFailure(step=SetupStep.JIRA, problems=[])


def test_step_failed_is_variadic_and_needs_at_least_one_problem() -> None:
    problem = StepProblem(message="bad")
    built = step_failed(SetupStep.JIRA, problem)
    assert built.problems == [problem]
    with pytest.raises(ValueError):
        step_failed(SetupStep.JIRA)


def test_public_paths_are_declared_once() -> None:
    assert frozenset({"/console/api/status"}) == PUBLIC_PATHS


def test_a_step_module_public_route_needs_no_session_but_its_other_routes_do(console) -> None:
    client, *_ = console
    assert client.get("/console/api/steps/github/callback").json() == {"public": True}
    assert client.get("/console/api/steps/github/private").status_code == 401


def test_public_path_matching_is_exact_and_cannot_be_widened_by_a_prefix(console) -> None:
    """A public path is an exact match in the guard: neither a longer sibling path nor a
    path nested under it becomes public just because it starts with the public string."""
    client, *_ = console
    assert client.get("/console/api/steps/github/installed").json() == {"public": True}
    assert client.get("/console/api/steps/github/installed-evil").status_code == 401
    assert client.get("/console/api/steps/github/installed/x").status_code == 401


def test_create_console_app_validates_injected_step_routes_too(tmp_path: Path) -> None:
    """Not just discovery: a `step_routes` mapping passed straight into create_console_app
    (bypassing henchmen.console.steps.<step> modules entirely) is held to the same
    fail-closed rules -- an injected public path outside the step's own prefix raises."""
    store = SetupStateStore(tmp_path / "setup-state.json")
    auth = ConsoleAuth(setup_token="tok", signing_key=b"k" * 32)
    bad_routes = {SetupStep.GITHUB: StepRoutes(router=APIRouter(), public_paths=frozenset({"/console/api/apply"}))}
    with pytest.raises(ValueError, match="PUBLIC_ROUTE_PATHS"):
        create_console_app(
            mode=ConsoleMode.SETUP,
            store=store,
            auth=auth,
            config_file=tmp_path / "henchmen.env",
            secrets_dir=tmp_path / "secrets",
            on_apply=lambda: None,
            step_routes=bad_routes,
        )


def test_validate_step_routes_is_the_single_check_used_by_both_paths() -> None:
    validate_step_routes(SetupStep.GITHUB, StepRoutes(router=APIRouter()))
    with pytest.raises(TypeError, match="without a prefix"):
        validate_step_routes(SetupStep.GITHUB, StepRoutes(router=APIRouter(prefix="/github")))
    with pytest.raises(ValueError, match="PUBLIC_ROUTE_PATHS"):
        validate_step_routes(
            SetupStep.GITHUB, StepRoutes(router=APIRouter(), public_paths=frozenset({"/console/api/apply"}))
        )


def test_the_setup_state_put_cannot_write_server_choices(console) -> None:
    client, store, auth = console
    client.cookies.set(SESSION_COOKIE, auth.issue_session())
    body = {"current_step": "github", "server_choices": {"github_app_slug": "forged"}}
    assert client.put("/console/api/setup/state", json=body, headers=ORIGIN).status_code == 422
    assert store.load().server_choices == {}


def _fake_step_module(
    monkeypatch: pytest.MonkeyPatch, step: SetupStep, router: object, public: frozenset[str] | None = None
) -> None:
    name = f"henchmen.console.steps.{step.value}"
    module = types.ModuleType(name)
    module.router = router  # type: ignore[attr-defined]
    if public is not None:
        module.PUBLIC_ROUTE_PATHS = public  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, module)


def test_no_step_modules_ship_with_plan_2a_so_discovery_skips_them() -> None:
    assert all(isinstance(routes, StepRoutes) for routes in discover_step_routes().values())


def test_discovery_reads_the_router_and_its_public_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    router = APIRouter()
    public = frozenset({"/console/api/steps/jira/callback"})
    _fake_step_module(monkeypatch, SetupStep.JIRA, router, public)
    discovered = discover_step_routes()[SetupStep.JIRA]
    assert discovered.router is router
    assert discovered.public_paths == public


def test_discovery_defaults_to_no_public_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_step_module(monkeypatch, SetupStep.JIRA, APIRouter())
    assert discover_step_routes()[SetupStep.JIRA].public_paths == frozenset()


def test_discovery_rejects_a_step_module_without_a_router(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_step_module(monkeypatch, SetupStep.SLACK, object())
    with pytest.raises(TypeError, match="henchmen.console.steps.slack"):
        discover_step_routes()


def test_discovery_rejects_a_router_declared_with_a_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_step_module(monkeypatch, SetupStep.SLACK, APIRouter(prefix="/slack"))
    with pytest.raises(TypeError, match="without a prefix"):
        discover_step_routes()


@pytest.mark.parametrize("path", ["/console/api/apply", "/console/api/steps/github/callback", "/"])
def test_a_step_cannot_make_paths_outside_its_own_prefix_public(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    _fake_step_module(monkeypatch, SetupStep.JIRA, APIRouter(), frozenset({path}))
    with pytest.raises(ValueError, match="PUBLIC_ROUTE_PATHS"):
        discover_step_routes()


def test_a_step_public_path_with_a_trailing_slash_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_step_module(monkeypatch, SetupStep.JIRA, APIRouter(), frozenset({"/console/api/steps/jira/callback/"}))
    with pytest.raises(ValueError, match="PUBLIC_ROUTE_PATHS"):
        discover_step_routes()


def test_a_step_public_path_containing_dot_dot_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    path = "/console/api/steps/jira/../github/check"
    _fake_step_module(monkeypatch, SetupStep.JIRA, APIRouter(), frozenset({path}))
    with pytest.raises(ValueError, match="PUBLIC_ROUTE_PATHS"):
        discover_step_routes()


def test_a_non_apirouter_router_attribute_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_step_module(monkeypatch, SetupStep.SLACK, router={"not": "a router"})
    with pytest.raises(TypeError, match="henchmen.console.steps.slack"):
        discover_step_routes()


def test_discovery_only_swallows_module_not_found_for_the_exact_step_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step module that exists but fails on one of ITS OWN imports must propagate that
    failure, not be silently treated as 'no step module here' (Ruling in Task 9 brief:
    discovery may only catch ModuleNotFoundError naming the exact module it tried to
    import; check exc.name)."""
    import henchmen.console.steps as steps_module

    target = f"{steps_module.__name__}.{SetupStep.SLACK.value}"
    real_import_module = steps_module.importlib.import_module

    def flaky_import(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if name == target:
            raise ModuleNotFoundError("No module named 'not_a_real_dependency'", name="not_a_real_dependency")
        return real_import_module(name, *args, **kwargs)

    fake_importlib = types.SimpleNamespace(import_module=flaky_import)
    monkeypatch.setattr(steps_module, "importlib", fake_importlib)

    with pytest.raises(ModuleNotFoundError) as exc_info:
        discover_step_routes()
    assert exc_info.value.name == "not_a_real_dependency"
