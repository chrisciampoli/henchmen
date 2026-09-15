"""Console step 2: connect GitHub through a GitHub App created from a manifest (spec §5.1, D-P11).

The browser POSTs the manifest to github.com; GitHub creates the App and sends
the browser back to ``manifest-callback`` with a one-time ``code``. That
callback, and ``installed`` after the user installs the App (Task 6), are
*public* routes (declared in ``PUBLIC_ROUTE_PATHS``): the SameSite=Strict
cookie is not sent on a cross-site redirect, so each is authorised by a
single-use, expiring, purpose-bound ``state`` that a session-authenticated
route issued (spec §7 as amended; :mod:`henchmen.console.callback_state`).
Single use is guaranteed within this process; ``henchmen serve`` is one process.

Rules every public callback here follows:

* The state is consumed (and burned) before anything else happens. A missing,
  unknown, expired, replayed or wrong-purpose state never calls GitHub and
  never touches the configuration.
* The answer is always a 303 -- never JSON and never a body with data: to a
  fixed Console path (``/?step=github&github_error=<reason>``), or to the
  App's install page on the state-bound GitHub web URL for a slug GitHub itself
  returned and :func:`github_app.is_valid_slug` accepted. No query parameter
  ever chooses the redirect target.
* No secret appears in a redirect, a response or a log line. Responses carry
  ``Cache-Control: no-store`` and ``Referrer-Policy: no-referrer``.

GitHub hosts: ``POST /manifest`` resolves ``github_api_url``/``github_web_url``
fresh from the configuration (:func:`henchmen.utils.endpoints.resolve_github_endpoints`;
an invalid value fails the step and never falls back) and binds them into the
state's data. The callback uses those state-bound URLs and binds them again
into the install state, so every hop of one connection uses the same hosts even
if the configuration changes in between.

``manifest-callback`` stores, in this order:

1. the install state (if it cannot be saved, nothing is written);
2. the private key in ``secrets/github-app-<app id>.pem``
   (``ConfigStore.write_secret_file``: 0600, atomic). A reconnect never
   overwrites the key the running process still signs with; key files no
   configuration references are removed at apply
   (:func:`github_app.remove_unreferenced_app_keys`);
3. one atomic ``ConfigStore.update`` setting ``github_app_id``,
   ``github_app_private_key_path`` and ``github_webhook_secret`` and removing
   ``github_app_installation_id`` (an installation belongs to one App) and, when
   the new App has none, the old webhook secret. If this write fails, the new
   key file is deleted again;
4. the App slug and owner in ``server_choices`` -- display values only, so a
   failure is logged and the browser still goes on to install the App.

The step completes only in Task 6, once a default repository the installation
can push to is chosen.
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

from henchmen.config.settings import require_secure_github_url
from henchmen.console import github_app
from henchmen.console.callback_state import CallbackStateStore
from henchmen.console.config_store import CONFIGURED, ConfigStore, ConfigStoreError
from henchmen.console.deps import (
    HttpClientFactory,
    get_callback_states,
    get_config_store,
    get_http_client_factory,
    get_seeded_env,
)
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
ERROR_CONFIGURATION = "configuration"

_NO_STORE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

ConfigDep = Annotated[ConfigStore, Depends(get_config_store)]
SetupDep = Annotated[SetupStateStore, Depends(get_setup_store)]
StatesDep = Annotated[CallbackStateStore, Depends(get_callback_states)]
HttpDep = Annotated[HttpClientFactory, Depends(get_http_client_factory)]
SeededEnvDep = Annotated[dict[str, str], Depends(get_seeded_env)]


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


def state_endpoints(data: dict[str, str]) -> github_app.GitHubEndpoints | None:
    """The GitHub hosts bound into a callback state's data; ``None`` when absent or unusable."""
    api_url = data.get("api_url", "")
    web_url = data.get("web_url", "")
    if not api_url or not web_url:
        return None
    try:
        return github_app.GitHubEndpoints(
            api_url=require_secure_github_url(api_url).rstrip("/"),
            web_url=require_secure_github_url(web_url).rstrip("/"),
        )
    except ValueError:
        return None


