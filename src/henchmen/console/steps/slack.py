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
never complete the step, even on success. ``status.details.completed`` is
also false whenever the bot token, the app token or the channel is no longer
set, even if the step was previously recorded complete.

Tokens are saved only through :class:`~henchmen.console.config_store.ConfigStore`
and are never echoed back; ``status`` reports only ``"configured"``/``""``.
Saving a bot token that now authenticates to a different workspace or bot
user reopens a previously completed step, and clears the saved channel,
*before* the new tokens are written -- the same reopen-before-write ordering
as the GitHub step -- so a reconnect to a different Slack workspace never
leaves a stale "done" badge, or the old workspace's channel id, behind. The
workspace/user comparison uses Slack's own ``team_id``/``user_id``
(:func:`~henchmen.cli.checks.slack_bot_identity`), never a display name --
two workspaces can share a name. When those ids cannot be confirmed, the
change is assumed (fail closed) rather than trusted.

:func:`~henchmen.cli.checks.list_slack_channels_page` stops after
``MAX_LIST_PAGES`` pages; ``GET /channels`` reports that as
``details.truncated`` so the UI can say "showing the first N channels", and
``POST /channel`` still lets a channel beyond that listing be chosen by id --
confirmed directly with ``conversations.info``
(:func:`~henchmen.cli.checks.get_slack_channel`, bounded to one call, fails
closed) rather than trusted from the client. A connection-level failure
(:class:`~henchmen.cli.checks.SlackUnreachableError`) is reported as "Slack
could not be reached", distinct from a definite "you can't see this channel"
answer, so the user is not sent to ask for an invite over what is really a
network hiccup.

``choose_channel`` re-checks, under the config file's lock and only after
every network call, that the bot token is still the one it started with
before writing the channel and completing the step -- a save of different
tokens mid-flight (through a second browser tab, say) must not attribute a
channel confirmed with the old bot to the new one. It also skips posting a
fresh test message when the same channel is already confirmed for the
current workspace and the step is already complete, and applies a short
per-process cooldown keyed on the current workspace fingerprint and channel
together (never the channel alone -- a brand-new bot token must still post),
so repeatedly re-choosing the same channel (or mashing "Check again") does
not spam the workspace with test messages.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from henchmen.cli import checks
from henchmen.cli.checks import (
    CheckResult,
    CheckStatus,
    SlackChannel,
    SlackChannelListing,
    SlackIdentity,
    SlackScopeError,
    SlackUnreachableError,
)
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

# `team_id:user_id` from the last successful bot-token check (server-only,
# like the GitHub step's app slug/account), used only to notice a real
# workspace or bot-user change on the next save. Never a display name (two
# workspaces can share one) and never trusted as a real credential.
WORKSPACE_CHOICE = "slack_workspace"

# A repeat "Check again"/re-choose of an already-confirmed channel, or a user
# mashing the choose-channel action, must not spam the workspace with a fresh
# test message every time (ruling F6). Module-level and per-process: a
# generous, best-effort cooldown, not a security control. Keyed on
# `(workspace fingerprint, channel_id)`, not the channel alone (ruling F1):
# otherwise a brand-new bot token (a different workspace, or the same
# workspace reinstalled with a new bot user) that happens to pick the same
# channel id within the window would be silently treated as "already
# confirmed" for a workspace it has never actually posted to.
_TEST_MESSAGE_COOLDOWN_SECONDS = 30.0
_last_test_message: dict[tuple[str, str], float] = {}

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


def _unreachable_problem() -> StepProblem:
    return StepProblem(
        message="Slack could not be reached.",
        action="Check Henchmen's network connection and choose Check again.",
    )


def _storage_problem() -> StepProblem:
    return StepProblem(
        message="Henchmen could not save the Slack connection.",
        action="Check that the Henchmen data folder is writable and has free space, then try again.",
    )


def _check_tokens(bot_token: str, app_token: str) -> tuple[CheckResult, CheckResult, SlackIdentity | None]:
    bot_result = checks.check_slack_bot_token(bot_token)
    app_result = checks.check_slack_app_token(app_token)
    # Only worth a second call when the bot token actually authenticated.
    identity = checks.slack_bot_identity(bot_token) if bot_result.status == CheckStatus.OK else None
    return bot_result, app_result, identity


def _fingerprint(identity: SlackIdentity | None) -> str:
    """``team_id:user_id``, or ``""`` when the identity could not be confirmed (fail closed)."""
    return f"{identity.team_id}:{identity.user_id}" if identity is not None else ""


def _list_channels(bot_token: str) -> SlackChannelListing:
    return checks.list_slack_channels_page(bot_token)


async def _find_channel(bot_token: str, channel_id: str) -> tuple[SlackChannel | None, bool]:
    """The channel from the listing, or -- beyond a truncated listing -- confirmed directly by id.

    Raises :class:`SlackScopeError` or :class:`SlackUnreachableError` from
    either Slack call; the caller reports it.
    """
    listing = await run_in_threadpool(_list_channels, bot_token)
    selected = next((channel for channel in listing.channels if channel.id == channel_id), None)
    if selected is None and listing.truncated:
        selected = await run_in_threadpool(checks.get_slack_channel, bot_token, channel_id)
    return selected, listing.truncated


