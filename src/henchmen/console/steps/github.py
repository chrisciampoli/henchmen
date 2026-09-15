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
   configuration references are removed at the next run-mode start, before
   any service is built (:func:`github_app.remove_unused_app_keys_at_startup`);
3. one atomic ``ConfigStore.update`` setting ``github_app_id``,
   ``github_app_private_key_path`` and ``github_webhook_secret`` and removing
   ``github_app_installation_id`` (an installation belongs to one App) and, when
   the new App has none, the old webhook secret. If this write fails, the new
   key file is deleted again;
4. the App slug and owner in ``server_choices`` -- display values only, so a
   failure is logged and the browser still goes on to install the App.

``installed`` (GitHub's return from the install page) consumes an
``INSTALL_PURPOSE`` state and uses only the GitHub URLs bound into it. It never
trusts the ``installation_id`` query parameter: it reads
``/app/installations/{id}`` with this App's JWT and requires GitHub's answer to
name this App (``app_id``/``app_slug``) before one atomic ``ConfigStore.update``
saves the id. ``setup_action=request`` (an organisation owner must approve)
saves nothing and sends the UI to its waiting state.

The session routes resolve the GitHub URLs fresh on every call:

* ``POST /installation/link`` issues a new install URL with a fresh state for a
  lost or closed install tab (ruling C9);
* ``POST /installation/check`` ("Check again") finds this App's installation
  with the app JWT;
* ``GET /repositories`` lists what the installation can access, with an
  installation token minted for the listing (pages bounded by
  ``github_app.MAX_REPO_PAGES``);
* ``POST /repository`` checks the installation still belongs to this App and
  may write contents and pull requests, validates the chosen repository
  against GitHub's list (never the client's spelling), saves it with the App's
  bot account as the git author and only then completes the step
  (``step_succeeded``).
"""

from __future__ import annotations

import json
import logging
import re
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
from henchmen.console.steps import (
    StepFailure,
    StepProblem,
    StepSuccess,
    get_setup_store,
    step_failed,
    step_succeeded,
)
from henchmen.utils.github_auth import GitHubAppConfig, GitHubAuthError, GitHubCredentialsProvider, app_jwt_for
from henchmen.utils.redaction import redact

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
GIT_AUTHOR_NAME_KEY = "HENCHMEN_GIT_AUTHOR_NAME"
GIT_AUTHOR_EMAIL_KEY = "HENCHMEN_GIT_AUTHOR_EMAIL"
SLUG_CHOICE = "github_app_slug"
ACCOUNT_CHOICE = "github_account"
PUBLIC_ROUTE_PATHS: frozenset[str] = frozenset({github_app.MANIFEST_CALLBACK_PATH, github_app.INSTALLED_CALLBACK_PATH})

# `github_error` reasons a callback can send the browser back with (fixed values only).
ERROR_EXPIRED = "expired"
ERROR_CONVERSION = "conversion"
ERROR_STORAGE = "storage"
ERROR_CONFIGURATION = "configuration"
ERROR_INSTALLATION = "installation"

_INSTALLATION_ID_RE = re.compile(r"[0-9]{1,20}")
_PERMISSION_WORDS: dict[str, str] = {"contents": "push code", "pull_requests": "open pull requests"}

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


# -- installation and repository choice ------------------------------------------


class RepositoryChoice(BaseModel):
    """The repository Henchmen works on by default."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo: str = Field(..., max_length=200, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", description="owner/name")


def _app_jwt(config: ConfigStore) -> str:
    """App JWT from the saved App ID and key file; raises GitHubAuthError when the App is not usable.

    The key is read only through :func:`henchmen.utils.github_auth.app_jwt_for` (ruling PI-14).
    """
    app_id = config.get(APP_ID_KEY).strip()
    key_path = config.get(PRIVATE_KEY_PATH_KEY).strip()
    if not app_id or not key_path:
        raise GitHubAuthError("The GitHub App has not been created yet.")
    return app_jwt_for(app_id, Path(key_path))


def _not_created_problem() -> StepProblem:
    return StepProblem(
        message="The GitHub App has not been created yet.",
        action="Choose Create GitHub App to connect GitHub.",
    )


def _storage_problem() -> StepProblem:
    return StepProblem(
        message="Henchmen could not save the GitHub connection.",
        action="Check that the Henchmen data folder is writable and has free space, then try again.",
    )


def _credentials_problem(exc: GitHubAuthError) -> StepProblem:
    return StepProblem(
        message=redact(str(exc)),
        action=(
            "Finish installing Henchmen on GitHub, then choose Check again. If this keeps happening, "
            "choose Create GitHub App to start over."
        ),
    )


