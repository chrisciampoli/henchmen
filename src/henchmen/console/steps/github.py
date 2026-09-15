"""Console step 2: connect GitHub through a GitHub App created from a manifest (spec §5.1, D-P11).

The browser POSTs the manifest to github.com; GitHub creates the App and sends
the browser back to ``manifest-callback`` with a one-time ``code``. That
callback, and ``installed`` after the user installs the App (Task 6), are
*public* routes (declared in ``PUBLIC_ROUTE_PATHS``): the SameSite=Strict
cookie is not sent on a cross-site redirect, so each is authorised by a
single-use, expiring, purpose-bound ``state`` that a session-authenticated
route issued (spec §7 as amended; :mod:`henchmen.console.callback_state`).

Rules every public callback here follows:

* The state is consumed (and burned) before anything else happens. A missing,
  unknown, expired, replayed or wrong-purpose state never calls GitHub and
  never touches the configuration.
* The answer is always a 303 -- never JSON and never a body with data: to a
  fixed Console path (``/?step=github&github_error=<reason>``), or to the
  App's install page on the configured GitHub web URL for a slug GitHub itself
  returned and :func:`github_app.is_valid_slug` accepted. No query parameter
  ever chooses the redirect target.
* No secret appears in a redirect, a response or a log line. Responses carry
  ``Cache-Control: no-store`` and ``Referrer-Policy: no-referrer``.

Stored by ``manifest-callback``: the private key in ``secrets/github-app.pem``
(``ConfigStore.write_secret_file``: 0600, atomic) with its path in
``github_app_private_key_path``; ``github_app_id`` and the webhook secret in
``github_webhook_secret``, set in the same atomic ``ConfigStore.update`` that
removes ``github_app_installation_id`` (an installation belongs to one App).
The App slug and owner go to ``server_choices``. The step completes only in
Task 6, once a default repository the installation can push to is chosen.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from henchmen.console import github_app
from henchmen.console.callback_state import CallbackStateStore
from henchmen.console.config_store import CONFIGURED, ConfigStore, ConfigStoreError
from henchmen.console.deps import HttpClientFactory, get_callback_states, get_config_store, get_http_client_factory
from henchmen.console.state import SetupStateStore, SetupStep
from henchmen.console.steps import StepFailure, StepProblem, StepSuccess, get_setup_store, step_failed

logger = logging.getLogger(__name__)
router = APIRouter()
STEP = SetupStep.GITHUB
CONFIG_SECTION = "GitHub"

APP_ID_KEY = "HENCHMEN_GITHUB_APP_ID"
INSTALLATION_ID_KEY = "HENCHMEN_GITHUB_APP_INSTALLATION_ID"
PRIVATE_KEY_PATH_KEY = "HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH"
WEBHOOK_SECRET_KEY = "HENCHMEN_GITHUB_WEBHOOK_SECRET"
DEFAULT_REPO_KEY = "HENCHMEN_GITHUB_DEFAULT_REPO"
DEFAULT_ORG_KEY = "HENCHMEN_GITHUB_DEFAULT_ORG"
SLUG_CHOICE = "github_app_slug"
ACCOUNT_CHOICE = "github_account"
PUBLIC_ROUTE_PATHS: frozenset[str] = frozenset({github_app.MANIFEST_CALLBACK_PATH, github_app.INSTALLED_CALLBACK_PATH})

# `github_error` reasons a callback can send the browser back with (fixed values only).
ERROR_EXPIRED = "expired"
ERROR_CONVERSION = "conversion"
ERROR_STORAGE = "storage"

_NO_STORE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

ConfigDep = Annotated[ConfigStore, Depends(get_config_store)]
SetupDep = Annotated[SetupStateStore, Depends(get_setup_store)]
StatesDep = Annotated[CallbackStateStore, Depends(get_callback_states)]
HttpDep = Annotated[HttpClientFactory, Depends(get_http_client_factory)]


class ManifestRequest(BaseModel):
    """Where the user wants the GitHub App created."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    account_type: Literal["personal", "organization"] = Field(default="personal", description="Account kind")
    organization: str | None = Field(
        default=None,
        max_length=39,
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$",
        description="Organisation login when account_type is organization",
    )
    machine_name: str = Field(default="", max_length=64, description="Label for the App name")


def back_to_console(**params: str) -> RedirectResponse:
    """303 to the Console page with the GitHub step's outcome in the query string.

    The path is always ``/``; callers pass only fixed, non-secret values.
    """
    query = urlencode({"step": STEP.value, **params})
    return RedirectResponse(f"/?{query}", status_code=303, headers=_NO_STORE_HEADERS)


def app_created(config: ConfigStore) -> bool:
    """True when an App ID is saved and its private key file exists."""
    key_path = config.get(PRIVATE_KEY_PATH_KEY).strip()
    return config.is_set(APP_ID_KEY) and bool(key_path) and Path(key_path).is_file()


