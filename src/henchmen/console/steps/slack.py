"""Console step 3 (optional): connect Slack through an app manifest link (spec §4, §5.2).

Slack's "create app from manifest" link returns no credentials to Henchmen, so
the user creates the app from a fixed manifest (Socket Mode, the six bot
scopes below, no user-controlled interpolation at all -- the app name is a
literal, not a field), installs it, and pastes two tokens back: the bot token
(OAuth & Permissions) and an app-level token with ``connections:write``
(Basic Information) for Socket Mode. Both are validated with the
``cli.checks`` functions ``henchmen init`` uses -- through the ``checks``
module, never a raw ``slack_sdk`` call here, so tests can substitute fakes.
Workspaces that require approval show "Request to Install" instead of a bot
token; the step offers a copyable message for the admin, and "Check again"
simply re-submits the saved tokens.

Only :func:`choose_channel` completes the step (ruling PB-1): it joins the
chosen channel if needed, posts a test message to prove the bot can actually
write there, saves the channel and only then records completion via
``step_succeeded``. ``status``, ``manifest``, ``save_tokens`` and ``channels``
never complete the step, even on success.

Tokens are saved only through :class:`~henchmen.console.config_store.ConfigStore`
and are never echoed back; ``status`` reports only ``"configured"``/``""``.
Saving a bot token that now authenticates to a different workspace or bot
user reopens a previously completed step *before* the new tokens are
written -- the same reopen-before-write ordering as the GitHub step -- so a
reconnect to a different Slack workspace does not leave a stale "done" badge
over an unverified channel.

:func:`~henchmen.cli.checks.list_slack_channels_page` stops after
``MAX_LIST_PAGES`` pages; ``GET /channels`` reports that as
``details.truncated`` so the UI can say "showing the first N channels", and
``POST /channel`` still lets a channel beyond that listing be chosen by id --
confirmed directly with ``conversations.info`` (:func:`~henchmen.cli.checks.get_slack_channel`,
bounded to one call, fails closed) rather than trusted from the client.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from henchmen.cli import checks
from henchmen.cli.checks import CheckResult, CheckStatus, SlackChannel, SlackChannelListing, SlackScopeError
from henchmen.console.check_problems import problem_from_check
from henchmen.console.config_store import CONFIGURED, ConfigStore, ConfigStoreError
from henchmen.console.deps import get_config_store
from henchmen.console.state import SetupStateStore, SetupStep
from henchmen.console.steps import StepFailure, StepProblem, StepSuccess, get_setup_store, step_failed, step_succeeded

logger = logging.getLogger(__name__)

router = APIRouter()
STEP = SetupStep.SLACK
CONFIG_SECTION = "Slack"
BOT_TOKEN_KEY = "HENCHMEN_SLACK_BOT_TOKEN"
APP_TOKEN_KEY = "HENCHMEN_SLACK_APP_TOKEN"
CHANNEL_KEY = "HENCHMEN_SLACK_NOTIFICATION_CHANNEL"

# Display text from the last successful bot-token check (ruling: server-only,
# like the GitHub step's app slug/account), used only to notice a workspace or
# bot-user change on the next save -- never trusted as a real identifier.
WORKSPACE_CHOICE = "slack_workspace"

SLACK_BOT_SCOPES: tuple[str, ...] = (
    "app_mentions:read",
    "chat:write",
    "channels:history",
    "channels:read",
    "channels:join",
    "groups:read",
)
SLACK_MANIFEST: dict[str, Any] = {
    "display_information": {"name": "Henchmen", "description": "Turns requests in Slack into pull requests."},
    "features": {"bot_user": {"display_name": "Henchmen", "always_online": True}},
    "oauth_config": {"scopes": {"bot": list(SLACK_BOT_SCOPES)}},
    "settings": {
        "event_subscriptions": {"bot_events": ["app_mention"]},
        "interactivity": {"is_enabled": False},
        "org_deploy_enabled": False,
        "socket_mode_enabled": True,
        "token_rotation_enabled": False,
    },
}
SLACK_NEW_APP_URL = "https://api.slack.com/apps?new_app=1&manifest_json="
ADMIN_REQUEST_MESSAGE = (
    "Hi! I'd like to add the Henchmen app to our Slack workspace so our team can ask it for code changes. "
    "It reads messages that mention it, posts replies, and can see and join public channels. "
    "Could you approve the pending Henchmen install request under Manage apps? Thank you!"
)
TEST_MESSAGE = "Henchmen is connected. Mention @Henchmen in this channel to give it a task."

ConfigDep = Annotated[ConfigStore, Depends(get_config_store)]
SetupDep = Annotated[SetupStateStore, Depends(get_setup_store)]


class SlackTokens(BaseModel):
    """Tokens copied from the Slack app pages; blank reuses the saved one."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    bot_token: str = Field(default="", max_length=256, description="Bot User OAuth Token (xoxb-)")
    app_token: str = Field(default="", max_length=256, description="App-level token (xapp-)")


