"""GitHub App setup for the Console: the manifest and the GitHub REST calls the GitHub step makes.

The manifest follows spec §5.1 with amendment A4: a private App named
``Henchmen (<machine>)`` with a random suffix (App names are global on GitHub,
limited to 34 characters), redirect and setup URLs on this Console, contents,
pull requests and issues write, metadata, checks and actions read -- never
workflows -- and the webhook disabled (laptops poll instead).

REST helpers take an ``httpx.AsyncClient`` so tests drive them with a
``MockTransport``. They raise :class:`GitHubAppApiError` with a message that
is safe to log: GitHub's own error text goes through
:func:`henchmen.utils.github_auth.github_error_detail` (redacted, truncated)
and a response body is never quoted otherwise. Credentials returned by GitHub
(private key, webhook secret) are excluded from model ``repr`` so they cannot
reach a log by accident.

The callback paths are derived from ``STEP_ROUTE_PREFIX`` so the exact-match
public-path check in the Console guard can never drift from where the step
router is mounted (ruling M-13).
"""

from __future__ import annotations

import re
import secrets
import socket
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field

from henchmen.console.state import SetupStep
from henchmen.console.steps import STEP_ROUTE_PREFIX
from henchmen.utils.github_auth import (
    GITHUB_API_URL,
    GITHUB_WEB_URL,
    api_headers,
    github_error_detail,
    github_json_headers,
)

MANIFEST_PURPOSE = "github-manifest"
INSTALL_PURPOSE = "github-install"
_STEP_PATH = f"{STEP_ROUTE_PREFIX}/{SetupStep.GITHUB.value}"
MANIFEST_CALLBACK_PATH = f"{_STEP_PATH}/manifest-callback"
INSTALLED_CALLBACK_PATH = f"{_STEP_PATH}/installed"
PRIVATE_KEY_FILE_NAME = "github-app.pem"
HOMEPAGE_URL = "https://github.com/chrisciampoli/henchmen"
# Checks and Actions are read by CI feedback (forge/error_extractor.py, MastermindAgent._build_dossier).
# Never "workflows": operatives must not change CI workflows, so GitHub refuses such pushes (A4).
APP_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "metadata": "read",
    "checks": "read",
    "actions": "read",
}
REQUIRED_WRITE_PERMISSIONS: tuple[str, ...] = ("contents", "pull_requests")

_APP_NAME_LIMIT = 34
_APP_NAME_PREFIX = "Henchmen ("
_CODE_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")
_INSTALLATION_ID_RE = re.compile(r"\d{1,20}")
_REPO_PAGE_SIZE = 100
_MAX_REPO_PAGES = 10


class GitHubAppApiError(RuntimeError):
    """GitHub did not do what the setup flow asked; the message is safe to log."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubEndpoints(BaseModel):
    """Where the GitHub API and web UI live (github.com unless a test fake overrides it)."""

    model_config = ConfigDict(frozen=True)

    api_url: str = Field(..., description="REST API base URL, no trailing slash")
    web_url: str = Field(..., description="Web base URL, no trailing slash")


class AppConversion(BaseModel):
    """What GitHub returns when a manifest code is converted into an App."""

    model_config = ConfigDict(frozen=True)

    app_id: str = Field(..., description="Numeric App ID")
    slug: str = Field(..., description="App slug used in URLs")
    pem: str = Field(..., repr=False, description="PEM private key")
    webhook_secret: str = Field(default="", repr=False, description="Webhook secret")
    owner_login: str = Field(default="", description="Account that owns the App")


class Installation(BaseModel):
    """An installation of the App on a user or organisation account."""

    model_config = ConfigDict(frozen=True)

    installation_id: str = Field(..., description="Installation ID")
    account_login: str = Field(..., description="Account the App is installed on")
    account_type: str = Field(default="", description="User or Organization")
    permissions: dict[str, str] = Field(default_factory=dict, description="Granted permissions")
    app_slug: str = Field(default="", description="Slug of the installed App")

    def missing_write_permissions(self) -> list[str]:
        """Required permissions not granted at ``write``."""
        return [name for name in REQUIRED_WRITE_PERMISSIONS if self.permissions.get(name) != "write"]


class InstalledRepository(BaseModel):
    """A repository the installation can access."""

    model_config = ConfigDict(frozen=True)

    full_name: str = Field(..., description="owner/name")
    default_branch: str = Field(default="main", description="Default branch")
    private: bool = Field(default=False, description="Whether the repository is private")


def github_endpoints() -> GitHubEndpoints:
    """GitHub endpoints from ``Settings.github_api_url``/``github_web_url``.

    Falls back to github.com when Settings cannot be built (setup mode, before
    the configuration is complete). Settings already refuses an insecure or
    credential-carrying URL, so the fallback can only ever point at github.com,
    never at an unvalidated host.
    """
    from henchmen.config.settings import get_settings

    try:
        settings = get_settings()
    except ValueError:
        return GitHubEndpoints(api_url=GITHUB_API_URL, web_url=GITHUB_WEB_URL)
    return GitHubEndpoints(
        api_url=settings.github_api_url.strip().rstrip("/") or GITHUB_API_URL,
        web_url=settings.github_web_url.strip().rstrip("/") or GITHUB_WEB_URL,
    )


def is_valid_code(code: str) -> bool:
    """True for a manifest ``code`` that can be placed in a URL path as is."""
    return isinstance(code, str) and bool(_CODE_RE.fullmatch(code))


def is_valid_slug(slug: str) -> bool:
    """True for a GitHub App slug (lowercase letters, digits and hyphens)."""
    return isinstance(slug, str) and bool(_SLUG_RE.fullmatch(slug))


def default_machine_name() -> str:
    """This machine's host name (inside a container, the container's)."""
    return socket.gethostname()


def app_name(machine_name: str, suffix: str) -> str:
    """``Henchmen (<machine>-<suffix>)`` trimmed to GitHub's 34-character limit."""
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "-", machine_name).strip("-")
    room = _APP_NAME_LIMIT - len(_APP_NAME_PREFIX) - len(")") - len(suffix) - 1
    trimmed = cleaned[: max(room, 0)].rstrip("-")
    label = f"{trimmed}-{suffix}" if trimmed else suffix
    return f"{_APP_NAME_PREFIX}{label})"


