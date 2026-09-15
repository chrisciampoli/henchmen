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

import logging
import os
import re
import secrets
import socket
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from pydantic import BaseModel, ConfigDict, Field

from henchmen.console.state import SetupStep
from henchmen.console.steps import STEP_ROUTE_PREFIX
from henchmen.utils.endpoints import EndpointError, GitHubEndpoints, resolve_github_endpoints
from henchmen.utils.github_auth import api_headers, github_error_detail, github_json_headers

__all__ = ["EndpointError", "GitHubEndpoints"]

logger = logging.getLogger(__name__)

MANIFEST_PURPOSE = "github-manifest"
INSTALL_PURPOSE = "github-install"
_STEP_PATH = f"{STEP_ROUTE_PREFIX}/{SetupStep.GITHUB.value}"
MANIFEST_CALLBACK_PATH = f"{_STEP_PATH}/manifest-callback"
INSTALLED_CALLBACK_PATH = f"{_STEP_PATH}/installed"
# Each App's key has its own file, ``github-app-<app id>.pem``: a reconnect writes the new
# App's key beside the one the running process still signs with, never over it.
PRIVATE_KEY_FILE_PREFIX = "github-app"
_KEY_FILE_RE = re.compile(r"github-app(?:-\d{1,20})?\.pem")
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
_APP_ID_RE = re.compile(r"\d{1,20}")
_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_REPO_PAGE_SIZE = 100
MAX_REPO_PAGES = 10


