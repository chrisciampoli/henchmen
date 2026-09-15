"""Live credential and service checks shared by ``henchmen doctor`` and ``henchmen init``.

Every ``check_*`` function returns a :class:`CheckResult` and never raises:
network errors, authentication failures and missing optional SDKs all become
``FAIL`` or ``WARN`` results with an actionable ``hint``. ``list_*`` helpers
return an empty list on any failure so callers can fall back to free-text
input -- except :func:`list_jira_projects` and :func:`list_jira_fields`,
which raise :class:`JiraUnreachableError` instead (ruling F4): a listing
failure after credentials already checked out (a 5xx, a timeout, a rate
limit) must never be read as "zero projects" by a caller that would then
offer fail-open advice like "ask a Jira admin for Browse access".

SDK clients are built through small module-level factories (``_anthropic_client``
and friends) so tests can substitute fakes without touching the network.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

import httpx

from henchmen.config.settings import Settings, require_secure_github_url
from henchmen.providers.pricing import PRICE_TABLE
from henchmen.providers.tiers import TIER_FIELDS
from henchmen.utils.redaction import redact

DEFAULT_TIMEOUT = 10.0

# Every cursor/next-token paging loop in this module stops after this many
# pages rather than trusting a remote API to eventually return an empty
# cursor -- a misbehaving or malicious server otherwise hangs the check.
MAX_LIST_PAGES = 50

RECOMMENDED_OLLAMA_MODELS: tuple[str, ...] = ("qwen2.5-coder:7b", "llama3.3", "deepseek-r1:8b")

_logger = logging.getLogger(__name__)

# Vertex AI has no per-key model listing Henchmen can use; these are the Gemini
# models the tier defaults and the price table know. No Claude on Vertex AI.
# Computed, never a literal (CLAUDE.md: never hardcode a model name).
VERTEX_MODELS: tuple[str, ...] = tuple(
    dict.fromkeys(
        [str(Settings.model_fields[field].default) for field in TIER_FIELDS["gcp"].values()]
        + [model for model in PRICE_TABLE if model.startswith("gemini-")]
    )
)

_OPENAI_EXCLUDE: tuple[str, ...] = (
    "realtime",
    "audio",
    "tts",
    "transcribe",
    "embedding",
    "moderation",
    "image",
    "dall",
    "whisper",
    "search",
    "instruct",
    "babbage",
    "davinci",
)


class CheckStatus(StrEnum):
    """Outcome of a single diagnostic check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class CheckResult:
    """Result of a single check."""

    name: str
    status: CheckStatus
    message: str
    hint: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.status == CheckStatus.OK

    @property
    def is_failure(self) -> bool:
        return self.status == CheckStatus.FAIL


@dataclass(frozen=True)
class SlackChannel:
    """A Slack conversation the bot could join or post to."""

    id: str
    name: str
    is_private: bool
    is_member: bool

    @property
    def display(self) -> str:
        lock = " (private)" if self.is_private else ""
        member = " [joined]" if self.is_member else ""
        return f"#{self.name}{lock}{member}"


class SlackScopeError(Exception):
    """Raised when Slack rejects a call because the bot token lacks a scope."""

    def __init__(self, needed: str) -> None:
        super().__init__(f"Slack bot token is missing the '{needed}' scope")
        self.needed = needed


class SlackUnreachableError(Exception):
    """Raised when Slack could not be reached at all: no SDK, or a connection-level failure.

    Distinguished from a definite "no, you can't see this" answer (``None``
    from :func:`get_slack_channel`, an empty/non-scope failure from
    :func:`list_slack_channels_page`'s ``ImportError`` branch) so a caller can
    show "Slack could not be reached, try again" instead of fail-closed advice
    that assumes Slack actually answered (e.g. "ask for an invite").
    """


def _is_unreachable(exc: BaseException) -> bool:
    """True when ``exc`` carries no Slack ``response`` -- a connection-level failure, not an API answer."""
    return getattr(exc, "response", None) is None


# ---------------------------------------------------------------------------
# Client factories (patched in tests)
# ---------------------------------------------------------------------------


def _http_client(timeout: float) -> httpx.Client:
    # trust_env=False: an ambient proxy or netrc configuration must not see the
    # credentials these calls carry (a Jira API token, an Ollama address).
    return httpx.Client(timeout=timeout, trust_env=False)


def _anthropic_client(api_key: str, timeout: float) -> Any:
    import anthropic

    return anthropic.Anthropic(api_key=api_key, timeout=timeout)


def _openai_client(api_key: str, timeout: float) -> Any:
    import openai

    return openai.OpenAI(api_key=api_key, timeout=timeout)


