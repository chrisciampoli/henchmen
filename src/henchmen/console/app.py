"""FastAPI application for the Console.

Status, the one-time sign-in exchange, persisted guide state, the per-step
validation routers (``henchmen.console.steps``) and the apply-and-restart
transition.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from henchmen import __version__
from henchmen.config.validation import settings_problems
from henchmen.console.auth import SESSION_COOKIE, ConsoleAuth, ConsoleGuard
from henchmen.console.config_store import DISPATCH_API_TOKEN_KEY, ConfigStore
from henchmen.console.state import OPTIONAL_STEPS, SetupState, SetupStateStore, SetupStep
from henchmen.console.steps import STEP_ROUTE_PREFIX, StepRoutes, discover_step_routes, validate_step_routes
from henchmen.utils.redaction import redact

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

#: The single declaration of Console API paths served without a session. Step
#: modules add their own session-less routes only through their
#: ``PUBLIC_ROUTE_PATHS`` (henchmen.console.steps.discover_step_routes), never
#: by widening this set directly.
PUBLIC_PATHS: frozenset[str] = frozenset({"/console/api/status"})

_CHOICE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SECRET_KEY_SEGMENTS = frozenset({"token", "secret", "password", "passwd", "credential", "credentials", "pem"})
_SECRET_KEY_SUFFIXES = ("api_key", "private_key", "signing_key", "access_key")
_MAX_CHOICES = 32
_MAX_CHOICE_VALUE_CHARS = 256


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
    """Client-writable part of the setup state.

    Step completion is recorded only by a step's own validation route
    (``henchmen.console.steps.step_succeeded`` ->
    ``SetupStateStore.record_step_complete``) and ``completed`` only by apply,
    and ``server_choices`` only by the server itself
    (``SetupStateStore.set_server_choices``), so none of those three fields is
    accepted here -- ``extra="forbid"`` turns any of them into a 422 instead
    of being silently dropped.
    """

    model_config = ConfigDict(extra="forbid")

    current_step: SetupStep = Field(..., description="Step the guide should show")
    skipped_steps: list[SetupStep] = Field(default_factory=list, description="Optional steps the user skipped")
    choices: dict[str, str] = Field(default_factory=dict, description="Non-secret selections")

    @field_validator("skipped_steps")
    @classmethod
    def _only_optional_steps_can_be_skipped(cls, steps: list[SetupStep]) -> list[SetupStep]:
        refused = [step.value for step in steps if step not in OPTIONAL_STEPS]
        if refused:
            raise ValueError("these steps cannot be skipped: " + ", ".join(refused))
        return list(dict.fromkeys(steps))

    @field_validator("choices")
    @classmethod
    def _choices_are_not_secrets(cls, choices: dict[str, str]) -> dict[str, str]:
        if len(choices) > _MAX_CHOICES:
            raise ValueError(f"at most {_MAX_CHOICES} choices can be saved")
        for key, value in choices.items():
            if not _CHOICE_KEY.fullmatch(key):
                raise ValueError(f"choice name {key!r} is not allowed")
            if redact(key) != key:
                # The key itself is never echoed here: a key that trips the same secret
                # patterns as a value (e.g. a token pasted into the name by mistake) must
                # not be quoted back in the error text.
                raise ValueError("choice key looks like a secret; secrets are never kept in setup state")
            looks_secret = bool(set(key.split("_")) & _SECRET_KEY_SEGMENTS) or key.endswith(_SECRET_KEY_SUFFIXES)
            if looks_secret or redact(value) != value:
                raise ValueError(f"choice {key!r} looks like a credential; credentials are never kept in setup state")
            if len(value) > _MAX_CHOICE_VALUE_CHARS:
                raise ValueError(f"choice {key!r} is longer than {_MAX_CHOICE_VALUE_CHARS} characters")
        return choices


def create_console_app(
    *,
    mode: ConsoleMode,
    store: SetupStateStore,
    auth: ConsoleAuth,
    config_file: Path,
    on_apply: Callable[[], None],
    step_routes: Mapping[SetupStep, StepRoutes] | None = None,
    seeded_env: Mapping[str, str] | None = None,
) -> FastAPI:
    """Build the Console app. ``on_apply`` is called after apply's response is sent.

    ``step_routes`` defaults to every ``henchmen.console.steps.<step>`` module found
    (:func:`henchmen.console.steps.discover_step_routes`). ``seeded_env`` are the
    defaults ``henchmen serve`` put into this process's environment; apply validates
    as if they were absent unless the file leaves the key out (D-P8).
    """
    app = FastAPI(title="Henchmen Console", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.setup_store = store

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default handler echoes the rejected value back in each error's "input"
        # key, which would leak a credential-shaped choice value straight into the 422
        # body. Every validation error on this app is reported without it.
        errors = [{key: value for key, value in error.items() if key != "input"} for error in exc.errors()]
        return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)

    @app.get("/console/api/status")
    async def status() -> ConsoleStatus:
        return ConsoleStatus(mode=mode, setup_completed=store.load().completed, version=__version__)

    @app.get("/console/session", response_model=None)
    async def session(request: Request, setup_token: str = Query(default="")) -> RedirectResponse | JSONResponse:
        if auth.verify_session(request.cookies.get(SESSION_COOKIE)):
            # Already signed in: an old or reused link must neither fail nor burn the current token.
            return RedirectResponse("/", status_code=303)
        if not auth.consume_setup_token(setup_token):
            return JSONResponse(
                {
                    "detail": (
                        "This sign-in link has expired or was already used. Open Henchmen again from the Henchmen app."
                    )
                },
                status_code=403,
            )
        response = RedirectResponse("/", status_code=303)
        # Max-Age matches what verify_session accepts, so closing the browser does not sign the user out early.
        response.set_cookie(
            SESSION_COOKIE,
            auth.issue_session(),
            max_age=auth.max_age_seconds,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/console/api/setup/state")
    async def get_state() -> SetupState:
        return store.load()

    @app.put("/console/api/setup/state")
    async def put_state(update: SetupStateUpdate) -> SetupState:
        return store.update_client_fields(
            current_step=update.current_step, skipped_steps=update.skipped_steps, choices=update.choices
        )

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
        # Run mode must start with an authenticated task API (D-P10). The token is only
        # computed here, in memory: it is validated as part of the configuration before
        # anything is written, so a refused apply never touches the file (ruling P6).
        config_store = ConfigStore(config_file, config_file.parent / "secrets")
        env_files = (str(config_file),)
        # Skip generating one when the running environment already supplies a usable
        # token (e.g. a Secret Manager mount): the environment outranks the file anyway.
        pending_token = config_store.pending_dispatch_api_token(env_files=env_files, seeded_env=seeded_env)
        overrides = {DISPATCH_API_TOKEN_KEY: pending_token} if pending_token is not None else None
        _settings, problems = settings_problems(env_files, seeded_env=seeded_env, overrides=overrides)
        if problems:
            # Marking setup complete would restart into a run mode that cannot start,
            # and setup mode would no longer be offered to fix it.
            raise HTTPException(
                status_code=409,
                detail={"message": "The saved configuration cannot start Henchmen.", "problems": problems},
            )
        if pending_token is not None:
            try:
                config_store.write_dispatch_api_token(pending_token)
            except OSError:
                # Never mark setup complete or restart into a run mode that still has no
                # usable token; the exception itself (path, errno) carries no secret.
                logger.exception("Could not write the Dispatch API token to %s", config_file)
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Could not save the Dispatch API token to the configuration file. "
                        "Check permissions and free space on the data volume."
                    ),
                ) from None
        store.mark_completed()
        background.add_task(on_apply)
        return {"restarting": True}

    steps = discover_step_routes() if step_routes is None else step_routes
    public_paths = set(PUBLIC_PATHS)
    for step, routes in steps.items():
        # Discovery already validates its own findings; an explicitly injected
        # `step_routes` (a future direct caller, or a test) is held to the same rules.
        validate_step_routes(step, routes)
        app.include_router(routes.router, prefix=f"{STEP_ROUTE_PREFIX}/{step.value}")
        public_paths |= routes.public_paths

    @app.get("/", response_model=None)
    async def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")

    app.add_middleware(ConsoleGuard, auth=auth, public_paths=frozenset(public_paths))
    return app