def endpoint_problem(exc: github_app.EndpointError) -> StepProblem:
    """A step problem for an unusable GitHub URL setting (the value itself is never repeated)."""
    return StepProblem(
        field=exc.field,
        message=f"The {exc.field} setting cannot be used: it {exc.reason}.",
        action=(
            f"Fix or remove HENCHMEN_{exc.field.upper()} in the Henchmen configuration file or environment, "
            "then try again. Leave it unset to use github.com."
        ),
    )


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
async def create_manifest(
    body: ManifestRequest, request: Request, config: ConfigDep, states: StatesDep, seeded_env: SeededEnvDep
) -> StepSuccess | StepFailure:
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
    try:
        endpoints = github_app.github_endpoints(config.config_file, seeded_env=seeded_env)
    except github_app.EndpointError as exc:
        logger.warning("Refused to start the GitHub App manifest flow: %s", exc)
        return step_failed(STEP, endpoint_problem(exc))
    manifest = github_app.build_manifest(
        console_base_url=str(request.base_url),
        machine_name=body.machine_name or github_app.default_machine_name(),
    )
    try:
        state = states.issue(
            github_app.MANIFEST_PURPOSE,
            {
                "account_type": body.account_type,
                "organization": organization or "",
                "api_url": endpoints.api_url,
                "web_url": endpoints.web_url,
            },
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


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove the unused GitHub App key %s (%s)", path.name, type(exc).__name__)


def _same_file(first: str, second: Path) -> bool:
    if not first:
        return False
    try:
        return Path(first).resolve() == second.resolve()
    except OSError:
        return False


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
    data = states.consume(github_app.MANIFEST_PURPOSE, state)
    if data is None:
        logger.warning("Refused a GitHub App manifest callback without a valid state")
        return back_to_console(github_error=ERROR_EXPIRED)
    endpoints = state_endpoints(data)
    if endpoints is None:
        logger.warning("Refused a GitHub App manifest callback whose state carries no usable GitHub URLs")
        return back_to_console(github_error=ERROR_CONFIGURATION)
    try:
        async with http() as client:
            conversion = await github_app.convert_manifest(client, endpoints.api_url, code)
    except github_app.GitHubAppApiError as exc:
        # The message is built to be safe to log (GitHub's text is redacted and truncated).
        logger.warning("GitHub App manifest conversion failed: %s", exc)
        return back_to_console(github_error=ERROR_CONVERSION)

    # 1. The install state first: when it cannot be saved, nothing has been written yet.
    try:
        install_state = states.issue(
            github_app.INSTALL_PURPOSE,
            {"slug": conversion.slug, "api_url": endpoints.api_url, "web_url": endpoints.web_url},
        )
    except OSError as exc:
        logger.warning("Could not save the install state for GitHub App %s (%s)", conversion.slug, type(exc).__name__)
        return back_to_console(github_error=ERROR_STORAGE)

    values = {APP_ID_KEY: conversion.app_id}
    unset = [INSTALLATION_ID_KEY]
    if conversion.webhook_secret:
        values[WEBHOOK_SECRET_KEY] = conversion.webhook_secret
    else:
        # The old secret belonged to the old App; never leave it paired with the new one.
        unset.append(WEBHOOK_SECRET_KEY)
    # Held across the key file and the configuration that points at it, so no other
    # Console write lands in between (never `await` inside it).
    with config.locked():
        previous_key = config.get(PRIVATE_KEY_PATH_KEY).strip()
        try:
            # 2. The new App's own key file, beside -- never over -- the key currently in use.
            key_path = config.write_secret_file(
                github_app.private_key_file_name(conversion.app_id), conversion.pem.encode("utf-8")
            )
        except (OSError, ValueError) as exc:
            logger.warning("Could not save the key of GitHub App %s (%s)", conversion.slug, type(exc).__name__)
            states.consume(github_app.INSTALL_PURPOSE, install_state)
            return back_to_console(github_error=ERROR_STORAGE)
        values[PRIVATE_KEY_PATH_KEY] = str(key_path)
        try:
            # 3. One atomic write (ruling PM-7).
            config.update(values, section=CONFIG_SECTION, unset=unset)
        except (OSError, ConfigStoreError, ValueError) as exc:
            logger.warning("Could not save GitHub App %s (%s)", conversion.slug, type(exc).__name__)
            if not _same_file(previous_key, key_path):
                _remove_quietly(key_path)
            states.consume(github_app.INSTALL_PURPOSE, install_state)
            return back_to_console(github_error=ERROR_STORAGE)
    # 4. Display-only values: the App is stored, so a failure here must not strand the user.
    try:
        setup.set_server_choices({SLUG_CHOICE: conversion.slug, ACCOUNT_CHOICE: conversion.owner_login})
    except (OSError, ValueError) as exc:
        logger.warning("Could not record the name of GitHub App %s (%s)", conversion.slug, type(exc).__name__)
    logger.info("GitHub App %s created; sending the browser to install it", conversion.slug)
    return RedirectResponse(
        github_app.installation_url(endpoints.web_url, conversion.slug, install_state),
        status_code=303,
        headers=_NO_STORE_HEADERS,
    )