@router.get("")
async def status(config: ConfigDep, setup: SetupDep) -> StepSuccess:
    """Where the GitHub connection stands (no secrets)."""
    state = setup.load()
    created = app_created(config)
    return StepSuccess(
        step=STEP,
        details={
            "app_created": created,
            "app_slug": state.server_choices.get(SLUG_CHOICE, ""),
            "account": state.server_choices.get(ACCOUNT_CHOICE, ""),
            "installed": config.is_set(INSTALLATION_ID_KEY),
            "default_repo": config.get(DEFAULT_REPO_KEY),
            "private_key": CONFIGURED if created else "",
            "completed": STEP in state.completed_steps,
        },
    )


@router.post("/manifest")
async def create_manifest(body: ManifestRequest, request: Request, states: StatesDep) -> StepSuccess | StepFailure:
    """The manifest, the github.com form action and the state the browser form carries."""
    if body.account_type == "organization" and not body.organization:
        return step_failed(
            STEP,
            StepProblem(
                field="organization",
                message="Enter the GitHub organisation that owns your repositories.",
                action="Use the name in the organisation's address: github.com/acme is acme.",
            ),
        )
    organization = body.organization if body.account_type == "organization" else None
    endpoints = github_app.github_endpoints()
    manifest = github_app.build_manifest(
        console_base_url=str(request.base_url),
        machine_name=body.machine_name or github_app.default_machine_name(),
    )
    try:
        state = states.issue(
            github_app.MANIFEST_PURPOSE, {"account_type": body.account_type, "organization": organization or ""}
        )
    except OSError as exc:
        logger.warning("Could not save a GitHub callback state (%s)", type(exc).__name__)
        return step_failed(
            STEP,
            StepProblem(
                message="Henchmen could not save the information it needs to finish connecting GitHub.",
                action="Check that the Henchmen data folder is writable and has free space, then try again.",
            ),
        )
    details: dict[str, Any] = {
        "form_action": github_app.manifest_form_action(endpoints.web_url, organization, state),
        "manifest": manifest,
        "manifest_json": json.dumps(manifest),
        "state": state,
    }
    if organization:
        details["admin_request_message"] = github_app.org_admin_request(endpoints.web_url, organization)
    return StepSuccess(step=STEP, details=details)


@router.get("/manifest-callback", include_in_schema=False)
async def manifest_callback(
    config: ConfigDep,
    setup: SetupDep,
    states: StatesDep,
    http: HttpDep,
    code: str = "",
    state: str = "",
) -> RedirectResponse:
    """Public: verify state, convert the code into an App, store it and send the browser to install it."""
    if states.consume(github_app.MANIFEST_PURPOSE, state) is None:
        logger.warning("Refused a GitHub App manifest callback without a valid state")
        return back_to_console(github_error=ERROR_EXPIRED)
    endpoints = github_app.github_endpoints()
    try:
        async with http() as client:
            conversion = await github_app.convert_manifest(client, endpoints.api_url, code)
    except github_app.GitHubAppApiError as exc:
        # The message is built to be safe to log (GitHub's text is redacted and truncated).
        logger.warning("GitHub App manifest conversion failed: %s", exc)
        return back_to_console(github_error=ERROR_CONVERSION)

    values = {APP_ID_KEY: conversion.app_id}
    if conversion.webhook_secret:
        values[WEBHOOK_SECRET_KEY] = conversion.webhook_secret
    try:
        # Held across both writes so no other Console write lands between the key file
        # and the configuration that points at it (never `await` inside it).
        with config.locked():
            key_path = config.write_secret_file(github_app.PRIVATE_KEY_FILE_NAME, conversion.pem.encode("utf-8"))
            values[PRIVATE_KEY_PATH_KEY] = str(key_path)
            # An installation belongs to one App: a new App must be installed again (ruling PM-7:
            # set and remove in one atomic write).
            config.update(values, section=CONFIG_SECTION, unset=[INSTALLATION_ID_KEY])
        setup.set_server_choices({SLUG_CHOICE: conversion.slug, ACCOUNT_CHOICE: conversion.owner_login})
        install_state = states.issue(github_app.INSTALL_PURPOSE, {"slug": conversion.slug})
    except (OSError, ConfigStoreError, ValueError) as exc:
        logger.warning("Could not save the new GitHub App %s (%s)", conversion.slug, type(exc).__name__)
        return back_to_console(github_error=ERROR_STORAGE)
    logger.info("GitHub App %s created; sending the browser to install it", conversion.slug)
    return RedirectResponse(
        github_app.installation_url(endpoints.web_url, conversion.slug, install_state),
        status_code=303,
        headers=_NO_STORE_HEADERS,
    )
