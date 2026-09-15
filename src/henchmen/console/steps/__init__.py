"""Per-step validation routers for the Console setup guide (cross-plan contract).

Each setup step that validates something lives in ``henchmen.console.steps.<step>``
(for example ``henchmen.console.steps.github``) and defines a module-level
``router: APIRouter`` declared without a prefix. ``create_console_app`` mounts
it at ``/console/api/steps/<step>``, behind the Console guard (loopback Host,
matching Origin on writes, signed session). Routes that must work without a
session (for example a callback from github.com, authorised by its own stored
``state``) are listed, as full paths under the step's own prefix, in a
module-level ``PUBLIC_ROUTE_PATHS: frozenset[str]``; the app merges them into
``henchmen.console.app.PUBLIC_PATHS``.

Response contract — HTTP 200 for both outcomes; malformed input is FastAPI's 422::

    {"ok": true,  "step": "<step>", "details": {...}}
    {"ok": false, "step": "<step>", "problems": [{"field": ..., "message": ..., "action": ...}]}

The contract a step route is built from is exactly:

* :class:`StepProblem`, :class:`StepSuccess`, :class:`StepFailure` — the response models.
* :func:`step_succeeded` ``(store, step, details=None)`` — records ``step`` complete and
  builds the success response.
* :func:`step_failed` ``(step, *problems)`` — builds the failure response; records nothing.

There is no ``step_ok``. A step becomes complete only through :func:`step_succeeded`,
which records it server-side (``SetupStateStore.record_step_complete``); clients cannot
write ``completed_steps`` directly.

The response models above are defined exactly once, here (ruling C11). Plans 2B
and 2C import them from this module rather than redefining their own.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from henchmen.console.state import SetupStateStore, SetupStep

STEP_ROUTE_PREFIX = "/console/api/steps"

__all__ = [
    "STEP_ROUTE_PREFIX",
    "StepFailure",
    "StepProblem",
    "StepRoutes",
    "StepSuccess",
    "discover_step_routes",
    "get_setup_store",
    "step_failed",
    "step_succeeded",
    "validate_step_routes",
]


class StepProblem(BaseModel):
    """One plain-language problem with a next action."""

    field: str | None = Field(default=None, description="Input field the problem is about, if any")
    message: str = Field(..., description="What happened and why")
    action: str | None = Field(default=None, description="What the user can do next")


class StepSuccess(BaseModel):
    """A step's validation succeeded."""

    ok: Literal[True] = Field(default=True, description="Always true")
    step: SetupStep = Field(..., description="The validated step")
    details: dict[str, Any] = Field(default_factory=dict, description="Step-specific, non-secret results")


class StepFailure(BaseModel):
    """A step's validation failed (a validation outcome, not a transport error)."""

    ok: Literal[False] = Field(default=False, description="Always false")
    step: SetupStep = Field(..., description="The validated step")
    problems: list[StepProblem] = Field(..., min_length=1, description="Why it failed and what to do")


def get_setup_store(request: Request) -> SetupStateStore:
    """The Console's setup-state store, for use inside a step route."""
    store = getattr(request.app.state, "setup_store", None)
    if not isinstance(store, SetupStateStore):
        raise RuntimeError("the Console app has no setup store")
    return store


def step_succeeded(store: SetupStateStore, step: SetupStep, details: dict[str, Any] | None = None) -> StepSuccess:
    """Record ``step`` as complete and build the success response."""
    store.record_step_complete(step)
    return StepSuccess(step=step, details=details or {})


def step_failed(step: SetupStep, *problems: StepProblem) -> StepFailure:
    """Build the failure response; nothing is recorded. At least one problem is required."""
    if not problems:
        raise ValueError("step_failed needs at least one problem")
    return StepFailure(step=step, problems=list(problems))


@dataclass(frozen=True)
class StepRoutes:
    """One step module's router and the full paths of its session-less routes."""

    router: APIRouter
    public_paths: frozenset[str] = frozenset()


def validate_step_routes(step: SetupStep, routes: StepRoutes) -> None:
    """Fail closed on a malformed :class:`StepRoutes`, discovered or directly injected.

    Both :func:`discover_step_routes` and ``create_console_app`` call this for every
    entry, so a ``step_routes`` mapping passed straight into ``create_console_app``
    (bypassing discovery entirely, as a future caller or a test might) is held to the
    same rules as a real ``henchmen.console.steps.<step>`` module:

    * ``routes.router`` must be an :class:`~fastapi.APIRouter`.
    * It must be declared without a prefix (mounted at its step path instead).
    * Every ``public_paths`` entry must sit under the step's own prefix
      (``/console/api/steps/<step>/...``), must not end in ``/``, and must not
      contain ``..``.
    """
    label = f"{__name__}.{step.value}"
    if not isinstance(routes.router, APIRouter):
        raise TypeError(f"{label} must define `router: APIRouter`")
    if routes.router.prefix:
        raise TypeError(f"{label}.router must be declared without a prefix; it is mounted at its step path")
    own_prefix = f"{STEP_ROUTE_PREFIX}/{step.value}/"
    invalid = sorted(
        path for path in routes.public_paths if not path.startswith(own_prefix) or path.endswith("/") or ".." in path
    )
    if invalid:
        raise ValueError(f"{label}.PUBLIC_ROUTE_PATHS must stay under {own_prefix}: {invalid}")


def discover_step_routes() -> dict[SetupStep, StepRoutes]:
    """Import every ``henchmen.console.steps.<step>`` module that exists; skip the rest.

    Fails closed at app build via :func:`validate_step_routes`: a module without an
    ``APIRouter`` named ``router``, a router declared with a prefix, or a
    ``PUBLIC_ROUTE_PATHS`` entry outside the step's own prefix (or ending in ``/`` or
    containing ``..``) is an error rather than a silently exposed route.

    Only a :class:`ModuleNotFoundError` naming *this exact module* is treated
    as "the step has no module yet" and skipped (2A ships none). A step
    module that exists but fails on one of its own imports raises a
    :class:`ModuleNotFoundError` (or other :class:`ImportError`) naming a
    *different* module -- that propagates rather than being swallowed as a
    missing step.
    """
    discovered: dict[SetupStep, StepRoutes] = {}
    for step in SetupStep:
        module_name = f"{__name__}.{step.value}"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
            continue
        router = getattr(module, "router", None)
        public = frozenset(getattr(module, "PUBLIC_ROUTE_PATHS", frozenset()))
        # validate_step_routes checks router's real type at runtime; the module attribute
        # itself is untyped (it may not even be an APIRouter -- that is exactly what is
        # being checked next).
        routes = StepRoutes(router=router, public_paths=public)  # type: ignore[arg-type]
        validate_step_routes(step, routes)
        discovered[step] = routes
    return discovered
