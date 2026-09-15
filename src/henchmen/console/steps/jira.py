"""Console step 4 (optional): connect Jira (spec §4, §5.3).

The user enters the site address, account email and an API token (deep link
to Atlassian's token page). ``check_jira`` validates them; the step then
offers projects and custom fields by display name so nobody types a project
key or a ``customfield_<n>`` id, and saves the project, the repository and
branch fields and the intake label (default ``henchmen``). Choices are checked
again against the site on save.

Only :func:`save` completes the step (ruling PB-1): it re-checks the chosen
project and fields against the site, saves them and only then records
completion via ``step_succeeded``. ``status``, ``connect`` and ``options``
never complete the step, even on success. ``status.details.completed`` is
also false whenever the base URL, email, API token or project key is no
longer set, even if the step was previously recorded complete.

Credentials are saved only through
:class:`~henchmen.console.config_store.ConfigStore` and the token is never
echoed back; ``status`` reports only ``"configured"``/``""`` for it.
:func:`connect` fingerprints the Jira site and account on the Atlassian
``accountId`` (:func:`~henchmen.cli.checks.jira_identity`) paired with the
site address, never a display name (an account holder can rename themselves
at any time) -- the same "stable id, never a display name" rule as the Slack
step's ``team_id:user_id``. Saving credentials that resolve to a different
site or a different account reopens a previously completed step, and clears
the saved project and custom fields, *before* the new credentials are
written -- the same reopen-before-write ordering as the GitHub and Slack
steps -- so a reconnect to a different Jira site never leaves a stale "done"
badge, or the old site's project/field choices, behind. The intake label is
deliberately *not* cleared on a site or account change: it is a Henchmen-side
label name, not a value that means anything to a particular Jira site, so it
carries over unchanged (ruling F8). When the account id cannot be confirmed
at all, the change is assumed (fail closed) rather than trusted, exactly like
the Slack step's workspace identity.

:func:`~henchmen.cli.checks.list_jira_projects` is page-bounded by
``checks._JIRA_MAX_PAGES`` (20 pages of ``checks._JIRA_PAGE_SIZE`` (50) each);
``connect`` and ``options`` report that as ``details.truncated`` so the UI can
say "showing the first N projects", and :func:`save` still lets a project
beyond that listing be chosen by key -- confirmed directly with
:func:`~henchmen.cli.checks.get_jira_project` (bounded to one call, fails
closed) rather than trusted from the client. ``/rest/api/3/field`` is not
paginated by Jira at all, so there is no truncation concept for custom
fields.

A listing failure after the credentials already checked out (a 5xx, a
timeout, a rate limit) raises
:class:`~henchmen.cli.checks.JiraUnreachableError`, reported as "Henchmen
could not list Jira projects and fields" -- distinct from a genuine empty
listing, so it is never mistaken for "no projects" and never produces "ask a
Jira admin for Browse access" advice for what is really a connectivity
problem (ruling F4). A credentials failure is itself distinguished by cause
(unreachable, wrong credentials, no site access, no such site, rate limited)
by :func:`~henchmen.cli.checks.check_jira` (ruling F3).

:func:`save` re-checks, under the config file's lock and only after every
network call, that the saved credentials are still the ones it started with
before writing the project, the fields and the intake label and completing
the step -- a save of different credentials mid-flight (through a second
browser tab, say) must not attribute a project or field confirmed on the old
site to the new one.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from henchmen.cli import checks
from henchmen.cli.checks import CheckResult, CheckStatus, JiraField, JiraIdentity, JiraProject
from henchmen.console.check_problems import problem_from_check
from henchmen.console.config_store import CONFIGURED, ConfigStore, ConfigStoreError
from henchmen.console.deps import get_config_store
from henchmen.console.state import SetupStateStore, SetupStep
from henchmen.console.steps import StepFailure, StepProblem, StepSuccess, get_setup_store, step_failed, step_succeeded
from henchmen.utils.redaction import redact

logger = logging.getLogger(__name__)

router = APIRouter()
STEP = SetupStep.JIRA
CONFIG_SECTION = "Jira"
BASE_URL_KEY = "HENCHMEN_JIRA_BASE_URL"
EMAIL_KEY = "HENCHMEN_JIRA_EMAIL"
TOKEN_KEY = "HENCHMEN_JIRA_API_TOKEN"
PROJECT_KEY = "HENCHMEN_JIRA_PROJECT_KEY"
REPO_FIELD_KEY = "HENCHMEN_JIRA_REPO_FIELD"
BRANCH_FIELD_KEY = "HENCHMEN_JIRA_BRANCH_FIELD"
INTAKE_LABEL_KEY = "HENCHMEN_JIRA_INTAKE_LABEL"
TOKEN_PAGE_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"
DEFAULT_INTAKE_LABEL = "henchmen"

# `base_url:account_id` from the last successful credentials check (server-only,
# like the GitHub step's app slug/account and the Slack step's workspace choice),
# used only to notice a real site or account change on the next connect. Never a
# display name (an account holder can rename themselves) and never trusted as a
# real credential.
JIRA_ACCOUNT_CHOICE = "jira_account"

ConfigDep = Annotated[ConfigStore, Depends(get_config_store)]
SetupDep = Annotated[SetupStateStore, Depends(get_setup_store)]


class JiraCredentials(BaseModel):
    """How to reach the Jira site. The token is write-only; blank reuses the saved one."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    base_url: str = Field(..., max_length=300, description="Jira site URL")
    email: str = Field(..., max_length=254, pattern=r"^[^@\s]+@[^@\s]+$", description="Atlassian account email")
    api_token: str = Field(default="", max_length=512, description="Atlassian API token")

    @field_validator("base_url", mode="after")
    @classmethod
    def _valid_base_url(cls, value: str) -> str:
        """Jira-specific tightening of the shared https/host rules (ruling F6; no separate copy)."""
        return checks.validate_jira_base_url(value)