def build_manifest(*, console_base_url: str, machine_name: str, suffix: str | None = None) -> dict[str, Any]:
    """The GitHub App manifest for this Console (spec §5.1)."""
    base = console_base_url.rstrip("/")
    return {
        "name": app_name(machine_name, suffix or secrets.token_hex(2)),
        "url": HOMEPAGE_URL,
        "description": "Henchmen opens pull requests for the tasks you give it.",
        "public": False,
        "redirect_url": f"{base}{MANIFEST_CALLBACK_PATH}",
        "setup_url": f"{base}{INSTALLED_CALLBACK_PATH}",
        "setup_on_update": False,
        "request_oauth_on_install": False,
        "hook_attributes": {"url": f"{base}/dispatch/webhooks/github", "active": False},
        "default_permissions": dict(APP_PERMISSIONS),
        "default_events": [],
    }


def manifest_form_action(web_url: str, organization: str | None, state: str) -> str:
    """Where the browser POSTs the manifest: the personal or the organisation App creation page."""
    query = urlencode({"state": state})
    if organization:
        return f"{web_url}/organizations/{quote(organization, safe='')}/settings/apps/new?{query}"
    return f"{web_url}/settings/apps/new?{query}"


def org_admin_request(web_url: str, organization: str) -> str:
    """Copyable request for an organisation owner when the user cannot create Apps there."""
    return (
        f"Could you create the Henchmen GitHub App for the {organization} organisation, or make me a "
        f"GitHub App manager at {web_url}/organizations/{quote(organization, safe='')}/settings/roles? "
        "Henchmen needs it to open pull requests on our repositories."
    )


def installation_url(web_url: str, slug: str, state: str) -> str:
    """The App's install page; GitHub returns ``state`` to the setup URL."""
    return f"{web_url}/apps/{quote(slug, safe='')}/installations/new?{urlencode({'state': state})}"


def installation_settings_url(web_url: str, installation: Installation) -> str:
    """Where an account owner changes the installation's repositories and permissions."""
    login = quote(installation.account_login, safe="")
    installation_id = quote(installation.installation_id, safe="")
    if installation.account_type == "Organization":
        return f"{web_url}/organizations/{login}/settings/installations/{installation_id}"
    return f"{web_url}/settings/installations/{installation_id}"