def _api_problem(exc: github_app.GitHubAppApiError) -> StepProblem:
    return StepProblem(
        message=f"GitHub did not answer as expected: {redact(str(exc))}",
        action="Check your internet connection and choose Check again.",
    )


def _install_request(web_url: str, slug: str) -> str:
    valid = github_app.is_valid_slug(slug)
    link = f"{web_url}/apps/{slug}/installations/new" if valid else f"{web_url}/settings/apps"
    return (
        "Install Henchmen on the account that owns your repositories. If your organisation needs an owner to "
        f'approve apps, copy this request to them: "Please approve the Henchmen GitHub App at {link} so it can '
        'open pull requests on our repositories." Then choose Check again.'
    )


def _pick_installation(
    installations: list[github_app.Installation], configured_id: str, account: str
) -> github_app.Installation | None:
    """The configured installation, else the only one on ``account``, else the only one at all."""
    for installation in installations:
        if configured_id and installation.installation_id == configured_id:
            return installation
    if account:
        matches = [item for item in installations if item.account_login.lower() == account.lower()]
        if len(matches) == 1:
            return matches[0]
    return installations[0] if len(installations) == 1 else None


def _record_account(setup: SetupStateStore, installation: github_app.Installation) -> None:
    """Display-only: the account the App is installed on. A failure is logged, never fatal."""
    if not installation.account_login:
        return
    try:
        setup.set_server_choices({ACCOUNT_CHOICE: installation.account_login})
    except (OSError, ValueError) as exc:
        logger.warning("Could not record the GitHub account name (%s)", type(exc).__name__)


def _installation_context(
    config: ConfigStore, http: HttpClientFactory, seeded_env: dict[str, str]
) -> tuple[github_app.GitHubEndpoints, GitHubCredentialsProvider] | StepFailure:
    """Endpoints and a credentials provider for the saved installation, or the failure to return.

    A provider of its own rather than the process-wide one: setup mode has no
    ``Settings``, and the URLs must be what the configuration says now.
    """
    if not app_created(config):
        return step_failed(STEP, _not_created_problem())
    app = GitHubAppConfig.from_values(
        config.get(APP_ID_KEY), config.get(PRIVATE_KEY_PATH_KEY), config.get(INSTALLATION_ID_KEY)
    )
    if app is None:
        return step_failed(
            STEP,
            StepProblem(
                message="Install the GitHub App before choosing a repository.",
                action="Choose Install on GitHub, then choose Check again.",
            ),
        )
    try:
        endpoints = github_app.github_endpoints(config.config_file, seeded_env=seeded_env)
    except github_app.EndpointError as exc:
        return step_failed(STEP, endpoint_problem(exc))
    try:
        provider = GitHubCredentialsProvider(app=app, api_url=endpoints.api_url, async_client_factory=http)
    except GitHubAuthError as exc:
        return step_failed(STEP, _credentials_problem(exc))
    return endpoints, provider


@router.get("/installed", include_in_schema=False)
async def installed(
    config: ConfigDep,
    setup: SetupDep,
    states: StatesDep,
    http: HttpDep,
    installation_id: str = "",
    setup_action: str = "",
    state: str = "",
) -> RedirectResponse:
    """Public: GitHub's return from the install page; verify the installation is this App's, then save it."""
    data = states.consume(github_app.INSTALL_PURPOSE, state)
    if data is None:
        logger.warning("Refused a GitHub App installation callback without a valid state")
        return back_to_console(github_error=ERROR_EXPIRED)
    if setup_action == "request":
        # An organisation owner must approve; nothing is installed yet, so nothing is saved.
        logger.info("GitHub App installation requested; waiting for an owner to approve it")
        return back_to_console(github="requested")
    endpoints = state_endpoints(data)
    if endpoints is None:
        logger.warning("Refused a GitHub App installation callback whose state carries no usable GitHub URLs")
        return back_to_console(github_error=ERROR_CONFIGURATION)
    slug = data.get("slug", "")
    if not _INSTALLATION_ID_RE.fullmatch(installation_id) or not github_app.is_valid_slug(slug):
        logger.warning("Refused a GitHub App installation callback with a malformed installation")
        return back_to_console(github_error=ERROR_INSTALLATION)
    app_id = config.get(APP_ID_KEY).strip()
    try:
        app_jwt = _app_jwt(config)
        async with http() as client:
            installation = await github_app.get_installation(client, endpoints.api_url, app_jwt, installation_id)
    except (GitHubAuthError, github_app.GitHubAppApiError) as exc:
        logger.warning("Could not verify GitHub App installation %s: %s", installation_id, redact(str(exc)))
        return back_to_console(github_error=ERROR_INSTALLATION)
    if (
        installation is None
        or installation.installation_id != installation_id
        or not installation.belongs_to(app_id=app_id, slug=slug)
    ):
        logger.warning("Refused GitHub installation %s: GitHub does not list it as this App's", installation_id)
        return back_to_console(github_error=ERROR_INSTALLATION)
    try:
        config.update({INSTALLATION_ID_KEY: installation.installation_id}, section=CONFIG_SECTION)
    except (OSError, ConfigStoreError, ValueError) as exc:
        logger.warning("Could not save GitHub App installation %s (%s)", installation_id, type(exc).__name__)
        return back_to_console(github_error=ERROR_STORAGE)
    _record_account(setup, installation)
    logger.info("GitHub App %s installed (installation %s)", slug, installation_id)
    return back_to_console(github="installed")