def _github_client(token: str, timeout: float) -> Any:
    import github

    return github.Github(auth=github.Auth.Token(token), timeout=int(timeout))


def _slack_client(token: str, timeout: float) -> Any:
    from slack_sdk import WebClient

    return WebClient(token=token, timeout=int(timeout))


def _google_default_credentials() -> tuple[Any, str | None]:
    import google.auth

    credentials, project = google.auth.default()
    return credentials, project


def _bedrock_client(region: str, timeout: float) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock",
        region_name=region,
        config=Config(connect_timeout=timeout, read_timeout=timeout, retries={"max_attempts": 1}),
    )


def _slack_error_code(exc: BaseException) -> str:
    """Extract Slack's ``error`` string from a SlackApiError-shaped exception."""
    response = getattr(exc, "response", None)
    try:
        return str(response["error"])  # type: ignore[index]
    except (TypeError, KeyError):
        return str(exc)


def _slack_needed_scope(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    try:
        return str(response.get("needed", ""))  # type: ignore[union-attr]
    except AttributeError:
        return ""


def _short(exc: BaseException, limit: int = 160) -> str:
    """First line of the exception text, redacted, truncated to ``limit`` characters.

    Every ``check_*`` function surfaces exception text to the user through
    this helper, so redacting here (rather than at each call site) protects
    every check at once from leaking a credential or an AWS account id
    embedded in an SDK error message.
    """
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return redact(text)[:limit]


def _sdk_missing(name: str, extra: str, exc: ImportError) -> CheckResult:
    return CheckResult(
        name=name,
        status=CheckStatus.WARN,
        message=f"SDK not installed ({_short(exc)}); credential stored but not verified",
        hint=f'Install it with: pip install -e ".[{extra}]"',
    )


# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------


def check_anthropic_key(api_key: str, *, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Verify an Anthropic API key by listing models."""
    name = "Anthropic API key"
    if not api_key:
        return CheckResult(name, CheckStatus.FAIL, "no API key set", hint="Set HENCHMEN_ANTHROPIC_API_KEY")
    try:
        models = _list_paged_ids(_anthropic_client(api_key, timeout).models.list(limit=100))
    except ImportError as exc:
        return _sdk_missing(name, "anthropic", exc)
    except Exception as exc:
        return CheckResult(
            name, CheckStatus.FAIL, f"rejected: {_short(exc)}", hint="Check the key at console.anthropic.com"
        )
    return CheckResult(name, CheckStatus.OK, f"valid ({len(models)} models available)")


def list_anthropic_models(api_key: str, *, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Return the sorted model IDs the key can access, or ``[]`` on any failure."""
    try:
        return _list_paged_ids(_anthropic_client(api_key, timeout).models.list(limit=100))
    except Exception:
        return []


def check_openai_key(api_key: str, *, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Verify an OpenAI API key by listing models."""
    name = "OpenAI API key"
    if not api_key:
        return CheckResult(name, CheckStatus.FAIL, "no API key set", hint="Set HENCHMEN_OPENAI_API_KEY")
    try:
        models = _list_paged_ids(_openai_client(api_key, timeout).models.list())
    except ImportError as exc:
        return _sdk_missing(name, "openai", exc)
    except Exception as exc:
        return CheckResult(
            name, CheckStatus.FAIL, f"rejected: {_short(exc)}", hint="Check the key at platform.openai.com"
        )
    return CheckResult(name, CheckStatus.OK, f"valid ({len(models)} models available)")


def list_openai_models(api_key: str, *, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    try:
        return _list_paged_ids(_openai_client(api_key, timeout).models.list())
    except Exception:
        return []


def filter_openai_models(models: Sequence[str]) -> list[str]:
    """Keep chat/reasoning models an operative can use; drop audio, image, embedding and legacy ids."""
    keep: list[str] = []
    for model in models:
        lowered = model.lower()
        if not lowered.startswith(("gpt-", "o1", "o3", "o4")):
            continue
        if any(marker in lowered for marker in _OPENAI_EXCLUDE):
            continue
        keep.append(model)
    return keep


def _list_paged_ids(page: Any) -> list[str]:
    ids = [str(getattr(item, "id", "")) for item in getattr(page, "data", [])]
    return sorted(i for i in ids if i)


def check_ollama(base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Verify an Ollama server is reachable and has at least one model pulled."""
    name = "Ollama"
    try:
        models = _ollama_tags(base_url, timeout)
    except httpx.HTTPError as exc:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"cannot reach {base_url} ({_short(exc)})",
            hint="Start it with: ollama serve",
        )
    except Exception as exc:
        return CheckResult(name, CheckStatus.FAIL, f"unexpected response from {base_url}: {_short(exc)}")
    if not models:
        return CheckResult(
            name,
            CheckStatus.WARN,
            f"reachable at {base_url} but no models pulled",
            hint=f"Pull one with: ollama pull {RECOMMENDED_OLLAMA_MODELS[0]}",
        )
    return CheckResult(name, CheckStatus.OK, f"reachable at {base_url} ({len(models)} models pulled)")


def list_ollama_models(base_url: str, *, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    try:
        return _ollama_tags(base_url, timeout)
    except Exception:
        return []


def _ollama_tags(base_url: str, timeout: float) -> list[str]:
    with _http_client(timeout) as client:
        response = client.get(f"{base_url.rstrip('/')}/api/tags")
        response.raise_for_status()
        data = response.json()
    names = [str(m.get("name", "")) for m in data.get("models", [])]
    return sorted(n for n in names if n)


def check_vertex(project_id: str, region: str, *, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Verify Application Default Credentials exist for Vertex AI."""
    name = "Vertex AI credentials"
    if not project_id:
        return CheckResult(name, CheckStatus.FAIL, "no GCP project set", hint="Set HENCHMEN_GCP_PROJECT_ID")
    try:
        _, adc_project = _google_default_credentials()
    except ImportError as exc:
        return _sdk_missing(name, "gcp", exc)
    except Exception as exc:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"no Application Default Credentials ({_short(exc)})",
            hint="Run: gcloud auth application-default login",
        )
    if adc_project and adc_project != project_id:
        return CheckResult(
            name,
            CheckStatus.WARN,
            f"ADC found for project {adc_project!r} but HENCHMEN_GCP_PROJECT_ID is {project_id!r}",
            hint=f"Run: gcloud auth application-default set-quota-project {project_id}",
        )
    return CheckResult(name, CheckStatus.OK, f"ADC present for {project_id} ({region})")


def check_bedrock(region: str, *, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Verify AWS credentials can list Bedrock text models in ``region``."""
    name = "AWS Bedrock"
    if not region:
        return CheckResult(name, CheckStatus.FAIL, "no AWS region set", hint="Set HENCHMEN_AWS_REGION")
    try:
        models = _bedrock_model_ids(_bedrock_client(region, timeout))
    except ImportError as exc:
        return _sdk_missing(name, "aws", exc)
    except Exception as exc:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"cannot list Bedrock models in {region}: {_short(exc)}",
            hint=(
                "Give Henchmen AWS credentials allowed to call bedrock:ListFoundationModels and "
                "bedrock:ListInferenceProfiles in this region"
            ),
        )
    if not models:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"no text models are available in {region}",
            hint="Request model access in the Bedrock console for this region",
        )
    return CheckResult(name, CheckStatus.OK, f"reachable in {region} ({len(models)} models)")


def list_bedrock_models(region: str, *, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    """Foundation model ids and inference profile ids, sorted; ``[]`` on any failure."""
    try:
        return _bedrock_model_ids(_bedrock_client(region, timeout))
    except Exception:
        return []


def _bedrock_model_ids(client: Any) -> list[str]:
    ids: set[str] = set()
    for summary in client.list_foundation_models(byOutputModality="TEXT").get("modelSummaries", []):
        model_id = str(summary.get("modelId", ""))
        if model_id:
            ids.add(model_id)
    next_token: str | None = None
    for _page in range(MAX_LIST_PAGES):
        kwargs: dict[str, Any] = {"maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        page = client.list_inference_profiles(**kwargs)
        for summary in page.get("inferenceProfileSummaries", []):
            profile_id = str(summary.get("inferenceProfileId", ""))
            if profile_id:
                ids.add(profile_id)
        next_token = page.get("nextToken") or None
        if not next_token:
            break
    else:
        raise RuntimeError(f"Bedrock inference profiles are still paging after {MAX_LIST_PAGES} pages")
    return sorted(ids)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


def check_github_token(token: str, *, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Verify a GitHub token by fetching the authenticated user."""
    name = "GitHub token"
    if not token:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            "no token set",
            hint="Set HENCHMEN_GITHUB_TOKEN to a classic PAT with the 'repo' scope",
        )
    try:
        login = _github_client(token, timeout).get_user().login
    except ImportError as exc:
        return _sdk_missing(name, "dev", exc)
    except Exception as exc:
        return CheckResult(
            name, CheckStatus.FAIL, f"rejected: {_short(exc)}", hint="Create a token at github.com/settings/tokens"
        )
    return CheckResult(name, CheckStatus.OK, f"authenticated as {login}")


def check_github_repo(token: str, repo: str, *, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Verify the default target repository exists and the token can push to it."""
    name = "GitHub repository"
    if not repo or "/" not in repo:
        return CheckResult(name, CheckStatus.FAIL, f"{repo!r} is not in owner/repo form")
    try:
        gh_repo = _github_client(token, timeout).get_repo(repo)
    except ImportError as exc:
        return _sdk_missing(name, "dev", exc)
    except Exception as exc:
        return CheckResult(name, CheckStatus.FAIL, f"cannot access {repo}: {_short(exc)}")
    permissions = getattr(gh_repo, "permissions", None)
    can_push = bool(getattr(permissions, "push", False))
    default_branch = getattr(gh_repo, "default_branch", "main")
    if not can_push:
        return CheckResult(
            name,
            CheckStatus.WARN,
            f"{repo} reachable but the token has no push access — operatives cannot open PRs",
            hint="Use a token with 'repo' scope from an account with write access",
        )
    return CheckResult(name, CheckStatus.OK, f"{repo} reachable, push access OK (default branch: {default_branch})")


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def check_slack_bot_token(token: str, *, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Verify a Slack bot token via ``auth.test``."""
    name = "Slack bot token"
    if not token:
        return CheckResult(
            name, CheckStatus.FAIL, "no bot token set", hint="Set HENCHMEN_SLACK_BOT_TOKEN (starts with xoxb-)"
        )
    try:
        info = _slack_client(token, timeout).auth_test()
    except ImportError as exc:
        return _sdk_missing(name, "slack", exc)
    except Exception as exc:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"rejected: {_slack_error_code(exc)}",
            hint="Reinstall the app and copy the Bot User OAuth Token",
        )
    user = info.get("user", "?")
    team = info.get("team", "?")
    return CheckResult(name, CheckStatus.OK, f"authenticated as @{user} in workspace {team}")


@dataclass(frozen=True)
class SlackIdentity:
    """Stable ids for a bot token's workspace and bot user -- never a display name.

    Display names (``check_slack_bot_token``'s ``@user in workspace team``)
    can collide across two different workspaces or change on a rename;
    ``team_id``/``user_id`` cannot, so callers that must notice "this token
    now points at a different workspace or bot user" fingerprint on these
    instead.
    """

    team_id: str
    user_id: str
    bot_id: str


def slack_bot_identity(token: str, *, timeout: float = DEFAULT_TIMEOUT) -> SlackIdentity | None:
    """``auth.test``'s ``team_id``/``user_id``/``bot_id``; ``None`` on any failure or missing id.

    A second ``auth.test`` call alongside :func:`check_slack_bot_token` rather
    than a change to its return shape, so every existing caller and test of
    ``check_slack_bot_token`` is unaffected. Fails closed: a caller comparing
    identities across two calls must treat ``None`` as "cannot prove this is
    the same workspace", never as "unchanged".
    """
    try:
        info = _slack_client(token, timeout).auth_test()
    except Exception:
        return None
    team_id = str(info.get("team_id", ""))
    user_id = str(info.get("user_id", ""))
    if not team_id or not user_id:
        return None
    return SlackIdentity(team_id=team_id, user_id=user_id, bot_id=str(info.get("bot_id", "")))


def check_slack_app_token(app_token: str, *, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Verify a Slack app-level token (Socket Mode) via ``apps.connections.open``."""
    name = "Slack app token"
    if not app_token:
        return CheckResult(
            name, CheckStatus.FAIL, "no app-level token set", hint="Set HENCHMEN_SLACK_APP_TOKEN (starts with xapp-)"
        )
    if not app_token.startswith("xapp-"):
        return CheckResult(name, CheckStatus.FAIL, "app-level tokens start with xapp- (bot tokens start with xoxb-)")
    try:
        _slack_client(app_token, timeout).apps_connections_open(app_token=app_token)
    except ImportError as exc:
        return _sdk_missing(name, "slack", exc)
    except Exception as exc:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"rejected: {_slack_error_code(exc)}",
            hint="Enable Socket Mode and generate an app-level token with the connections:write scope",
        )
    return CheckResult(name, CheckStatus.OK, "Socket Mode token valid")


@dataclass(frozen=True)
class SlackChannelListing:
    """Channels visible to the bot, and whether ``MAX_LIST_PAGES`` cut the listing short."""

    channels: list[SlackChannel]
    truncated: bool


def list_slack_channels_page(token: str, *, timeout: float = DEFAULT_TIMEOUT) -> SlackChannelListing:
    """Return every non-archived channel visible to the bot, sorted by name, and a truncation flag.

    Stops after ``MAX_LIST_PAGES`` pages rather than trusting a cursor to
    eventually come back empty; ``truncated`` is true when that limit was
    reached, so a channel beyond this listing can still be confirmed
    directly by id with :func:`get_slack_channel`. Raises
    :class:`SlackScopeError` when the token lacks ``channels:read`` /
    ``groups:read``, and :class:`SlackUnreachableError` when a page request
    fails with no Slack response at all (a connection-level failure, once the
    SDK itself is present). A missing SDK still returns an empty,
    non-truncated listing -- unchanged so :func:`list_slack_channels`'s
    existing callers (``henchmen init``/``doctor``) keep falling back to
    manual input rather than crashing.
    """
    try:
        client = _slack_client(token, timeout)
    except ImportError:
        return SlackChannelListing(channels=[], truncated=False)
    channels: list[SlackChannel] = []
    cursor: str | None = None
    truncated = False
    try:
        for page in range(1, MAX_LIST_PAGES + 1):
            response = client.conversations_list(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
            for raw in response.get("channels", []):
                channels.append(
                    SlackChannel(
                        id=str(raw.get("id", "")),
                        name=str(raw.get("name", "")),
                        is_private=bool(raw.get("is_private", False)),
                        is_member=bool(raw.get("is_member", False)),
                    )
                )
            cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
            if page == MAX_LIST_PAGES:
                truncated = True
                _logger.warning(
                    "Slack channel listing stopped after %d pages (%d channels seen); "
                    "the workspace may have more channels than were returned",
                    MAX_LIST_PAGES,
                    len(channels),
                )
    except Exception as exc:
        if _slack_error_code(exc) == "missing_scope":
            raise SlackScopeError(_slack_needed_scope(exc) or "channels:read") from exc
        if _is_unreachable(exc):
            raise SlackUnreachableError(str(exc)) from exc
        return SlackChannelListing(channels=[], truncated=False)
    return SlackChannelListing(channels=sorted(channels, key=lambda c: c.name), truncated=truncated)


def list_slack_channels(token: str, *, timeout: float = DEFAULT_TIMEOUT) -> list[SlackChannel]:
    """Return every non-archived public/private channel visible to the bot, sorted by name.

    A thin wrapper over :func:`list_slack_channels_page` for callers
    (``henchmen init``/``doctor``) that only need the channels themselves,
    not whether ``MAX_LIST_PAGES`` truncated the listing.

    Raises :class:`SlackScopeError` when the token lacks ``channels:read`` /
    ``groups:read``; returns ``[]`` for other failures.
    """
    return list_slack_channels_page(token, timeout=timeout).channels


def get_slack_channel(token: str, channel_id: str, *, timeout: float = DEFAULT_TIMEOUT) -> SlackChannel | None:
    """Look up one channel directly (``conversations.info``), for a channel beyond a truncated listing.

    Bounded to a single call. Raises :class:`SlackScopeError` when the token
    lacks the scope, and :class:`SlackUnreachableError` when Slack could not
    be reached at all (a missing SDK, or a connection-level failure with no
    Slack response) -- distinguished from ``None``, which means Slack
    actually answered "no such channel" or "you can't see it". Only ``None``
    should draw fail-closed advice like "ask for an invite"; the other two
    are a reason to try again, not a reason to assume the channel is
    inaccessible.
    """
    try:
        client = _slack_client(token, timeout)
    except ImportError as exc:
        raise SlackUnreachableError(str(exc)) from exc
    try:
        response = client.conversations_info(channel=channel_id)
    except Exception as exc:
        code = _slack_error_code(exc)
        if code == "missing_scope":
            raise SlackScopeError(_slack_needed_scope(exc) or "channels:read") from exc
        if _is_unreachable(exc):
            raise SlackUnreachableError(str(exc)) from exc
        return None
    raw = response.get("channel") or {}
    found_id = str(raw.get("id", ""))
    if not found_id:
        return None
    return SlackChannel(
        id=found_id,
        name=str(raw.get("name", "")),
        is_private=bool(raw.get("is_private", False)),
        is_member=bool(raw.get("is_member", False)),
    )


def join_slack_channel(token: str, channel_id: str, *, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Join a public channel so the bot receives mentions there. Idempotent."""
    name = "Slack channel"
    try:
        response = _slack_client(token, timeout).conversations_join(channel=channel_id)
    except ImportError as exc:
        return _sdk_missing(name, "slack", exc)
    except Exception as exc:
        code = _slack_error_code(exc)
        if code == "already_in_channel":
            return CheckResult(name, CheckStatus.OK, f"already a member of {channel_id}")
        if code == "method_not_supported_for_channel_type":
            return CheckResult(
                name,
                CheckStatus.WARN,
                f"{channel_id} is private; bots cannot self-join private channels",
                hint="In Slack, open the channel and run: /invite @<your-bot-name>",
            )
        if code == "missing_scope":
            needed = _slack_needed_scope(exc) or "channels:join"
            return CheckResult(
                name,
                CheckStatus.FAIL,
                f"bot token is missing the '{needed}' scope",
                hint="Add the scope under OAuth & Permissions, then reinstall the app",
            )
        return CheckResult(name, CheckStatus.FAIL, f"could not join {channel_id}: {code}")
    channel_name = (response.get("channel") or {}).get("name", channel_id)
    return CheckResult(name, CheckStatus.OK, f"joined #{channel_name}")


def post_slack_message(token: str, channel_id: str, text: str, *, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Post ``text`` to a channel as the bot -- the setup guide's test message."""
    name = "Slack test message"
    try:
        _slack_client(token, timeout).chat_postMessage(channel=channel_id, text=text)
    except ImportError as exc:
        return _sdk_missing(name, "slack", exc)
    except Exception as exc:
        code = _slack_error_code(exc)
        if code == "not_in_channel":
            return CheckResult(
                name,
                CheckStatus.FAIL,
                f"the bot is not a member of {channel_id}",
                hint="In Slack, open the channel and run: /invite @Henchmen",
            )
        if code == "missing_scope":
            needed = _slack_needed_scope(exc) or "chat:write"
            return CheckResult(
                name,
                CheckStatus.FAIL,
                f"bot token is missing the '{needed}' scope",
                hint="Add the scope under OAuth & Permissions, then reinstall the app",
            )
        return CheckResult(name, CheckStatus.FAIL, f"could not post to {channel_id}: {code}")
    return CheckResult(name, CheckStatus.OK, f"posted a test message to {channel_id}")


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

_JIRA_PAGE_SIZE = 50
_JIRA_MAX_PAGES = 20

# Jira never needs the loopback-http "Docker Compose service name" exception
# ``require_secure_github_url`` allows for a fake GitHub host in end-to-end
# tests, so it is narrowed away here rather than weakening that validator.
_JIRA_LOOPBACK_NAMES = frozenset({"localhost"})


class JiraUnreachableError(Exception):
    """Raised when a Jira listing/lookup could not be completed at all.

    Covers a non-2xx response (a 5xx, a rate limit, an expired session) and a
    network-level failure (timeout, connection error) alike -- distinguished
    from a definite empty result (Jira answered 200 with no items, or 404 for
    "no such project") so a caller never reads "the call failed" as "there is
    nothing there" and offers fail-open advice (e.g. "ask a Jira admin for
    Browse access") for what is really a connectivity or rate-limit problem.
    """


@dataclass(frozen=True)
class JiraProject:
    """A Jira project the account can browse."""

    key: str
    name: str


@dataclass(frozen=True)
class JiraField:
    """A Jira issue field; ``custom`` fields are the ones webhooks send as customfield_<n>."""

    id: str
    name: str
    custom: bool


@dataclass(frozen=True)
class JiraProjectListing:
    """Projects visible to the account, and whether ``_JIRA_MAX_PAGES`` cut the listing short."""

    projects: list[JiraProject]
    truncated: bool


@dataclass(frozen=True)
class JiraIdentity:
    """A stable id for a Jira account -- never a display name.

    Display names (a check's "authenticated as Pat Manager") can be changed
    by the account holder at any time and are not guaranteed unique, so a
    caller that must notice "this token now authenticates a different Jira
    account" fingerprints on ``account_id`` instead.
    """

    account_id: str
    display_name: str


def _jira_headers(email: str, api_token: str) -> dict[str, str]:
    auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    return {"Authorization": f"Basic {auth}", "Accept": "application/json"}


def _is_loopback_host(host: str) -> bool:
    """``localhost``, or any address in ``127.0.0.0/8``/``::1`` written as a canonical IP literal."""
    if host in _JIRA_LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_jira_base_url(value: str) -> str:
    """A usable Jira site address, or ``ValueError`` (ruling F6).

    Reuses (never re-implements) :func:`~henchmen.config.settings.require_secure_github_url`
    for the https-except-loopback and no-embedded-credentials rules, then
    narrows further for Jira specifically:

    * that validator's loopback-*or*-Docker-Compose-service-name exception
      (added for a fake GitHub host in end-to-end tests) is narrowed to
      loopback only -- a real Jira site is never a bare single-label
      hostname reached over plain http;
    * a query string, a fragment or (legacy) URL parameters are refused -- a
      Jira site address is an origin plus, at most, a path, never something
      with its own ``?``/``#``/``;`` component.
    """
    checked = require_secure_github_url(value)
    parts = urlparse(checked)
    if parts.query or parts.fragment or parts.params:
        raise ValueError("must not contain a query string, a fragment or URL parameters")
    host = (parts.hostname or "").lower()
    if parts.scheme == "http" and not _is_loopback_host(host):
        raise ValueError("must use https (plain http is allowed only for loopback)")
    return checked


_JIRA_STATUS_PROBLEMS: dict[int, tuple[str, str]] = {
    401: (
        "the email or API token is wrong",
        "Create a new API token at id.atlassian.com/manage-profile/security/api-tokens",
    ),
    403: (
        "this account is signed in but has no access to this Jira site",
        "Ask a Jira admin to grant this account access to the site",
    ),
    404: ("no Jira site at that address", "Check the site URL"),
    429: ("Jira is rate-limiting this account", "Wait a moment, then try again"),
}


def _jira_status_result(base_url: str, status_code: int) -> CheckResult:
    """Distinguish why ``GET /rest/api/3/myself`` failed rather than one "returned HTTP <code>" message (ruling F3).

    Collapsing 401 (bad credentials), 403 (no site access), 404 (no such
    site) and 429 (rate limited) into a single shape sends the user to create
    a new API token even when the real problem is a typo'd site address, a
    permission request to a Jira admin, or nothing more than "try again in a
    minute".
    """
    reason, hint = _JIRA_STATUS_PROBLEMS.get(
        status_code,
        (
            f"returned HTTP {status_code}",
            "Create an API token at id.atlassian.com/manage-profile/security/api-tokens",
        ),
    )
    return CheckResult("Jira", CheckStatus.FAIL, redact(f"{base_url}: {reason}"), hint=hint)


def check_jira(base_url: str, email: str, api_token: str, *, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Verify Jira credentials via ``GET /rest/api/3/myself``."""
    name = "Jira"
    if not base_url or not email or not api_token:
        return CheckResult(name, CheckStatus.FAIL, "base URL, email and API token are all required")
    url = f"{base_url.rstrip('/')}/rest/api/3/myself"
    try:
        with _http_client(timeout) as client:
            response = client.get(url, headers=_jira_headers(email, api_token))
    except Exception as exc:
        return CheckResult(name, CheckStatus.FAIL, f"cannot reach {base_url}: {_short(exc)}")
    if response.status_code != 200:
        return _jira_status_result(base_url, response.status_code)
    try:
        display_name = str(response.json().get("displayName", email))
    except Exception:
        display_name = email
    return CheckResult(name, CheckStatus.OK, f"authenticated as {display_name}")


def jira_identity(
    base_url: str, email: str, api_token: str, *, timeout: float = DEFAULT_TIMEOUT
) -> JiraIdentity | None:
    """``/rest/api/3/myself``'s ``accountId``/``displayName``; ``None`` on any failure or missing id.

    A second call to the same endpoint :func:`check_jira` already uses,
    alongside it -- the same pattern as :func:`slack_bot_identity` alongside
    :func:`check_slack_bot_token` -- rather than a change to ``check_jira``'s
    return shape, so every existing caller and test of ``check_jira`` stays
    unaffected. Fails closed: a caller comparing identities across two calls
    must treat ``None`` as "cannot prove this is the same account", never as
    "unchanged".
    """
    url = f"{base_url.rstrip('/')}/rest/api/3/myself"
    try:
        with _http_client(timeout) as client:
            response = client.get(url, headers=_jira_headers(email, api_token))
        if response.status_code != 200:
            return None
        raw = response.json()
    except Exception:
        return None
    account_id = str(raw.get("accountId", ""))
    if not account_id:
        return None
    return JiraIdentity(account_id=account_id, display_name=str(raw.get("displayName", "")))


def list_jira_projects(
    base_url: str, email: str, api_token: str, *, timeout: float = DEFAULT_TIMEOUT
) -> JiraProjectListing:
    """Projects the account can browse (``GET /rest/api/3/project/search``), sorted by key.

    Stops after ``_JIRA_MAX_PAGES`` (20) pages of ``_JIRA_PAGE_SIZE`` (50)
    each rather than trusting ``isLast`` to eventually turn true; ``truncated``
    is true when that limit was reached, so a project beyond this listing can
    still be confirmed directly by key with :func:`get_jira_project`. Raises
    :class:`JiraUnreachableError` on any non-2xx response or network failure
    (ruling F4) -- never an empty listing for that case.
    """
    url = f"{base_url.rstrip('/')}/rest/api/3/project/search"
    projects: list[JiraProject] = []
    truncated = False
    try:
        with _http_client(timeout) as client:
            for page in range(_JIRA_MAX_PAGES):
                response = client.get(
                    url,
                    params={"startAt": page * _JIRA_PAGE_SIZE, "maxResults": _JIRA_PAGE_SIZE, "orderBy": "key"},
                    headers=_jira_headers(email, api_token),
                )
                if response.status_code != 200:
                    raise JiraUnreachableError(f"project listing returned HTTP {response.status_code}")
                body = response.json()
                values = body.get("values", [])
                for raw in values:
                    key = str(raw.get("key", ""))
                    if key:
                        projects.append(JiraProject(key=key, name=str(raw.get("name") or key)))
                if body.get("isLast", True) or not values:
                    break
            else:
                truncated = True
                _logger.warning(
                    "Jira project listing stopped after %d pages (%d projects seen); "
                    "the site may have more projects than were returned",
                    _JIRA_MAX_PAGES,
                    len(projects),
                )
    except JiraUnreachableError:
        raise
    except Exception as exc:
        raise JiraUnreachableError(_short(exc)) from exc
    return JiraProjectListing(projects=sorted(projects, key=lambda project: project.key), truncated=truncated)


def get_jira_project(
    base_url: str, email: str, api_token: str, project_key: str, *, timeout: float = DEFAULT_TIMEOUT
) -> JiraProject | None:
    """Look up one project directly (``GET /rest/api/3/project/{projectIdOrKey}``), for beyond a truncated listing.

    Bounded to a single call, the same pattern as :func:`get_slack_channel`.
    Returns ``None`` only for a definite "no" answer -- Jira responds 404 both
    for "no such project" and "you can't see this project" (it deliberately
    does not distinguish the two, to avoid revealing existence). Raises
    :class:`JiraUnreachableError` for anything else -- a network failure or an
    unexpected status -- so that case is never confused with "no such project".
    """
    url = f"{base_url.rstrip('/')}/rest/api/3/project/{project_key}"
    try:
        with _http_client(timeout) as client:
            response = client.get(url, headers=_jira_headers(email, api_token))
    except Exception as exc:
        raise JiraUnreachableError(_short(exc)) from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise JiraUnreachableError(f"project lookup returned HTTP {response.status_code}")
    try:
        raw = response.json()
    except Exception as exc:
        raise JiraUnreachableError(_short(exc)) from exc
    key = str(raw.get("key", ""))
    if not key:
        return None
    return JiraProject(key=key, name=str(raw.get("name") or key))


def list_jira_fields(base_url: str, email: str, api_token: str, *, timeout: float = DEFAULT_TIMEOUT) -> list[JiraField]:
    """Every issue field (``GET /rest/api/3/field``), sorted by display name.

    Jira does not paginate this endpoint (it always returns the complete
    field list in one response), so there is no truncation to report here --
    unlike :func:`list_jira_projects`. Raises :class:`JiraUnreachableError` on
    any non-2xx response or network failure (ruling F4).
    """
    url = f"{base_url.rstrip('/')}/rest/api/3/field"
    try:
        with _http_client(timeout) as client:
            response = client.get(url, headers=_jira_headers(email, api_token))
        if response.status_code != 200:
            raise JiraUnreachableError(f"field listing returned HTTP {response.status_code}")
        raw_fields = response.json()
    except JiraUnreachableError:
        raise
    except Exception as exc:
        raise JiraUnreachableError(_short(exc)) from exc
    fields = [
        JiraField(
            id=str(raw.get("id", "")), name=str(raw.get("name") or raw.get("id", "")), custom=bool(raw.get("custom"))
        )
        for raw in raw_fields
        if isinstance(raw, dict) and raw.get("id")
    ]
    return sorted(fields, key=lambda field: field.name.lower())


__all__ = [
    "DEFAULT_TIMEOUT",
    "RECOMMENDED_OLLAMA_MODELS",
    "VERTEX_MODELS",
    "CheckResult",
    "CheckStatus",
    "JiraField",
    "JiraIdentity",
    "JiraProject",
    "JiraProjectListing",
    "JiraUnreachableError",
    "SlackChannel",
    "SlackChannelListing",
    "SlackIdentity",
    "SlackScopeError",
    "SlackUnreachableError",
    "check_anthropic_key",
    "check_bedrock",
    "check_github_repo",
    "check_github_token",
    "check_jira",
    "check_ollama",
    "check_openai_key",
    "check_slack_app_token",
    "check_slack_bot_token",
    "check_vertex",
    "filter_openai_models",
    "get_jira_project",
    "get_slack_channel",
    "jira_identity",
    "join_slack_channel",
    "list_anthropic_models",
    "list_bedrock_models",
    "list_jira_fields",
    "list_jira_projects",
    "list_ollama_models",
    "list_openai_models",
    "list_slack_channels",
    "list_slack_channels_page",
    "post_slack_message",
    "slack_bot_identity",
    "validate_jira_base_url",
]
