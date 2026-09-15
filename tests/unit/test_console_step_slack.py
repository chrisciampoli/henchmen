"""Tests for the Console's Slack step."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

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
from henchmen.console.state import SetupStep
from henchmen.console.steps import slack as slack_step
from henchmen.console.steps.slack import ADMIN_REQUEST_MESSAGE, TEST_MESSAGE
from tests.unit.console_harness import ConsoleHarness, make_harness

BASE = "/console/api/steps/slack"
BOT = "xoxb-1111-2222-secretbot"
APP = "xapp-1-A111-secretapp"
OTHER_BOT = "xoxb-9999-8888-otherbot"
CHANNELS = [
    SlackChannel(id="C0001", name="engineering", is_private=False, is_member=True),
    SlackChannel(id="C0002", name="random", is_private=False, is_member=False),
    SlackChannel(id="G0003", name="secret-project", is_private=True, is_member=False),
]
DEFAULT_WORKSPACE_MESSAGE = "authenticated as @henchmen in workspace Acme"
DEFAULT_IDENTITY = SlackIdentity(team_id="T1", user_id="U1", bot_id="B1")


class FakeSlack:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.bot_ok = True
        self.workspace_message = DEFAULT_WORKSPACE_MESSAGE
        self.identity: SlackIdentity | None = DEFAULT_IDENTITY
        self.channels: list[SlackChannel] = list(CHANNELS)
        self.truncated = False
        self.unlisted_channel: SlackChannel | None = None
        self.scope_error: str | None = None
        self.unreachable = False
        self.post_ok = True

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def check_bot(token: str, *, timeout: float = checks.DEFAULT_TIMEOUT) -> CheckResult:
            self.calls.append(("check_bot", (token,)))
            if self.bot_ok:
                return CheckResult("Slack bot token", CheckStatus.OK, self.workspace_message)
            return CheckResult(
                "Slack bot token",
                CheckStatus.FAIL,
                "rejected: invalid_auth",
                hint="Reinstall the app and copy the Bot User OAuth Token",
            )

        def check_app(token: str, *, timeout: float = checks.DEFAULT_TIMEOUT) -> CheckResult:
            self.calls.append(("check_app", (token,)))
            return CheckResult("Slack app token", CheckStatus.OK, "Socket Mode token valid")

        def bot_identity(token: str, *, timeout: float = checks.DEFAULT_TIMEOUT) -> SlackIdentity | None:
            self.calls.append(("identity", (token,)))
            return self.identity

        def list_channels_page(token: str, *, timeout: float = checks.DEFAULT_TIMEOUT) -> SlackChannelListing:
            self.calls.append(("list", (token,)))
            if self.unreachable:
                raise SlackUnreachableError("connection refused")
            if self.scope_error:
                raise SlackScopeError(self.scope_error)
            return SlackChannelListing(channels=list(self.channels), truncated=self.truncated)

        def get_channel(token: str, channel_id: str, *, timeout: float = checks.DEFAULT_TIMEOUT) -> SlackChannel | None:
            self.calls.append(("get_channel", (token, channel_id)))
            if self.unreachable:
                raise SlackUnreachableError("connection refused")
            if self.unlisted_channel is not None and self.unlisted_channel.id == channel_id:
                return self.unlisted_channel
            return None

        def join(token: str, channel_id: str, *, timeout: float = checks.DEFAULT_TIMEOUT) -> CheckResult:
            self.calls.append(("join", (token, channel_id)))
            if channel_id.startswith("G"):
                return CheckResult(
                    "Slack channel",
                    CheckStatus.WARN,
                    f"{channel_id} is private; bots cannot self-join private channels",
                    hint="In Slack, open the channel and run: /invite @<your-bot-name>",
                )
            return CheckResult("Slack channel", CheckStatus.OK, f"joined {channel_id}")

        def post(token: str, channel_id: str, text: str, *, timeout: float = checks.DEFAULT_TIMEOUT) -> CheckResult:
            self.calls.append(("post", (token, channel_id, text)))
            if self.post_ok:
                return CheckResult("Slack test message", CheckStatus.OK, f"posted a test message to {channel_id}")
            return CheckResult(
                "Slack test message", CheckStatus.FAIL, f"could not post to {channel_id}: channel_not_found"
            )

        monkeypatch.setattr(checks, "check_slack_bot_token", check_bot)
        monkeypatch.setattr(checks, "check_slack_app_token", check_app)
        monkeypatch.setattr(checks, "slack_bot_identity", bot_identity)
        monkeypatch.setattr(checks, "list_slack_channels_page", list_channels_page)
        monkeypatch.setattr(checks, "get_slack_channel", get_channel)
        monkeypatch.setattr(checks, "join_slack_channel", join)
        monkeypatch.setattr(checks, "post_slack_message", post)


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> FakeSlack:
    fake = FakeSlack()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def harness(tmp_path: Path) -> ConsoleHarness:
    return make_harness(tmp_path)


@pytest.fixture(autouse=True)
def _clear_test_message_cooldown() -> None:
    slack_step._last_test_message.clear()
    yield
    slack_step._last_test_message.clear()


def _save_tokens(harness: ConsoleHarness) -> None:
    harness.config_store.update({"HENCHMEN_SLACK_BOT_TOKEN": BOT, "HENCHMEN_SLACK_APP_TOKEN": APP}, section="Slack")


def _save_and_complete(harness: ConsoleHarness, channel_id: str = "C0001") -> None:
    assert harness.post(f"{BASE}/tokens", {"bot_token": BOT, "app_token": APP}).json()["ok"] is True
    assert harness.post(f"{BASE}/channel", {"channel_id": channel_id}).json()["ok"] is True


def test_requires_a_session(tmp_path: Path) -> None:
    assert make_harness(tmp_path, signed_in=False).get(f"{BASE}/manifest").status_code == 401


def test_manifest_link(harness: ConsoleHarness) -> None:
    details = harness.get(f"{BASE}/manifest").json()["details"]
    url = urlsplit(details["create_url"])
    assert (url.scheme, url.netloc, url.path) == ("https", "api.slack.com", "/apps")
    query = parse_qs(url.query)
    assert query["new_app"] == ["1"]
    manifest = json.loads(query["manifest_json"][0])
    assert manifest == details["manifest"]
    assert manifest["display_information"]["name"] == "Henchmen"
    assert manifest["settings"]["socket_mode_enabled"] is True
    assert manifest["settings"]["event_subscriptions"]["bot_events"] == ["app_mention"]
    assert manifest["oauth_config"]["scopes"]["bot"] == [
        "app_mentions:read",
        "chat:write",
        "channels:history",
        "channels:read",
        "channels:join",
        "groups:read",
    ]
    assert details["admin_request_message"] == ADMIN_REQUEST_MESSAGE


def test_manifest_does_not_complete_the_step(harness: ConsoleHarness) -> None:
    assert harness.get(f"{BASE}/manifest").json()["ok"] is True
    assert SetupStep.SLACK not in harness.setup_store.load().completed_steps


def test_missing_bot_token_explains_admin_approval(harness: ConsoleHarness, slack: FakeSlack) -> None:
    body = harness.post(f"{BASE}/tokens", {"app_token": APP}).json()
    assert body["ok"] is False
    problem = body["problems"][0]
    assert problem["field"] == "bot_token"
    assert "Request to Install" in problem["action"]
    assert ADMIN_REQUEST_MESSAGE in problem["action"]
    assert slack.calls == []


def test_app_token_in_the_bot_field_is_caught_before_calling_slack(harness: ConsoleHarness, slack: FakeSlack) -> None:
    body = harness.post(f"{BASE}/tokens", {"bot_token": APP, "app_token": APP}).json()
    assert body["problems"][0]["field"] == "bot_token"
    assert "xoxb-" in body["problems"][0]["message"]
    assert slack.calls == []


def test_bot_token_in_the_app_field_is_caught_before_calling_slack(harness: ConsoleHarness, slack: FakeSlack) -> None:
    body = harness.post(f"{BASE}/tokens", {"bot_token": BOT, "app_token": BOT}).json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "app_token"
    assert "xapp-" in body["problems"][0]["message"]
    assert slack.calls == []


def test_missing_app_token_explains_socket_mode(harness: ConsoleHarness, slack: FakeSlack) -> None:
    body = harness.post(f"{BASE}/tokens", {"bot_token": BOT}).json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "app_token"
    assert slack.calls == []


def test_valid_tokens_are_saved_but_do_not_complete_the_step(harness: ConsoleHarness, slack: FakeSlack) -> None:
    response = harness.post(f"{BASE}/tokens", {"bot_token": BOT, "app_token": APP})
    body = response.json()
    assert body == {
        "ok": True,
        "step": "slack",
        "details": {
            "workspace": DEFAULT_WORKSPACE_MESSAGE,
            "bot_token": "configured",
            "app_token": "configured",
        },
    }
    assert BOT not in response.text and APP not in response.text
    assert harness.config_store.get("HENCHMEN_SLACK_BOT_TOKEN") == BOT
    assert harness.config_store.get("HENCHMEN_SLACK_APP_TOKEN") == APP
    assert SetupStep.SLACK not in harness.setup_store.load().completed_steps


def test_rejected_bot_token_saves_nothing(harness: ConsoleHarness, slack: FakeSlack) -> None:
    slack.bot_ok = False
    body = harness.post(f"{BASE}/tokens", {"bot_token": BOT, "app_token": APP}).json()
    assert body["ok"] is False
    assert body["problems"] == [
        {
            "field": "bot_token",
            "message": "Slack bot token: rejected: invalid_auth",
            "action": "Reinstall the app and copy the Bot User OAuth Token",
        }
    ]
    assert not harness.config_store.config_file.exists()
    assert not any(name == "identity" for name, _ in slack.calls)


def test_check_again_reuses_saved_tokens(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_tokens(harness)
    assert harness.post(f"{BASE}/tokens", {}).json()["ok"] is True
    assert ("check_bot", (BOT,)) in slack.calls


def test_changing_the_workspace_id_reopens_a_completed_step(harness: ConsoleHarness, slack: FakeSlack) -> None:
    """F1: the fingerprint is `team_id:user_id`, not the display name."""
    _save_and_complete(harness)
    assert SetupStep.SLACK in harness.setup_store.load().completed_steps

    # Same display name/message, but a genuinely different workspace and bot user.
    slack.identity = SlackIdentity(team_id="T2", user_id="U2", bot_id="B2")
    body = harness.post(f"{BASE}/tokens", {"bot_token": OTHER_BOT, "app_token": APP}).json()
    assert body["ok"] is True
    assert body["details"]["workspace"] == DEFAULT_WORKSPACE_MESSAGE
    assert SetupStep.SLACK not in harness.setup_store.load().completed_steps


def test_missing_identity_fails_closed_and_reopens_the_step(harness: ConsoleHarness, slack: FakeSlack) -> None:
    """F1: when the ids can't be confirmed, treat it as a change rather than trust it."""
    _save_and_complete(harness)
    slack.identity = None
    body = harness.post(f"{BASE}/tokens", {"bot_token": BOT, "app_token": APP}).json()
    assert body["ok"] is True
    assert SetupStep.SLACK not in harness.setup_store.load().completed_steps
    assert harness.config_store.get("HENCHMEN_SLACK_NOTIFICATION_CHANNEL") == ""