@router.post("/installation/link")
async def installation_link(
    config: ConfigDep, setup: SetupDep, states: StatesDep, seeded_env: SeededEnvDep
) -> StepSuccess | StepFailure:
    """A fresh install URL (new single-use state) for the saved App, for a lost or closed install tab (C9)."""
    slug = setup.load().server_choices.get(SLUG_CHOICE, "")
    if not app_created(config) or not github_app.is_valid_slug(slug):
        return step_failed(STEP, _not_created_problem())
    try:
        endpoints = github_app.github_endpoints(config.config_file, seeded_env=seeded_env)
    except github_app.EndpointError as exc:
        logger.warning("Refused to issue a GitHub App install link: %s", exc)
        return step_failed(STEP, endpoint_problem(exc))
    try:
        state = states.issue(
            github_app.INSTALL_PURPOSE, {"slug": slug, "api_url": endpoints.api_url, "web_url": endpoints.web_url}
        )
    except OSError as exc:
        logger.warning("Could not save a GitHub install state (%s)", type(exc).__name__)
        return step_failed(STEP, _storage_problem())
    return StepSuccess(step=STEP, details={"install_url": github_app.installation_url(endpoints.web_url, slug, state)})


@router.post("/installation/check")
async def check_installation(
    config: ConfigDep, setup: SetupDep, http: HttpDep, seeded_env: SeededEnvDep
) -> StepSuccess | StepFailure:
    """Check again: find the App's installation after an approval or an install started on github.com."""
    if not app_created(config):
        return step_failed(STEP, _not_created_problem())
    try:
        endpoints = github_app.github_endpoints(config.config_file, seeded_env=seeded_env)
    except github_app.EndpointError as exc:
        return step_failed(STEP, endpoint_problem(exc))
    try:
        app_jwt = _app_jwt(config)
        async with http() as client:
            installations = await github_app.list_installations(client, endpoints.api_url, app_jwt)
    except GitHubAuthError as exc:
        return step_failed(STEP, _credentials_problem(exc))
    except github_app.GitHubAppApiError as exc:
        return step_failed(STEP, _api_problem(exc))
    choices = setup.load().server_choices
    slug = choices.get(SLUG_CHOICE, "")
    app_id = config.get(APP_ID_KEY).strip()
    ours = [
        item
        for item in installations
        if item.belongs_to(app_id=app_id, slug=slug) and _INSTALLATION_ID_RE.fullmatch(item.installation_id)
    ]
    chosen = _pick_installation(ours, config.get(INSTALLATION_ID_KEY).strip(), choices.get(ACCOUNT_CHOICE, ""))
    if chosen is None:
        if ours:
            message = (
                "Henchmen is installed on more than one account; "
                "install it only on the one that owns your repositories."
            )
        else:
            message = "Henchmen is not installed on your GitHub account yet."
        return step_failed(STEP, StepProblem(message=message, action=_install_request(endpoints.web_url, slug)))
    try:
        config.update({INSTALLATION_ID_KEY: chosen.installation_id}, section=CONFIG_SECTION)
    except (OSError, ConfigStoreError, ValueError) as exc:
        logger.warning("Could not save GitHub App installation %s (%s)", chosen.installation_id, type(exc).__name__)
        return step_failed(STEP, _storage_problem())
    _record_account(setup, chosen)
    return StepSuccess(step=STEP, details={"installation_id": chosen.installation_id, "account": chosen.account_login})


