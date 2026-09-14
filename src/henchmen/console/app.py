"""FastAPI application for the Console.

Phase 1 provides the skeleton every later screen builds on: status, the setup
token exchange, persisted guide state and the apply-and-restart transition.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from henchmen import __version__
from henchmen.console.auth import SESSION_COOKIE, ConsoleAuth, ConsoleGuard
from henchmen.console.state import SetupState, SetupStateStore, SetupStep

_STATIC_DIR = Path(__file__).parent / "static"
_PUBLIC_PATHS = frozenset({"/console/api/status"})


class ConsoleMode(StrEnum):
    """Which runtime mode the Console is serving in."""

    SETUP = "setup"
    RUN = "run"


class ConsoleStatus(BaseModel):
    """Unauthenticated status the launcher and the UI poll."""

    mode: ConsoleMode = Field(..., description="setup or run")
    setup_completed: bool = Field(..., description="Whether setup has been applied")
    version: str = Field(..., description="Henchmen package version")


class SetupStateUpdate(BaseModel):
    """Client-writable part of the setup state. `completed` is set only by apply."""

    model_config = ConfigDict(extra="forbid")

    current_step: SetupStep = Field(..., description="Step the guide should show")
    completed_steps: list[SetupStep] = Field(default_factory=list, description="Steps finished successfully")
    skipped_steps: list[SetupStep] = Field(default_factory=list, description="Optional steps skipped")
    choices: dict[str, str] = Field(default_factory=dict, description="Non-secret selections")


def create_console_app(
    *,
    mode: ConsoleMode,
    store: SetupStateStore,
    auth: ConsoleAuth,
    config_file: Path,
    on_apply: Callable[[], None],
) -> FastAPI:
    """Build the Console app. ``on_apply`` is called after apply's response is sent."""
    app = FastAPI(title="Henchmen Console", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/console/api/status")
    async def status() -> ConsoleStatus:
        return ConsoleStatus(mode=mode, setup_completed=store.load().completed, version=__version__)

    @app.get("/console/session", response_model=None)
    async def session(setup_token: str = Query(default="")) -> RedirectResponse | JSONResponse:
        if not auth.check_setup_token(setup_token):
            return JSONResponse(
                {"detail": "This sign-in link is not valid. Open Henchmen again from the Henchmen app."},
                status_code=403,
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(SESSION_COOKIE, auth.issue_session(), httponly=True, samesite="strict", path="/")
        return response

    @app.get("/console/api/setup/state")
    async def get_state() -> SetupState:
        return store.load()

    @app.put("/console/api/setup/state")
    async def put_state(update: SetupStateUpdate) -> SetupState:
        current = store.load()
        return store.save(current.model_copy(update=update.model_dump()))

    @app.post("/console/api/apply", status_code=202)
    async def apply(background: BackgroundTasks) -> dict[str, bool]:
        state = store.load()
        missing = state.missing_required_steps()
        if missing:
            raise HTTPException(
                status_code=409,
                detail="Finish these steps first: " + ", ".join(step.value for step in missing),
            )
        if not config_file.is_file():
            raise HTTPException(status_code=409, detail="No configuration has been saved yet.")
        store.mark_completed()
        background.add_task(on_apply)
        return {"restarting": True}

    @app.get("/", response_model=None)
    async def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")

    app.add_middleware(ConsoleGuard, auth=auth, public_paths=_PUBLIC_PATHS)
    return app