async def _send(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        return await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        # Only the exception type: its text can include the request URL (with a manifest code).
        raise GitHubAppApiError(f"Could not reach GitHub ({type(exc).__name__})") from None


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise GitHubAppApiError(f"GitHub returned an unreadable response (HTTP {response.status_code})") from None


def _installation(body: Any) -> Installation:
    if not isinstance(body, dict):
        raise GitHubAppApiError("GitHub returned an unreadable installation")
    raw_account = body.get("account")
    raw_permissions = body.get("permissions")
    account: dict[str, Any] = raw_account if isinstance(raw_account, dict) else {}
    permissions: dict[str, Any] = raw_permissions if isinstance(raw_permissions, dict) else {}
    return Installation(
        installation_id=str(body.get("id", "")),
        account_login=str(account.get("login", "")),
        account_type=str(account.get("type", "")),
        permissions={str(key): str(value) for key, value in permissions.items()},
        app_slug=str(body.get("app_slug", "")),
    )


async def convert_manifest(client: httpx.AsyncClient, api_url: str, code: str) -> AppConversion:
    """``POST /app-manifests/{code}/conversions`` (no authentication; the code is the credential).

    The response carries the App's private key and webhook secret; neither is
    ever put into an exception message or log line.
    """
    if not is_valid_code(code):
        raise GitHubAppApiError("The code GitHub returned is malformed")
    response = await _send(client, "POST", f"{api_url}/app-manifests/{code}/conversions", headers=github_json_headers())
    if response.status_code != 201:
        raise GitHubAppApiError(
            f"GitHub did not create the app ({github_error_detail(response)})", response.status_code
        )
    body = _json(response)
    if not isinstance(body, dict):
        raise GitHubAppApiError("GitHub's app details were incomplete")
    raw_id = body.get("id")
    app_id = str(raw_id) if isinstance(raw_id, int | str) and not isinstance(raw_id, bool) else ""
    slug = _string_field(body, "slug")
    pem = _string_field(body, "pem")
    webhook_secret = _string_field(body, "webhook_secret")
    if not (app_id.isascii() and app_id.isdigit() and is_valid_slug(slug) and "PRIVATE KEY" in pem):
        raise GitHubAppApiError("GitHub's app details were incomplete")
    if any(character in webhook_secret for character in ("\n", "\r", "\x00")):
        raise GitHubAppApiError("GitHub's app details were unreadable")
    owner = body.get("owner")
    owner_login = _string_field(owner, "login") if isinstance(owner, dict) else ""
    return AppConversion(
        app_id=app_id,
        slug=slug,
        pem=pem,
        webhook_secret=webhook_secret,
        owner_login=owner_login,
    )


def _string_field(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    return value if isinstance(value, str) else ""


async def get_installation(
    client: httpx.AsyncClient, api_url: str, app_jwt: str, installation_id: str
) -> Installation | None:
    """``GET /app/installations/{id}`` with the app JWT; ``None`` when it is not this App's installation."""
    if not _INSTALLATION_ID_RE.fullmatch(installation_id):
        return None
    response = await _send(
        client, "GET", f"{api_url}/app/installations/{installation_id}", headers=api_headers(app_jwt)
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise GitHubAppApiError(
            f"Could not read the installation ({github_error_detail(response)})", response.status_code
        )
    return _installation(_json(response))


async def list_installations(client: httpx.AsyncClient, api_url: str, app_jwt: str) -> list[Installation]:
    """``GET /app/installations`` with the app JWT."""
    response = await _send(
        client, "GET", f"{api_url}/app/installations", params={"per_page": 100}, headers=api_headers(app_jwt)
    )
    if response.status_code != 200:
        raise GitHubAppApiError(
            f"Could not list the app's installations ({github_error_detail(response)})", response.status_code
        )
    body = _json(response)
    return [_installation(item) for item in body] if isinstance(body, list) else []


async def get_app_slug(client: httpx.AsyncClient, api_url: str, app_jwt: str) -> str:
    """``GET /app`` with the app JWT."""
    response = await _send(client, "GET", f"{api_url}/app", headers=api_headers(app_jwt))
    if response.status_code != 200:
        raise GitHubAppApiError(f"Could not read the app ({github_error_detail(response)})", response.status_code)
    body = _json(response)
    slug = str(body.get("slug", "")) if isinstance(body, dict) else ""
    if not is_valid_slug(slug):
        raise GitHubAppApiError("GitHub returned an app without a usable name")
    return slug


async def list_installation_repositories(
    client: httpx.AsyncClient, api_url: str, installation_token: str
) -> list[InstalledRepository]:
    """``GET /installation/repositories`` with an installation token, following pages."""
    repositories: list[InstalledRepository] = []
    for page in range(1, _MAX_REPO_PAGES + 1):
        response = await _send(
            client,
            "GET",
            f"{api_url}/installation/repositories",
            params={"per_page": _REPO_PAGE_SIZE, "page": page},
            headers=api_headers(installation_token),
        )
        if response.status_code != 200:
            raise GitHubAppApiError(
                f"Could not list the installation's repositories ({github_error_detail(response)})",
                response.status_code,
            )
        body = _json(response)
        items = body.get("repositories", []) if isinstance(body, dict) else []
        if not isinstance(items, list):
            items = []
        for item in items:
            if isinstance(item, dict) and item.get("full_name"):
                repositories.append(
                    InstalledRepository(
                        full_name=str(item["full_name"]),
                        default_branch=str(item.get("default_branch") or "main"),
                        private=bool(item.get("private", False)),
                    )
                )
        if len(items) < _REPO_PAGE_SIZE:
            break
    return sorted(repositories, key=lambda repository: repository.full_name.lower())


async def bot_identity(client: httpx.AsyncClient, api_url: str, slug: str) -> tuple[str, str]:
    """Git author for the App's commits: ``<slug>[bot]`` and ``<id>+<slug>[bot]@users.noreply.github.com``."""
    if not is_valid_slug(slug):
        raise GitHubAppApiError("The GitHub App name is malformed")
    login = f"{slug}[bot]"
    response = await _send(client, "GET", f"{api_url}/users/{quote(login, safe='')}", headers=github_json_headers())
    if response.status_code != 200:
        raise GitHubAppApiError(
            f"Could not look up the app's bot account ({github_error_detail(response)})", response.status_code
        )
    body = _json(response)
    user_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise GitHubAppApiError("GitHub returned a bot account without an id")
    return login, f"{user_id}+{login}@users.noreply.github.com"