@router.get("/repositories")
async def repositories(
    config: ConfigDep, setup: SetupDep, http: HttpDep, seeded_env: SeededEnvDep
) -> StepSuccess | StepFailure:
    """Repositories the installation can access, listed with an installation token (never returned)."""
    context = _installation_context(config, http, seeded_env)
    if isinstance(context, StepFailure):
        return context
    endpoints, provider = context
    try:
        token = await provider.token_async()
        async with http() as client:
            found = await github_app.list_installation_repositories(client, endpoints.api_url, token)
    except GitHubAuthError as exc:
        return step_failed(STEP, _credentials_problem(exc))
    except github_app.GitHubAppApiError as exc:
        return step_failed(STEP, _api_problem(exc))
    return StepSuccess(
        step=STEP,
        details={
            "repositories": [repository.model_dump() for repository in found],
            "account": setup.load().server_choices.get(ACCOUNT_CHOICE, ""),
        },
    )


@router.post("/repository")
async def choose_repository(
    body: RepositoryChoice, config: ConfigDep, setup: SetupDep, http: HttpDep, seeded_env: SeededEnvDep
) -> StepSuccess | StepFailure:
    """Verify Henchmen can push to and open PRs on ``repo``, save it as the default and complete the step."""
    context = _installation_context(config, http, seeded_env)
    if isinstance(context, StepFailure):
        return context
    endpoints, provider = context
    app_id = config.get(APP_ID_KEY).strip()
    installation_id = config.get(INSTALLATION_ID_KEY).strip()
    known_slug = setup.load().server_choices.get(SLUG_CHOICE, "")
    try:
        app_jwt = provider.app_jwt()
        async with http() as client:
            installation = await github_app.get_installation(client, endpoints.api_url, app_jwt, installation_id)
    except GitHubAuthError as exc:
        return step_failed(STEP, _credentials_problem(exc))
    except github_app.GitHubAppApiError as exc:
        return step_failed(STEP, _api_problem(exc))
    if installation is None or not installation.belongs_to(app_id=app_id, slug=known_slug):
        return step_failed(
            STEP,
            StepProblem(
                message="The Henchmen app is no longer installed on GitHub.",
                action=_install_request(endpoints.web_url, known_slug),
            ),
        )
    settings_url = github_app.installation_settings_url(endpoints.web_url, installation)
    missing = installation.missing_write_permissions()
    if missing:
        abilities = " or ".join(_PERMISSION_WORDS.get(name, name) for name in missing)
        return step_failed(
            STEP,
            StepProblem(
                message=f"The Henchmen app is not allowed to {abilities}.",
                action=(
                    f"An owner of {installation.account_login} can accept the app's requested permissions at "
                    f"{settings_url}. Then choose Check again."
                ),
            ),
        )

    slug = author_name = author_email = ""
    try:
        token = await provider.token_async()
        async with http() as client:
            found = await github_app.list_installation_repositories(client, endpoints.api_url, token)
            # Validated against GitHub's list, and saved in GitHub's spelling -- never the client's.
            match = next((item for item in found if item.full_name.lower() == body.repo.lower()), None)
            if match is not None:
                if github_app.is_valid_slug(installation.app_slug):
                    slug = installation.app_slug
                elif github_app.is_valid_slug(known_slug):
                    slug = known_slug
                else:
                    slug = await github_app.get_app_slug(client, endpoints.api_url, app_jwt)
                author_name, author_email = await github_app.bot_identity(client, endpoints.api_url, slug)
    except GitHubAuthError as exc:
        return step_failed(STEP, _credentials_problem(exc))
    except github_app.GitHubAppApiError as exc:
        return step_failed(STEP, _api_problem(exc))
    if match is None:
        return step_failed(
            STEP,
            StepProblem(
                field="repo",
                message=f"Henchmen can't see {body.repo}.",
                action=(
                    f"Add the repository to the Henchmen app's repository access at {settings_url}, "
                    "then choose Check again."
                ),
            ),
        )

    details = {"default_repo": match.full_name, "default_branch": match.default_branch, "git_author": author_name}
    try:
        config.update(
            {
                DEFAULT_REPO_KEY: match.full_name,
                DEFAULT_ORG_KEY: match.full_name.split("/", 1)[0],
                GIT_AUTHOR_NAME_KEY: author_name,
                GIT_AUTHOR_EMAIL_KEY: author_email,
            },
            section=CONFIG_SECTION,
        )
        setup.set_server_choices({SLUG_CHOICE: slug})
        return step_succeeded(setup, STEP, details)
    except (OSError, ConfigStoreError, ValueError) as exc:
        logger.warning("Could not save the default GitHub repository (%s)", type(exc).__name__)
        return step_failed(STEP, _storage_problem())