def _skip_test_message(config: ConfigStore, setup: SetupStateStore, channel_id: str) -> bool:
    """True when posting the test message again would be redundant (ruling F6).

    Either this exact channel is already saved as the completed step's
    channel for a confirmed workspace fingerprint (a repeat "Check
    again"/re-choice of the same channel needs no second proof), or a test
    message was already sent to this exact ``(workspace fingerprint,
    channel_id)`` pair within the cooldown window (ruling F1) -- both are
    best-effort, not security checks: :func:`choose_channel` still
    re-verifies the bot token under lock before writing anything.
    """
    state = setup.load()
    fingerprint = state.server_choices.get(WORKSPACE_CHOICE, "")
    already_confirmed = STEP in state.completed_steps and bool(fingerprint) and config.get(CHANNEL_KEY) == channel_id
    if already_confirmed:
        return True
    last_sent = _last_test_message.get((fingerprint, channel_id))
    return last_sent is not None and (time.monotonic() - last_sent) < _TEST_MESSAGE_COOLDOWN_SECONDS


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
            # A recorded completion counts only while what it verified is still configured.
            "completed": STEP in setup.load().completed_steps
            and config.is_set(BOT_TOKEN_KEY)
            and config.is_set(APP_TOKEN_KEY)
            and config.is_set(CHANNEL_KEY),
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
    elif not app_token.startswith("xapp-"):
        problems.append(
            StepProblem(
                field="app_token",
                message="That is not an app-level token: app-level tokens start with xapp-.",
                action="Generate one in Basic Information, under App-Level Tokens, with the connections:write scope.",
            )
        )
    if problems:
        return step_failed(STEP, *problems)

    bot_result, app_result, identity = await run_in_threadpool(_check_tokens, bot_token, app_token)
    failures = [
        problem_from_check(result, field=field)
        for result, field in ((bot_result, "bot_token"), (app_result, "app_token"))
        if result.status != CheckStatus.OK
    ]
    if failures:
        return step_failed(STEP, *failures)

    new_fingerprint = _fingerprint(identity)
    try:
        # Reopen a completed step -- and clear its saved channel, which belonged to
        # the old workspace -- before writing new tokens that authenticate to a
        # different workspace or bot user, never after: the same ordering as the
        # GitHub step's `_save_installation`. All three happen in one atomic,
        # locked write. `identity is None` (the ids could not be confirmed) is
        # itself treated as a change: fail closed rather than assume nothing moved.
        # `record_step_incomplete` touches a different file, but is called only
        # while nothing else can also be mutating this config file (no `await`
        # inside the lock).
        with config.locked():
            previous_fingerprint = setup.load().server_choices.get(WORKSPACE_CHOICE, "")
            changed = identity is None or (previous_fingerprint and previous_fingerprint != new_fingerprint)
            if changed:
                setup.record_step_incomplete(STEP)
                config.update(
                    {BOT_TOKEN_KEY: bot_token, APP_TOKEN_KEY: app_token},
                    section=CONFIG_SECTION,
                    unset=(CHANNEL_KEY,),
                )
            else:
                config.update({BOT_TOKEN_KEY: bot_token, APP_TOKEN_KEY: app_token}, section=CONFIG_SECTION)
    except (OSError, ConfigStoreError, ValueError) as exc:
        logger.warning("Could not save Slack tokens (%s)", type(exc).__name__)
        return step_failed(STEP, _storage_problem())
    try:
        # Display-only, for the next save's workspace-change check: a failure here
        # is logged but must not strand the user, since the tokens are already saved.
        setup.set_server_choices({WORKSPACE_CHOICE: new_fingerprint})
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
    except SlackUnreachableError:
        return step_failed(STEP, _unreachable_problem())
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
    except SlackUnreachableError:
        return step_failed(STEP, _unreachable_problem())
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

    fingerprint = setup.load().server_choices.get(WORKSPACE_CHOICE, "")
    skipped = _skip_test_message(config, setup, selected.id)
    if skipped:
        posted = CheckResult("Slack test message", CheckStatus.OK, f"already confirmed for {selected.id}")
    else:
        posted = await run_in_threadpool(checks.post_slack_message, bot_token, selected.id, TEST_MESSAGE)
        if posted.status == CheckStatus.OK:
            _last_test_message[(fingerprint, selected.id)] = time.monotonic()
    if posted.status != CheckStatus.OK:
        return step_failed(STEP, problem_from_check(posted, field="channel_id"))
    test_message_state = "already confirmed" if skipped else "posted"

    # Every network call is done before this point (ruling F4: never await while
    # holding the lock). Re-check that the bot token is still the one this request
    # started with: a save of different tokens mid-flight (a second browser tab,
    # say) must not attribute a channel confirmed with the old bot to the new one.
    with config.locked():
        if config.get(BOT_TOKEN_KEY) != bot_token:
            problem = StepProblem(
                message="The Slack connection changed while choosing this channel.",
                action="Choose the channel again.",
            )
            return step_failed(STEP, problem)
        try:
            config.update({CHANNEL_KEY: selected.id}, section=CONFIG_SECTION)
        except (OSError, ConfigStoreError, ValueError) as exc:
            logger.warning("Could not save the Slack channel (%s)", type(exc).__name__)
            return step_failed(STEP, _storage_problem())
        return step_succeeded(setup, STEP, {"channel": _channel_details(selected), "test_message": test_message_state})