class GitHubAppApiError(RuntimeError):
    """GitHub did not do what the setup flow asked; the message is safe to log."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


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
    app_id: str = Field(default="", description="ID of the installed App")

    def missing_write_permissions(self) -> list[str]:
        """Required permissions not granted at ``write``."""
        return [name for name in REQUIRED_WRITE_PERMISSIONS if self.permissions.get(name) != "write"]

    def belongs_to(self, *, app_id: str, slug: str) -> bool:
        """True when GitHub says this installation is of the App ``app_id`` / ``slug``.

        When GitHub's answer carries an ``app_id``, that id alone decides (a
        saved slug can be stale; the App id cannot be renamed). Only an answer
        without an ``app_id`` is matched on its slug (case-insensitively). An
        expected value that is blank never matches: an installation id from a
        query string is never trusted on its own.
        """
        if self.app_id:
            return bool(app_id.strip()) and self.app_id == app_id.strip()
        wanted_slug = slug.strip().lower()
        return bool(self.app_slug) and bool(wanted_slug) and self.app_slug.lower() == wanted_slug


class InstalledRepository(BaseModel):
    """A repository the installation can access."""

    model_config = ConfigDict(frozen=True)

    full_name: str = Field(..., description="owner/name")
    default_branch: str = Field(default="main", description="Default branch")
    private: bool = Field(default=False, description="Whether the repository is private")


class RepositoryListing(BaseModel):
    """The repositories an installation can access, as far as the bounded listing read."""

    model_config = ConfigDict(frozen=True)

    repositories: list[InstalledRepository] = Field(default_factory=list, description="Sorted by full name")
    truncated: bool = Field(default=False, description="True when the installation can access more than listed")


def github_endpoints(config_file: Path, *, seeded_env: Mapping[str, str] | None = None) -> GitHubEndpoints:
    """The GitHub endpoints the configuration names right now.

    Uncached and independent of whether the full ``Settings`` validates
    (:func:`henchmen.utils.endpoints.resolve_github_endpoints`). An invalid URL
    raises :class:`EndpointError`; there is no fallback to github.com.
    """
    return resolve_github_endpoints(config_file, seeded_env=seeded_env)


def private_key_file_name(app_id: str) -> str:
    """``github-app-<app id>.pem``; ``app_id`` must be the digits GitHub assigned."""
    if not isinstance(app_id, str) or not _APP_ID_RE.fullmatch(app_id):
        raise ValueError("a GitHub App id is digits only")
    return f"{PRIVATE_KEY_FILE_PREFIX}-{app_id}.pem"


def remove_unreferenced_app_keys(secrets_dir: Path, referenced: Iterable[str | Path]) -> list[Path]:
    """Delete GitHub App key files in ``secrets_dir`` that no configuration references; best effort.

    Only regular files named ``github-app.pem`` or ``github-app-<digits>.pem``
    are considered, never a symbolic link or anything else in the directory.
    A file whose resolved path matches any ``referenced`` path is always kept.
    Failures are logged (file name and error type) and skipped. Returns the
    removed paths.
    """
    keep: set[Path] = set()
    for item in referenced:
        text = str(item).strip()
        if text:
            try:
                keep.add(Path(text).resolve())
            except OSError:
                continue
    removed: list[Path] = []
    try:
        candidates = sorted(secrets_dir.iterdir())
    except OSError:
        return removed
    for candidate in candidates:
        if not _KEY_FILE_RE.fullmatch(candidate.name):
            continue
        try:
            info = os.lstat(candidate)
            if not stat.S_ISREG(info.st_mode) or candidate.resolve() in keep:
                continue
            candidate.unlink()
        except OSError as exc:
            logger.warning("Could not remove unused GitHub App key %s (%s)", candidate.name, type(exc).__name__)
            continue
        logger.info("Removed unused GitHub App key %s", candidate.name)
        removed.append(candidate)
    return removed


def remove_unused_app_keys_at_startup(config_file: Path, secrets_dir: Path, effective_key_path: str) -> list[Path]:
    """Run-mode startup housekeeping: remove GitHub App keys that nothing references any more.

    Called by ``henchmen serve`` once, before any service is built -- so no
    running process can still be signing with a key this removes (a reconnect
    leaves the previous App's ``github-app-<id>.pem`` behind until then). Keeps
    the key named by the config file's ``HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH``
    and by the effective ``Settings.github_app_private_key_path`` (the
    environment can outrank the file). A relative reference is kept both as
    resolved from the working directory and as resolved against the config
    file's folder, so a hand-written ``secrets/github-app-<id>.pem`` never
    loses its key whichever way it is read. Never raises: a problem is logged
    and nothing is removed.
    """
    from henchmen.cli.envfile import EnvFile

    try:
        file_reference = EnvFile.load(config_file).get("HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH")
        references: list[str | Path] = []
        for reference in (file_reference, effective_key_path):
            text = str(reference).strip()
            if not text:
                continue
            references.append(text)
            if not Path(text).is_absolute():
                references.append(config_file.parent / text)
        return remove_unreferenced_app_keys(secrets_dir, references)
    except Exception as exc:  # housekeeping must never stop the server from starting
        logger.warning("Could not clean up unused GitHub App keys (%s)", type(exc).__name__)
        return []


def _is_rsa_private_key(pem: str) -> bool:
    """True when ``pem`` parses as an unencrypted RSA private key (GitHub App keys are RSA)."""
    try:
        key = load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception:  # ValueError, TypeError, UnsupportedAlgorithm, ...: all mean "unusable"
        return False
    return isinstance(key, RSAPrivateKey)


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
        app_id=_id_text(body.get("app_id")),
    )


def _id_text(raw: object) -> str:
    """A numeric GitHub id as text; ``""`` for anything else (a bool, a float, a missing value)."""
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        return ""
    text = str(raw)
    return text if _APP_ID_RE.fullmatch(text) else ""


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
    if not (_APP_ID_RE.fullmatch(app_id) and is_valid_slug(slug) and _is_rsa_private_key(pem)):
        raise GitHubAppApiError("GitHub's app details were incomplete")
    if any(character in webhook_secret for character in ("\n", "\r", "\x00")):
        raise GitHubAppApiError("GitHub's app details were unreadable")
    owner = body.get("owner")
    raw_login = _string_field(owner, "login") if isinstance(owner, dict) else ""
    owner_login = raw_login if _LOGIN_RE.fullmatch(raw_login) else ""
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
) -> RepositoryListing:
    """``GET /installation/repositories`` with an installation token, following at most ``MAX_REPO_PAGES`` pages.

    ``truncated`` is true when the last page fetched was still full at the page
    bound, or when GitHub's ``total_count`` exceeds what was returned.
    """
    repositories: list[InstalledRepository] = []
    total_count = 0
    truncated = True
    for page in range(1, MAX_REPO_PAGES + 1):
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
        raw_total = body.get("total_count") if isinstance(body, dict) else None
        if isinstance(raw_total, int) and not isinstance(raw_total, bool):
            total_count = max(total_count, raw_total)
        if not isinstance(items, list):
            items = []
        for item in items:
            repository = _repository(item)
            if repository is not None:
                repositories.append(repository)
        if len(items) < _REPO_PAGE_SIZE:
            truncated = False
            break
    return RepositoryListing(
        repositories=sorted(repositories, key=lambda repository: repository.full_name.lower()),
        truncated=truncated or total_count > len(repositories),
    )


def _repository(item: object) -> InstalledRepository | None:
    if not isinstance(item, dict) or not isinstance(item.get("full_name"), str) or not item["full_name"]:
        return None
    return InstalledRepository(
        full_name=item["full_name"],
        default_branch=str(item.get("default_branch") or "main"),
        private=bool(item.get("private", False)),
    )


async def get_installation_repository(
    client: httpx.AsyncClient, api_url: str, installation_token: str, full_name: str, account_login: str
) -> InstalledRepository | None:
    """``GET /repos/{owner}/{name}`` with an installation token: the repository if the installation can see it.

    Used when the listing was truncated. ``None`` unless GitHub answers 200 for
    exactly that repository and its owner is the installation's account
    ``account_login`` (case-insensitive). Other failures raise.
    """
    owner, _, name = full_name.partition("/")
    if not owner or not name or "/" in name or name in {".", ".."} or not account_login:
        return None
    response = await _send(
        client,
        "GET",
        f"{api_url}/repos/{quote(owner, safe='')}/{quote(name, safe='')}",
        headers=api_headers(installation_token),
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise GitHubAppApiError(
            f"Could not read the repository ({github_error_detail(response)})", response.status_code
        )
    body = _json(response)
    repository = _repository(body)
    raw_owner = body.get("owner") if isinstance(body, dict) else None
    login = raw_owner.get("login") if isinstance(raw_owner, dict) else None
    if (
        repository is None
        or repository.full_name.lower() != full_name.lower()
        or not isinstance(login, str)
        or login.lower() != account_login.lower()
    ):
        return None
    return repository


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