class ChannelChoice(BaseModel):
    """The channel Henchmen joins and reports to."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    channel_id: str = Field(..., pattern=r"^[CG][A-Z0-9]{2,20}$", description="Slack channel ID")


def slack_create_url() -> str:
    """The "create an app from this manifest" link."""
    return SLACK_NEW_APP_URL + quote(json.dumps(SLACK_MANIFEST, separators=(",", ":")), safe="")


def _channel_details(channel: SlackChannel) -> dict[str, Any]:
    return {"id": channel.id, "name": channel.name, "is_private": channel.is_private, "is_member": channel.is_member}


def _tokens_first() -> StepProblem:
    return StepProblem(
        message="Save the Slack tokens first.",
        action="Paste the bot token and the app-level token, then choose Check.",
    )


def _scope_problem(exc: SlackScopeError) -> StepProblem:
    return StepProblem(
        message=f"The Slack app is missing the {exc.needed} permission.",
        action="Add it under OAuth & Permissions, reinstall the app to your workspace, then choose Check again.",
    )


def _storage_problem() -> StepProblem:
    return StepProblem(
        message="Henchmen could not save the Slack connection.",
        action="Check that the Henchmen data folder is writable and has free space, then try again.",
    )


def _check_tokens(bot_token: str, app_token: str) -> tuple[CheckResult, CheckResult]:
    return checks.check_slack_bot_token(bot_token), checks.check_slack_app_token(app_token)


def _list_channels(bot_token: str) -> SlackChannelListing:
    return checks.list_slack_channels_page(bot_token)


async def _find_channel(bot_token: str, channel_id: str) -> tuple[SlackChannel | None, bool]:
    """The channel from the listing, or -- beyond a truncated listing -- confirmed directly by id.

    Raises :class:`SlackScopeError` from the listing call; the caller reports it.
    """
    listing = await run_in_threadpool(_list_channels, bot_token)
    selected = next((channel for channel in listing.channels if channel.id == channel_id), None)
    if selected is None and listing.truncated:
        selected = await run_in_threadpool(checks.get_slack_channel, bot_token, channel_id)
    return selected, listing.truncated


@router.get("")
async def status(config: ConfigDep, setup: SetupDep) -> StepSuccess:
    """What is saved (tokens masked). Never completes the step."""
    masked = config.masked([BOT_TOKEN_KEY, APP_TOKEN_KEY])
    return StepSuccess(
        step=STEP,
        details={
            "bot_token": masked[BOT_TOKEN_KEY],
            "app_token": masked[APP_TOKEN_KEY],
            "channel": config.get(CHANNEL_KEY),
            "completed": STEP in setup.load().completed_steps,
        },
    )


@router.get("/manifest")
async def manifest() -> StepSuccess:
    """The app manifest, the link that creates the app from it and the admin request text."""
    return StepSuccess(
        step=STEP,
        details={
            "create_url": slack_create_url(),
            "manifest": SLACK_MANIFEST,
            "admin_request_message": ADMIN_REQUEST_MESSAGE,
        },
    )


@router.post("/tokens")
async def save_tokens(body: SlackTokens, config: ConfigDep, setup: SetupDep) -> StepSuccess | StepFailure:
    """Validate both tokens and save them. Never completes the step -- only a chosen channel does."""
    bot_token = body.bot_token or config.get(BOT_TOKEN_KEY)
    app_token = body.app_token or config.get(APP_TOKEN_KEY)
    problems: list[StepProblem] = []
    if not bot_token:
        problems.append(
            StepProblem(
                field="bot_token",
                message="Paste the Bot User OAuth Token (it starts with xoxb-).",
                action=(
                    "It is under OAuth & Permissions once the app is installed. If Slack showed Request to Install "
                    f'instead, a workspace admin must approve it first. Send them: "{ADMIN_REQUEST_MESSAGE}" '
                    "Then choose Check again."
                ),
            )
        )
    elif not bot_token.startswith("xoxb-"):
        problems.append(
            StepProblem(
                field="bot_token",
                message="That is not a bot token: bot tokens start with xoxb-.",
                action="Copy the Bot User OAuth Token from OAuth & Permissions.",
            )
        )
    if not app_token:
        problems.append(
            StepProblem(
                field="app_token",
                message="Paste the app-level token (it starts with xapp-).",
                action="In Basic Information, under App-Level Tokens, generate one with the connections:write scope.",
            )
        )
    if problems:
        return step_failed(STEP, *problems)

    bot_result, app_result = await run_in_threadpool(_check_tokens, bot_token, app_token)
    failures = [
        problem_from_check(result, field=field)
        for result, field in ((bot_result, "bot_token"), (app_result, "app_token"))
        if result.status != CheckStatus.OK
    ]
    if failures:
        return step_failed(STEP, *failures)

    try:
        # Reopen a completed step before writing new tokens that authenticate to a
        # different workspace or bot user, never after: the same ordering as the
        # GitHub step's `_save_installation`. Both happen under the config file's
        # lock so a concurrent write cannot land between the check and the write;
        # `record_step_incomplete` touches a different file, but is called only
        # while nothing else can also be mutating this config file.
        with config.locked():
            previous_workspace = setup.load().server_choices.get(WORKSPACE_CHOICE, "")
            if previous_workspace and previous_workspace != bot_result.message:
                setup.record_step_incomplete(STEP)
            config.update({BOT_TOKEN_KEY: bot_token, APP_TOKEN_KEY: app_token}, section=CONFIG_SECTION)
    except (OSError, ConfigStoreError, ValueError) as exc:
        logger.warning("Could not save Slack tokens (%s)", type(exc).__name__)
        return step_failed(STEP, _storage_problem())
    try:
        # Display-only, for the next save's workspace-change check: a failure here
        # is logged but must not strand the user, since the tokens are already saved.
        setup.set_server_choices({WORKSPACE_CHOICE: bot_result.message})
    except (OSError, ValueError) as exc:
        logger.warning("Could not record the Slack workspace identity (%s)", type(exc).__name__)
    return StepSuccess(
        step=STEP, details={"workspace": bot_result.message, "bot_token": CONFIGURED, "app_token": CONFIGURED}
    )


@router.get("/channels")
async def channels(config: ConfigDep) -> StepSuccess | StepFailure:
    """Channels the bot can see. Never completes the step."""
    bot_token = config.get(BOT_TOKEN_KEY)
    if not bot_token:
        return step_failed(STEP, _tokens_first())
    try:
        listing = await run_in_threadpool(_list_channels, bot_token)
    except SlackScopeError as exc:
        return step_failed(STEP, _scope_problem(exc))
    if not listing.channels:
        problem = StepProblem(
            message="Henchmen could not find any Slack channels.",
            action="Check that the app is installed in your workspace, then choose Check again.",
        )
        return step_failed(STEP, problem)
    return StepSuccess(
        step=STEP,
        details={
            "channels": [_channel_details(channel) for channel in listing.channels],
            "truncated": listing.truncated,
        },
    )


@router.post("/channel")
async def choose_channel(body: ChannelChoice, config: ConfigDep, setup: SetupDep) -> StepSuccess | StepFailure:
    """Join the channel if needed, post a test message, save it and complete the step.

    The only route in this module that completes the step (ruling PB-1).
    """
    bot_token = config.get(BOT_TOKEN_KEY)
    if not bot_token or not config.is_set(APP_TOKEN_KEY):
        return step_failed(STEP, _tokens_first())
    try:
        selected, _truncated = await _find_channel(bot_token, body.channel_id)
    except SlackScopeError as exc:
        return step_failed(STEP, _scope_problem(exc))
    if selected is None:
        problem = StepProblem(
            field="channel_id",
            message="Henchmen can't see that channel.",
            action="For a private channel, open it in Slack and run /invite @Henchmen, then choose Check again.",
        )
        return step_failed(STEP, problem)
    if not selected.is_member:
        joined = await run_in_threadpool(checks.join_slack_channel, bot_token, selected.id)
        if joined.status != CheckStatus.OK:
            return step_failed(STEP, problem_from_check(joined, field="channel_id"))
    posted = await run_in_threadpool(checks.post_slack_message, bot_token, selected.id, TEST_MESSAGE)
    if posted.status != CheckStatus.OK:
        return step_failed(STEP, problem_from_check(posted, field="channel_id"))

    try:
        config.update({CHANNEL_KEY: selected.id}, section=CONFIG_SECTION)
    except (OSError, ConfigStoreError, ValueError) as exc:
        logger.warning("Could not save the Slack channel (%s)", type(exc).__name__)
        return step_failed(STEP, _storage_problem())
    return step_succeeded(setup, STEP, {"channel": _channel_details(selected), "test_message": "posted"})