class JiraChoices(BaseModel):
    """Where Jira tasks come from and how they name their repository and branch."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_key: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]{1,19}$", description="Jira project key")
    repo_field: str = Field(
        default="", pattern=r"^(customfield_\d{1,10})?$", description="Field holding owner/repo; empty uses the default"
    )
    branch_field: str = Field(
        default="", pattern=r"^(customfield_\d{1,10})?$", description="Field holding the branch; empty uses the default"
    )
    intake_label: str = Field(
        default=DEFAULT_INTAKE_LABEL, min_length=1, max_length=255, pattern=r"^\S+$", description="Intake label"
    )


def _lookup(base_url: str, email: str, api_token: str) -> tuple[CheckResult, list[JiraProject], list[JiraField], bool]:
    """Blocking credential check plus lookups (run in a worker thread), for ``options``/``save``.

    Raises :class:`~henchmen.cli.checks.JiraUnreachableError` when the
    credentials check out but the project or field listing itself fails
    (ruling F4) -- never a silent empty list for that case.
    """
    result = checks.check_jira(base_url, email, api_token)
    if result.status != CheckStatus.OK:
        return result, [], [], False
    listing = checks.list_jira_projects(base_url, email, api_token)
    fields = [field for field in checks.list_jira_fields(base_url, email, api_token) if field.custom]
    return result, listing.projects, fields, listing.truncated


def _connect_lookup(
    base_url: str, email: str, api_token: str
) -> tuple[CheckResult, JiraIdentity | None, list[JiraProject], list[JiraField], bool]:
    """Blocking credential check, account identity and lookups (run in a worker thread), for ``connect``.

    Raises :class:`~henchmen.cli.checks.JiraUnreachableError` on a listing
    failure, the same as :func:`_lookup`.
    """
    result = checks.check_jira(base_url, email, api_token)
    if result.status != CheckStatus.OK:
        return result, None, [], [], False
    identity = checks.jira_identity(base_url, email, api_token)
    listing = checks.list_jira_projects(base_url, email, api_token)
    fields = [field for field in checks.list_jira_fields(base_url, email, api_token) if field.custom]
    return result, identity, listing.projects, fields, listing.truncated


def _options(
    result: CheckResult, projects: list[JiraProject], fields: list[JiraField], truncated: bool
) -> dict[str, Any]:
    return {
        "account": result.message,
        "projects": [{"key": project.key, "name": project.name} for project in projects],
        "fields": [{"id": field.id, "name": field.name} for field in fields],
        "truncated": truncated,
    }


def _credentials_problem(result: CheckResult) -> StepProblem:
    # `check_jira` already distinguishes unreachable/wrong-credentials/no-access/no-such-site/
    # rate-limited (ruling F3); a "no such site" message points at the address field, not the token.
    field = "base_url" if "no Jira site at that address" in result.message else "api_token"
    problem = problem_from_check(result, field=field)
    if problem.action:
        return problem
    return problem.model_copy(
        update={"action": f"Check the site address and email, or create a new API token at {TOKEN_PAGE_URL}"}
    )


def _listing_problem(exc: Exception) -> StepProblem:
    """A listing failure after credentials already checked out (ruling F4) -- distinct from "no projects"."""
    return StepProblem(
        message=redact(f"Henchmen could not list Jira projects and fields ({exc})."),
        action="Try again in a moment. If this keeps happening, check Jira's own status page.",
    )


def _storage_problem() -> StepProblem:
    return StepProblem(
        message="Henchmen could not save the Jira connection.",
        action="Check that the Henchmen data folder is writable and has free space, then try again.",
    )


def _saved_credentials(config: ConfigStore) -> tuple[str, str, str] | None:
    base_url, email, token = config.get(BASE_URL_KEY), config.get(EMAIL_KEY), config.get(TOKEN_KEY)
    return (base_url, email, token) if base_url and email and token else None


def _connect_first() -> StepProblem:
    return StepProblem(message="Connect Jira first.", action="Enter your Jira site address, email and API token.")


def _account_fingerprint(base_url: str, identity: JiraIdentity | None) -> str:
    """``base_url:account_id``, or ``""`` when the identity could not be confirmed (fail closed)."""
    return f"{base_url}:{identity.account_id}" if identity is not None else ""


@router.get("")
async def status(config: ConfigDep, setup: SetupDep) -> StepSuccess:
    """What is saved (token masked). Never completes the step."""
    saved = config.masked(
        [BASE_URL_KEY, EMAIL_KEY, TOKEN_KEY, PROJECT_KEY, REPO_FIELD_KEY, BRANCH_FIELD_KEY, INTAKE_LABEL_KEY]
    )
    return StepSuccess(
        step=STEP,
        details={
            "base_url": saved[BASE_URL_KEY],
            "email": saved[EMAIL_KEY],
            "api_token": saved[TOKEN_KEY],
            "project_key": saved[PROJECT_KEY],
            "repo_field": saved[REPO_FIELD_KEY],
            "branch_field": saved[BRANCH_FIELD_KEY],
            "intake_label": saved[INTAKE_LABEL_KEY] or DEFAULT_INTAKE_LABEL,
            # A recorded completion counts only while what it verified is still configured.
            "completed": STEP in setup.load().completed_steps
            and config.is_set(BASE_URL_KEY)
            and config.is_set(EMAIL_KEY)
            and config.is_set(TOKEN_KEY)
            and config.is_set(PROJECT_KEY),
        },
    )


@router.post("/credentials")
async def connect(body: JiraCredentials, config: ConfigDep, setup: SetupDep) -> StepSuccess | StepFailure:
    """Validate the credentials, save them and return projects and custom fields. Never completes the step."""
    token = body.api_token or config.get(TOKEN_KEY)
    if not token:
        problem = StepProblem(
            field="api_token", message="Paste your Jira API token.", action=f"Create one at {TOKEN_PAGE_URL}"
        )
        return step_failed(STEP, problem)
    base_url = body.base_url.rstrip("/")
    try:
        result, identity, projects, fields, truncated = await run_in_threadpool(
            _connect_lookup, base_url, body.email, token
        )
    except checks.JiraUnreachableError as exc:
        return step_failed(STEP, _listing_problem(exc))
    if result.status != CheckStatus.OK:
        return step_failed(STEP, _credentials_problem(result))
    new_fingerprint = _account_fingerprint(base_url, identity)
    try:
        # Reopen a completed step -- and clear its saved project and custom fields,
        # which belonged to the old site or account -- before writing credentials that
        # resolve to a different site or account, never after: the same ordering as
        # the GitHub and Slack steps' reopen-before-write. Fingerprinted on the
        # Atlassian account id paired with the site address (never a display name),
        # since one Atlassian account can hold projects on more than one Jira site.
        # The intake label is deliberately left out of `unset`: it names nothing
        # site-specific, so it survives a reconnect unchanged (ruling F8).
        # `identity is None` (the id could not be confirmed) is itself treated as a
        # change: fail closed rather than assume nothing moved. `record_step_incomplete`
        # touches a different file, but is called only while nothing else can also be
        # mutating this config file (no `await` inside the lock).
        with config.locked():
            previous_fingerprint = setup.load().server_choices.get(JIRA_ACCOUNT_CHOICE, "")
            changed = identity is None or (previous_fingerprint and previous_fingerprint != new_fingerprint)
            if changed:
                setup.record_step_incomplete(STEP)
                config.update(
                    {BASE_URL_KEY: base_url, EMAIL_KEY: body.email, TOKEN_KEY: token},
                    section=CONFIG_SECTION,
                    unset=(PROJECT_KEY, REPO_FIELD_KEY, BRANCH_FIELD_KEY),
                )
            else:
                config.update({BASE_URL_KEY: base_url, EMAIL_KEY: body.email, TOKEN_KEY: token}, section=CONFIG_SECTION)
    except (OSError, ConfigStoreError, ValueError) as exc:
        logger.warning("Could not save Jira credentials (%s)", type(exc).__name__)
        return step_failed(STEP, _storage_problem())
    try:
        # Display-only, for the next connect's account-change check: a failure here
        # is logged but must not strand the user, since the credentials are already saved.
        setup.set_server_choices({JIRA_ACCOUNT_CHOICE: new_fingerprint})
    except (OSError, ValueError) as exc:
        logger.warning("Could not record the Jira account identity (%s)", type(exc).__name__)
    return StepSuccess(step=STEP, details=_options(result, projects, fields, truncated))


@router.get("/options")
async def options(config: ConfigDep) -> StepSuccess | StepFailure:
    """Projects and custom fields for the saved credentials. Never completes the step."""
    saved = _saved_credentials(config)
    if saved is None:
        return step_failed(STEP, _connect_first())
    try:
        result, projects, fields, truncated = await run_in_threadpool(_lookup, *saved)
    except checks.JiraUnreachableError as exc:
        return step_failed(STEP, _listing_problem(exc))
    if result.status != CheckStatus.OK:
        return step_failed(STEP, _credentials_problem(result))
    return StepSuccess(step=STEP, details=_options(result, projects, fields, truncated))


@router.post("")
async def save(body: JiraChoices, config: ConfigDep, setup: SetupDep) -> StepSuccess | StepFailure:
    """Check the choices against the site, save them and complete the step.

    The only route in this module that completes the step (ruling PB-1).
    """
    saved = _saved_credentials(config)
    if saved is None:
        return step_failed(STEP, _connect_first())
    try:
        result, projects, fields, truncated = await run_in_threadpool(_lookup, *saved)
    except checks.JiraUnreachableError as exc:
        return step_failed(STEP, _listing_problem(exc))
    if result.status != CheckStatus.OK:
        return step_failed(STEP, _credentials_problem(result))

    problems: list[StepProblem] = []
    project_keys = {project.key for project in projects}
    if body.project_key not in project_keys and truncated:
        # The listing was cut short (ruling F5): confirm directly before giving up,
        # the same pattern as the Slack step's `get_slack_channel` fallback.
        try:
            direct = await run_in_threadpool(checks.get_jira_project, *saved, body.project_key)
        except checks.JiraUnreachableError as exc:
            return step_failed(STEP, _listing_problem(exc))
        if direct is not None:
            project_keys = {*project_keys, direct.key}
    if body.project_key not in project_keys:
        problems.append(
            StepProblem(
                field="project_key",
                message=f"Henchmen can't see the Jira project {body.project_key}.",
                action=(
                    "Pick a project from the list. If it is missing, ask a Jira admin to give this "
                    "account Browse access."
                ),
            )
        )
    field_ids = {field.id for field in fields}
    for name, value in (("repo_field", body.repo_field), ("branch_field", body.branch_field)):
        if value and value not in field_ids:
            problems.append(
                StepProblem(
                    field=name,
                    message=f"{value} is not a custom field on this Jira site.",
                    action="Pick a field from the list.",
                )
            )
    if body.repo_field and body.repo_field == body.branch_field:
        problems.append(
            StepProblem(
                field="branch_field",
                message="The repository and the branch need different fields.",
                action="Pick another field for the branch.",
            )
        )
    if problems:
        return step_failed(STEP, *problems)

    # Every network call is done before this point (ruling F4: never await while
    # holding the lock). Re-check that the credentials are still the ones this
    # request started with: a save of different credentials mid-flight (a second
    # browser tab, say) must not attribute a project or field confirmed on the old
    # site to the new one.
    with config.locked():
        if _saved_credentials(config) != saved:
            problem = StepProblem(
                message="The Jira connection changed while choosing these options.",
                action="Choose the project and fields again.",
            )
            return step_failed(STEP, problem)
        try:
            config.update(
                {
                    PROJECT_KEY: body.project_key,
                    REPO_FIELD_KEY: body.repo_field,
                    BRANCH_FIELD_KEY: body.branch_field,
                    INTAKE_LABEL_KEY: body.intake_label,
                },
                section=CONFIG_SECTION,
            )
        except (OSError, ConfigStoreError, ValueError) as exc:
            logger.warning("Could not save the Jira project and fields (%s)", type(exc).__name__)
            return step_failed(STEP, _storage_problem())
        return step_succeeded(
            setup,
            STEP,
            {
                "project_key": body.project_key,
                "repo_field": body.repo_field,
                "branch_field": body.branch_field,
                "intake_label": body.intake_label,
                "api_token": CONFIGURED,
            },
        )