def test_saving_the_same_workspace_again_does_not_reopen_the_step(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_and_complete(harness)
    assert harness.post(f"{BASE}/tokens", {"bot_token": BOT, "app_token": APP}).json()["ok"] is True
    assert SetupStep.SLACK in harness.setup_store.load().completed_steps
    assert harness.config_store.get("HENCHMEN_SLACK_NOTIFICATION_CHANNEL") == "C0001"


def test_workspace_change_clears_the_saved_channel(harness: ConsoleHarness, slack: FakeSlack) -> None:
    """F2: the channel is cleared in the same locked write that reopens the step."""
    _save_and_complete(harness)
    slack.identity = SlackIdentity(team_id="T2", user_id="U2", bot_id="B2")
    body = harness.post(f"{BASE}/tokens", {"bot_token": OTHER_BOT, "app_token": APP}).json()
    assert body["ok"] is True
    assert harness.config_store.get("HENCHMEN_SLACK_NOTIFICATION_CHANNEL") == ""


def test_channels_need_saved_tokens(harness: ConsoleHarness, slack: FakeSlack) -> None:
    body = harness.get(f"{BASE}/channels").json()
    assert body["ok"] is False
    assert slack.calls == []


def test_channels_are_listed(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_tokens(harness)
    body = harness.get(f"{BASE}/channels").json()
    channels = body["details"]["channels"]
    assert channels[0] == {"id": "C0001", "name": "engineering", "is_private": False, "is_member": True}
    assert len(channels) == 3
    assert body["details"]["truncated"] is False


def test_channels_reports_truncation(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_tokens(harness)
    slack.truncated = True
    body = harness.get(f"{BASE}/channels").json()
    assert body["details"]["truncated"] is True


def test_channels_does_not_complete_the_step(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_tokens(harness)
    assert harness.get(f"{BASE}/channels").json()["ok"] is True
    assert SetupStep.SLACK not in harness.setup_store.load().completed_steps


def test_missing_channel_scope_is_explained(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_tokens(harness)
    slack.scope_error = "channels:read"
    body = harness.get(f"{BASE}/channels").json()
    assert body["ok"] is False
    assert "channels:read" in body["problems"][0]["message"]
    assert "reinstall" in body["problems"][0]["action"]


def test_unreachable_slack_is_a_distinct_problem_for_channels(harness: ConsoleHarness, slack: FakeSlack) -> None:
    """F8: a connection-level failure is not the same as "no channels found"."""
    _save_tokens(harness)
    slack.unreachable = True
    body = harness.get(f"{BASE}/channels").json()
    assert body["ok"] is False
    assert "could not be reached" in body["problems"][0]["message"]


def test_unreachable_slack_is_a_distinct_problem_for_choosing_a_channel(
    harness: ConsoleHarness, slack: FakeSlack
) -> None:
    _save_tokens(harness)
    slack.unreachable = True
    body = harness.post(f"{BASE}/channel", {"channel_id": "C0001"}).json()
    assert body["ok"] is False
    assert "could not be reached" in body["problems"][0]["message"]
    assert "/invite" not in body["problems"][0]["action"]


def test_choosing_a_joined_channel_posts_and_completes(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_tokens(harness)
    body = harness.post(f"{BASE}/channel", {"channel_id": "C0001"}).json()
    assert body["ok"] is True
    assert body["details"]["channel"]["name"] == "engineering"
    assert not any(name == "join" for name, _ in slack.calls)
    assert ("post", (BOT, "C0001", TEST_MESSAGE)) in slack.calls
    assert harness.config_store.get("HENCHMEN_SLACK_NOTIFICATION_CHANNEL") == "C0001"
    assert SetupStep.SLACK in harness.setup_store.load().completed_steps


def test_choosing_a_public_channel_joins_it_first(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_tokens(harness)
    assert harness.post(f"{BASE}/channel", {"channel_id": "C0002"}).json()["ok"] is True
    names = [name for name, _ in slack.calls]
    assert names.index("join") < names.index("post")


def test_private_channel_needs_an_invite(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_tokens(harness)
    body = harness.post(f"{BASE}/channel", {"channel_id": "G0003"}).json()
    assert body["ok"] is False
    assert "/invite" in body["problems"][0]["action"]
    assert not any(name == "post" for name, _ in slack.calls)
    assert SetupStep.SLACK not in harness.setup_store.load().completed_steps


def test_failed_test_message_does_not_complete(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_tokens(harness)
    slack.post_ok = False
    body = harness.post(f"{BASE}/channel", {"channel_id": "C0001"}).json()
    assert body["ok"] is False
    assert harness.config_store.get("HENCHMEN_SLACK_NOTIFICATION_CHANNEL") == ""
    assert SetupStep.SLACK not in harness.setup_store.load().completed_steps


def test_unknown_channel(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_tokens(harness)
    body = harness.post(f"{BASE}/channel", {"channel_id": "C9999"}).json()
    assert body["problems"][0]["field"] == "channel_id"
    assert not any(name == "get_channel" for name, _ in slack.calls)
    assert harness.post(f"{BASE}/channel", {"channel_id": "general"}).status_code == 422


def test_choosing_a_channel_beyond_a_truncated_listing_is_confirmed_by_id(
    harness: ConsoleHarness, slack: FakeSlack
) -> None:
    _save_tokens(harness)
    slack.truncated = True
    slack.unlisted_channel = SlackChannel(id="C9999", name="overflow", is_private=False, is_member=True)
    body = harness.post(f"{BASE}/channel", {"channel_id": "C9999"}).json()
    assert body["ok"] is True
    assert body["details"]["channel"]["name"] == "overflow"
    assert ("get_channel", (BOT, "C9999")) in slack.calls
    assert not any(name == "join" for name, _ in slack.calls)
    assert SetupStep.SLACK in harness.setup_store.load().completed_steps


def test_channel_beyond_a_truncated_listing_that_the_bot_cannot_see_fails_closed(
    harness: ConsoleHarness, slack: FakeSlack
) -> None:
    _save_tokens(harness)
    slack.truncated = True
    body = harness.post(f"{BASE}/channel", {"channel_id": "C9999"}).json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "channel_id"
    assert ("get_channel", (BOT, "C9999")) in slack.calls


def test_status_masks_tokens(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_tokens(harness)
    response = harness.get(BASE)
    assert response.json()["details"] == {
        "bot_token": "configured",
        "app_token": "configured",
        "channel": "",
        "completed": False,
    }
    assert BOT not in response.text


def test_status_completed_requires_tokens_and_channel_all_set(harness: ConsoleHarness, slack: FakeSlack) -> None:
    """F5."""
    _save_and_complete(harness)
    assert harness.get(BASE).json()["details"]["completed"] is True

    harness.config_store.unset(["HENCHMEN_SLACK_NOTIFICATION_CHANNEL"])
    assert harness.get(BASE).json()["details"]["completed"] is False


def test_status_completed_is_false_if_a_token_is_missing(harness: ConsoleHarness, slack: FakeSlack) -> None:
    """F5."""
    _save_and_complete(harness)
    harness.config_store.unset(["HENCHMEN_SLACK_APP_TOKEN"])
    assert harness.get(BASE).json()["details"]["completed"] is False


def test_channel_choice_is_refused_if_the_bot_token_changed_meanwhile(
    harness: ConsoleHarness, slack: FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4: every network call happens before the locked compare-and-write."""
    _save_tokens(harness)

    def post_and_swap(
        token: str, channel_id: str, text: str, *, timeout: float = checks.DEFAULT_TIMEOUT
    ) -> CheckResult:
        harness.config_store.update({"HENCHMEN_SLACK_BOT_TOKEN": OTHER_BOT}, section="Slack")
        return CheckResult("Slack test message", CheckStatus.OK, f"posted a test message to {channel_id}")

    monkeypatch.setattr(checks, "post_slack_message", post_and_swap)
    body = harness.post(f"{BASE}/channel", {"channel_id": "C0001"}).json()
    assert body["ok"] is False
    assert harness.config_store.get("HENCHMEN_SLACK_NOTIFICATION_CHANNEL") == ""
    assert SetupStep.SLACK not in harness.setup_store.load().completed_steps
    assert harness.config_store.get("HENCHMEN_SLACK_BOT_TOKEN") == OTHER_BOT


def test_reselecting_the_same_completed_channel_skips_the_test_message(
    harness: ConsoleHarness, slack: FakeSlack
) -> None:
    """F6: an already-confirmed channel for the current workspace needs no second post."""
    _save_and_complete(harness)
    posts_before = sum(1 for name, _ in slack.calls if name == "post")

    body = harness.post(f"{BASE}/channel", {"channel_id": "C0001"}).json()
    assert body["ok"] is True
    posts_after = sum(1 for name, _ in slack.calls if name == "post")
    assert posts_after == posts_before


def test_cooldown_skips_a_second_post_to_the_same_channel_within_30_seconds(
    harness: ConsoleHarness, slack: FakeSlack
) -> None:
    """F6: a per-process cooldown protects a channel even when the step isn't marked complete yet."""
    _save_tokens(harness)
    assert harness.post(f"{BASE}/channel", {"channel_id": "C0002"}).json()["ok"] is True
    posts_before = sum(1 for name, _ in slack.calls if name == "post")

    # Simulate the "not yet recorded complete" race the cooldown exists for.
    harness.setup_store.record_step_incomplete(SetupStep.SLACK)
    assert harness.post(f"{BASE}/channel", {"channel_id": "C0002"}).json()["ok"] is True
    posts_after = sum(1 for name, _ in slack.calls if name == "post")
    assert posts_after == posts_before


def test_cooldown_is_per_channel(harness: ConsoleHarness, slack: FakeSlack) -> None:
    _save_tokens(harness)
    assert harness.post(f"{BASE}/channel", {"channel_id": "C0001"}).json()["ok"] is True
    posts_before = sum(1 for name, _ in slack.calls if name == "post")

    # A different channel is not covered by C0001's cooldown or completion.
    assert harness.post(f"{BASE}/channel", {"channel_id": "C0002"}).json()["ok"] is True
    posts_after = sum(1 for name, _ in slack.calls if name == "post")
    assert posts_after == posts_before + 1
