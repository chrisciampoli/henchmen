"""GitHub credentials: GitHub App installation tokens, with the PAT as a fallback (spec §5.1, D-P11).

Every server-side component that talks to GitHub — git clone and push URLs,
PyGithub clients, REST calls, the token handed to operatives — asks this
module instead of reading ``Settings.github_token``:

* **GitHub App configured** (``github_app_id``, ``github_app_installation_id``
  and ``github_app_private_key_path`` all set): sign an RS256 JWT with the
  App's private key (``iat`` 60 s in the past to absorb clock drift, ``exp``
  9 minutes ahead, inside GitHub's 10-minute limit), exchange it at
  ``POST /app/installations/{id}/access_tokens`` and cache the installation
  token until 5 minutes before it expires. A token can be scoped to one
  repository, and a caller that keeps a token for a long time (an operative)
  asks for a minimum remaining lifetime.
* **No GitHub App** (none of the three set): return ``github_token`` (the
  PAT) exactly as before, so engineers and existing deployments keep working.
* **Partly configured App** (some but not all set): every token call raises
  :class:`GitHubAuthError` naming the missing settings; the PAT is never used.

A configured App that cannot produce a token raises :class:`GitHubAuthError`;
it never silently falls back to the PAT, and callers fail closed. The private
key never leaves the server: operatives receive installation tokens only.

Tokens, app JWTs and key material are never logged or put in an exception
message; GitHub's own error text is passed through
:func:`henchmen.utils.redaction.redact` before it is shown. The key file is
read only through :func:`load_app_private_key` (symlinks and files owned by
another user are refused), and expiry timestamps are parsed only by
:func:`parse_expiry`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
import weakref
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field

from henchmen.config.secret_files import check_secret_path
from henchmen.utils.redaction import redact

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"
GITHUB_WEB_URL = "https://github.com"
GITHUB_API_VERSION = "2022-11-28"
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 9 * 60
REFRESH_MARGIN_SECONDS = 5 * 60
# Installation tokens live one hour. The longest minimum lifetime a caller can ask for is
# 50 minutes, which leaves at least 10 minutes of tolerance for clock skew between this
# server and GitHub before a freshly minted token fails the lifetime check.
MAX_MIN_TTL_SECONDS = 50 * 60
_HTTP_TIMEOUT = 10.0
_ERROR_MESSAGE_LIMIT = 200
# ``owner/name`` with a GitHub login (letters, digits, hyphens) and a repository name.
_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
_NAME = r"[A-Za-z0-9._-]{1,100}"
_REPO_SHORT = re.compile(rf"(?P<owner>{_OWNER})/(?P<name>{_NAME})")
_REPO_HTTPS = re.compile(rf"https://[A-Za-z0-9.-]+(?::\d{{1,5}})?/(?P<owner>{_OWNER})/(?P<name>{_NAME})")
_REPO_SSH = re.compile(rf"git@[A-Za-z0-9.-]+:(?P<owner>{_OWNER})/(?P<name>{_NAME})")
# Environment names of the three App settings, in GitHubAppConfig.from_values argument order.
_APP_SETTING_NAMES = (
    "HENCHMEN_GITHUB_APP_ID",
    "HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH",
    "HENCHMEN_GITHUB_APP_INSTALLATION_ID",
)

SyncClientFactory = Callable[[], httpx.Client]
AsyncClientFactory = Callable[[], httpx.AsyncClient]


class GitHubAuthError(RuntimeError):
    """A GitHub App is configured but a usable token could not be produced.

    ``status_code`` is GitHub's HTTP status when GitHub answered the token
    request with a refusal, ``None`` otherwise (unreachable, unreadable
    response, local key or configuration problem).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubAppConfigurationError(GitHubAuthError):
    """The GitHub App settings are only partly configured; retrying cannot help until they are fixed."""


class GitHubRepositoryAccessError(GitHubAuthError):
    """GitHub will not scope a token to the requested repository: the installation cannot access it.

    Raised for GitHub's 422 refusal of a repository outside the installation's
    selection, and when GitHub scopes the token to a different repository than
    the one requested. A network error or a 5xx is a plain :class:`GitHubAuthError`.
    """


class GitHubAppKeyError(GitHubAuthError):
    """The GitHub App private key file is missing, unreadable or cannot sign; retrying cannot help until it is fixed."""


class GitHubRepositoryReferenceError(GitHubAuthError):
    """The repository reference is not ``owner/name`` or a GitHub clone URL; the same input always fails."""


class GitHubAppConfig(BaseModel):
    """The three settings that switch GitHub access to a GitHub App."""

    model_config = ConfigDict(frozen=True)

    app_id: str = Field(..., min_length=1, description="GitHub App ID")
    private_key_path: Path = Field(..., description="PEM private key file")
    installation_id: str = Field(..., min_length=1, description="Installation ID")

    @classmethod
    def from_values(cls, app_id: object, private_key_path: object, installation_id: object) -> GitHubAppConfig | None:
        """The config when all three are non-blank strings, else ``None``."""
        values = [
            value.strip() if isinstance(value, str) else "" for value in (app_id, private_key_path, installation_id)
        ]
        if not all(values):
            return None
        return cls(app_id=values[0], private_key_path=Path(values[1]), installation_id=values[2])

    @classmethod
    def from_settings(cls, settings: Settings) -> GitHubAppConfig | None:
        """The App configured in ``settings``, if any."""
        return cls.from_values(
            settings.github_app_id, settings.github_app_private_key_path, settings.github_app_installation_id
        )


def partial_app_message(app_id: object, private_key_path: object, installation_id: object) -> str | None:
    """The problem when some, but not all, of the three App settings are set; ``None`` otherwise.

    Names only the missing settings, never a value. A partly configured App is
    an error rather than "no App" (D-P11): someone set out to use a GitHub App,
    so quietly using the PAT instead would hide the mistake.
    """
    values = (app_id, private_key_path, installation_id)
    present = [isinstance(value, str) and bool(value.strip()) for value in values]
    if all(present) or not any(present):
        return None
    missing = [name for name, is_set in zip(_APP_SETTING_NAMES, present, strict=True) if not is_set]
    return f"The GitHub App is only partly configured: set {', '.join(missing)}."


class InstallationToken(BaseModel):
    """A cached installation access token (the token itself is kept out of ``repr``)."""

    model_config = ConfigDict(frozen=True)

    token: str = Field(..., repr=False, description="Installation access token")
    expires_at: float = Field(..., description="Expiry as a Unix timestamp")

    def expires_at_iso(self) -> str:
        """Expiry as an ISO 8601 UTC timestamp, e.g. ``2026-09-15T10:00:00Z``."""
        return datetime.fromtimestamp(self.expires_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def github_json_headers() -> dict[str, str]:
    """Headers every GitHub REST call sends."""
    return {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": GITHUB_API_VERSION}


def api_headers(bearer: str) -> dict[str, str]:
    """REST headers authenticated with an app JWT or an installation token."""
    return {**github_json_headers(), "Authorization": f"Bearer {bearer}"}


def github_error_detail(response: httpx.Response) -> str:
    """``HTTP <status>[: <GitHub message>]`` — redacted and truncated, safe to log and show."""
    try:
        body = response.json()
    except ValueError:
        body = None
    message = body.get("message") if isinstance(body, dict) else None
    if not message:
        return f"HTTP {response.status_code}"
    # Redact before truncating so a cut can never split a secret out of a pattern's reach,
    # then once more in case the cut created a fragment that still reads as one.
    return redact(f"HTTP {response.status_code}: {redact(str(message))[:_ERROR_MESSAGE_LIMIT]}")


def build_app_jwt(app_id: str, private_key_pem: bytes, *, now: float) -> str:
    """RS256 JWT identifying the App: ``iat`` = now − 60 s, ``exp`` = now + 9 min, ``iss`` = App ID."""
    issued_at = int(now)
    claims = {"iat": issued_at - JWT_BACKDATE_SECONDS, "exp": issued_at + JWT_LIFETIME_SECONDS, "iss": app_id}
    try:
        return jwt.encode(claims, private_key_pem, algorithm="RS256")
    except Exception:  # ValueError, TypeError, jwt.PyJWTError, cryptography's UnsupportedAlgorithm, ...
        # Any signing failure is the same fail-closed error. ``from None``: the underlying
        # cryptography error must not drag key material into a traceback.
        raise GitHubAppKeyError("The GitHub App private key could not sign a token; reconnect GitHub") from None


def load_app_private_key(path: Path) -> bytes:
    """Read the GitHub App's PEM private key; the only loader of that file.

    The file must be a regular file this user owns (``check_secret_path``: a
    symbolic link or another user's file is refused). Any problem is a
    :class:`GitHubAppKeyError` naming the path, never the key's content.
    """
    try:
        check_secret_path(path)
        return path.read_bytes()
    except OSError:
        # SecretFileError is an OSError. ``from None`` keeps the raw OS error out of the chain.
        raise GitHubAppKeyError(f"The GitHub App private key at {path} is missing or unreadable") from None


def app_jwt_for(app_id: str, private_key_path: Path, *, now: float | None = None) -> str:
    """A fresh app JWT for ``app_id``, signed with the key at ``private_key_path``.

    The key is read for this one signature and not kept: no frame or object
    holds the bytes after the call returns (and a rotated key file is picked up).
    """
    return build_app_jwt(app_id, load_app_private_key(private_key_path), now=time.time() if now is None else now)


def parse_expiry(raw: object) -> float:
    """An installation token's ``expires_at`` (ISO 8601, e.g. ``2026-09-15T10:00:00Z``) as a Unix timestamp.

    The only expiry parser: the server-side provider and the operative both use
    it. A timestamp without a zone is taken as UTC (GitHub always sends ``Z``).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise GitHubAuthError("GitHub returned an installation token without an expiry time")
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise GitHubAuthError("GitHub returned an unreadable installation token expiry time") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def parse_repository(repo: str | None) -> tuple[str, str] | None:
    """``(owner, name)`` for a repository reference; ``None`` for no repository.

    Accepted shapes, exactly two path parts each: ``owner/name``,
    ``https://<host>/owner/name[.git]`` and ``git@<host>:owner/name[.git]``.
    Anything else (a bare name, ``owner/name/tree/main``, ``http://``) raises
    :class:`GitHubRepositoryReferenceError`: an unparseable reference must never widen into a
    token for the whole installation or silently pick another repository.
    """
    if repo is None or not repo.strip():
        return None
    text = repo.strip()
    candidates = (
        (_REPO_SHORT, text),
        (_REPO_HTTPS, text.removesuffix(".git")),
        (_REPO_SSH, text.removesuffix(".git")),
    )
    for pattern, candidate in candidates:
        match = pattern.fullmatch(candidate)
        if match is not None:
            name = match["name"]
            if name in {".", ".."} or name.endswith(".git"):
                break
            return match["owner"], name
    raise GitHubRepositoryReferenceError(
        "The repository for a GitHub installation token must be owner/name or a GitHub clone URL"
    )


def _default_client() -> httpx.Client:
    # trust_env=False: no proxy or netrc credentials from the environment see the app JWT.
    return httpx.Client(timeout=_HTTP_TIMEOUT, trust_env=False)


def _default_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT, trust_env=False)


class GitHubCredentialsProvider:
    """Hands out GitHub tokens: cached installation tokens for an App, else the PAT.

    ``partial_app_problem`` (from :func:`partial_app_message`) marks a
    configuration that set some of the App settings but not all: every token
    method then raises :class:`GitHubAuthError` with that message, and the PAT
    is never returned (D-P11).

    Concurrency: a cache miss is minted under a per-cache-key lock — a
    ``threading.Lock`` for :meth:`installation_token` and an ``asyncio.Lock``
    (one per event loop) for :meth:`installation_token_async` — and the cache
    is checked again once the lock is held, so concurrent callers for the same
    repository share one mint instead of each spending an API call.
    """

    def __init__(
        self,
        *,
        app: GitHubAppConfig | None,
        pat: str = "",
        api_url: str = GITHUB_API_URL,
        client_factory: SyncClientFactory | None = None,
        async_client_factory: AsyncClientFactory | None = None,
        clock: Callable[[], float] = time.time,
        partial_app_problem: str | None = None,
    ) -> None:
        from henchmen.config.settings import require_secure_github_url

        try:
            self._api_url = require_secure_github_url(api_url or GITHUB_API_URL).rstrip("/")
        except ValueError as exc:
            raise GitHubAuthError(f"The GitHub API URL {exc}") from None
        self._app = app
        self._pat = pat
        self._partial_app_problem = partial_app_problem
        self._client_factory = client_factory or _default_client
        self._async_client_factory = async_client_factory or _default_async_client
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: dict[str, InstallationToken] = {}
        self._sync_locks: dict[str, threading.Lock] = {}
        self._async_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
            weakref.WeakKeyDictionary()
        )

    def __repr__(self) -> str:
        return f"GitHubCredentialsProvider(uses_app={self.uses_app})"

    @property
    def uses_app(self) -> bool:
        """True when tokens come from a GitHub App rather than the PAT.

        False for a partly configured App too, but every token method of such a
        provider raises, so a caller that falls back to :meth:`token` still fails closed.
        """
        return self._app is not None and self._partial_app_problem is None

    @property
    def app(self) -> GitHubAppConfig | None:
        """The GitHub App this provider mints tokens for, if any."""
        return self._app

    def app_jwt(self) -> str:
        """A fresh app JWT (for ``/app/*`` endpoints); the key file is read for this signature only."""
        app = self._require_app()
        return app_jwt_for(app.app_id, app.private_key_path, now=self._clock())

    def installation_token(
        self, repo: str | None = None, *, min_ttl_seconds: int = REFRESH_MARGIN_SECONDS
    ) -> InstallationToken:
        """An installation token for ``repo`` (or the whole installation) that stays valid long enough."""
        app = self._require_app()
        repository = parse_repository(repo)
        cache_key = _cache_key(repository)
        margin = self._required_lifetime(min_ttl_seconds)
        cached = self._cached(cache_key, margin)
        if cached is not None:
            return cached
        with self._sync_lock(cache_key):
            cached = self._cached(cache_key, margin)
            if cached is not None:
                return cached
            url, body = self._token_request(app, repository)
            headers = api_headers(self.app_jwt())
            try:
                with self._client_factory() as client:
                    response = client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise self._unreachable(exc) from None
            return self._remember(cache_key, repository, response, min_ttl_seconds)

    async def installation_token_async(
        self, repo: str | None = None, *, min_ttl_seconds: int = REFRESH_MARGIN_SECONDS
    ) -> InstallationToken:
        """Async :meth:`installation_token`."""
        app = self._require_app()
        repository = parse_repository(repo)
        cache_key = _cache_key(repository)
        margin = self._required_lifetime(min_ttl_seconds)
        cached = self._cached(cache_key, margin)
        if cached is not None:
            return cached
        async with self._async_lock(cache_key):
            cached = self._cached(cache_key, margin)
            if cached is not None:
                return cached
            url, body = self._token_request(app, repository)
            headers = api_headers(self.app_jwt())
            try:
                async with self._async_client_factory() as client:
                    response = await client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise self._unreachable(exc) from None
            return self._remember(cache_key, repository, response, min_ttl_seconds)

    def token(self, repo: str | None = None, *, min_ttl_seconds: int = REFRESH_MARGIN_SECONDS) -> str:
        """A token for ``repo``: an installation token with an App, else the PAT (maybe empty)."""
        if self._app is None and self._partial_app_problem is None:
            return self._pat
        return self.installation_token(repo, min_ttl_seconds=min_ttl_seconds).token

    async def token_async(self, repo: str | None = None, *, min_ttl_seconds: int = REFRESH_MARGIN_SECONDS) -> str:
        """Async :meth:`token` for code on the event loop."""
        if self._app is None and self._partial_app_problem is None:
            return self._pat
        return (await self.installation_token_async(repo, min_ttl_seconds=min_ttl_seconds)).token

    def invalidate(self) -> None:
        """Forget cached tokens (e.g. after reconnecting GitHub)."""
        with self._lock:
            self._tokens.clear()

    # -- internals -------------------------------------------------------------

    def _require_app(self) -> GitHubAppConfig:
        if self._partial_app_problem is not None:
            raise GitHubAppConfigurationError(self._partial_app_problem)
        if self._app is None:
            raise GitHubAuthError("No GitHub App is configured")
        return self._app

    def _sync_lock(self, cache_key: str) -> threading.Lock:
        with self._lock:
            return self._sync_locks.setdefault(cache_key, threading.Lock())

    def _async_lock(self, cache_key: str) -> asyncio.Lock:
        # asyncio.Lock binds to the loop it is first contended on, so a process that runs
        # several loops (asyncio.run more than once) gets one set of locks per loop.
        loop = asyncio.get_running_loop()
        with self._lock:
            locks = self._async_locks.get(loop)
            if locks is None:
                locks = {}
                self._async_locks[loop] = locks
            return locks.setdefault(cache_key, asyncio.Lock())

    @staticmethod
    def _required_lifetime(min_ttl_seconds: int) -> int:
        """Seconds a cached token must still be valid for: at least the refresh margin, at most the cap."""
        if min_ttl_seconds > MAX_MIN_TTL_SECONDS:
            logger.warning(
                "A GitHub token valid for %ss was requested; installation tokens last an hour, so %ss is used",
                min_ttl_seconds,
                MAX_MIN_TTL_SECONDS,
            )
        return min(max(min_ttl_seconds, REFRESH_MARGIN_SECONDS), MAX_MIN_TTL_SECONDS)

    def _cached(self, cache_key: str, margin: int) -> InstallationToken | None:
        """The cached token for ``cache_key`` if it outlives ``margin`` seconds from now."""
        with self._lock:
            cached = self._tokens.get(cache_key)
        if cached is not None and cached.expires_at - self._clock() > margin:
            return cached
        return None

    def _token_request(self, app: GitHubAppConfig, repository: tuple[str, str] | None) -> tuple[str, dict[str, Any]]:
        # GitHub's access_tokens endpoint takes repository *names*; the owner is the installation's
        # account, and the response is checked against the requested owner in _remember.
        body: dict[str, Any] = {"repositories": [repository[1]]} if repository else {}
        return f"{self._api_url}/app/installations/{app.installation_id}/access_tokens", body

    @staticmethod
    def _unreachable(exc: httpx.HTTPError) -> GitHubAuthError:
        logger.warning("Could not reach GitHub for an installation token (%s)", type(exc).__name__)
        return GitHubAuthError(f"Could not reach GitHub for an installation token ({type(exc).__name__})")

    def _remember(
        self,
        cache_key: str,
        repository: tuple[str, str] | None,
        response: httpx.Response,
        min_ttl_seconds: int,
    ) -> InstallationToken:
        scope = "/".join(repository) if repository else "the whole installation"
        if response.status_code != 201:
            detail = github_error_detail(response)
            logger.warning("GitHub refused an installation token for %s (%s)", scope, detail)
            message = f"GitHub refused to issue an installation token ({detail})"
            if response.status_code == 422 and repository is not None:
                raise GitHubRepositoryAccessError(message, status_code=422)
            raise GitHubAuthError(message, status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise GitHubAuthError("GitHub returned an unreadable installation token response") from None
        if not isinstance(payload, dict):
            raise GitHubAuthError("GitHub returned an unreadable installation token response")
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubAuthError("GitHub returned no installation token")
        if repository is not None:
            _check_token_repositories(payload.get("repositories"), repository)
        issued = InstallationToken(token=token, expires_at=parse_expiry(payload.get("expires_at")))
        remaining = issued.expires_at - self._clock()
        wanted = min(max(min_ttl_seconds, 0), MAX_MIN_TTL_SECONDS)
        if remaining <= wanted:
            # A token that is already (nearly) expired by our clock would fail mid-use; fail closed.
            raise GitHubAuthError(
                f"GitHub issued an installation token that expires in {int(remaining)}s, "
                f"less than the {wanted}s required; check the server clock"
            )
        with self._lock:
            self._tokens[cache_key] = issued
        logger.info("Issued a GitHub installation token for %s (expires %s)", scope, issued.expires_at_iso())
        return issued


def _cache_key(repository: tuple[str, str] | None) -> str:
    return "/".join(repository).lower() if repository else "*"


def _check_token_repositories(listed: object, repository: tuple[str, str]) -> None:
    """Raise unless every repository GitHub scoped the token to is the one requested.

    GitHub lists the token's repositories (``full_name``, ``owner.login``,
    ``name``) in the access_tokens response. Whatever identity an entry
    carries must match the requested ``owner/name`` (case-insensitive); an
    entry naming another owner or repository — e.g. the installation belongs
    to a different account that happens to have a repository of the same
    name — fails closed. A response without the list cannot be checked and is
    accepted.
    """
    if listed is None:
        return
    owner, name = (part.lower() for part in repository)
    if not isinstance(listed, list) or not listed:
        raise GitHubAuthError(f"GitHub did not scope the installation token to {repository[0]}/{repository[1]}")
    for entry in listed:
        if not isinstance(entry, dict):
            raise GitHubAuthError("GitHub returned an unreadable repository list for the installation token")
        full_name = entry.get("full_name")
        login = entry.get("owner", {}).get("login") if isinstance(entry.get("owner"), dict) else None
        entry_name = entry.get("name")
        mismatched = (
            (isinstance(full_name, str) and full_name.lower() != f"{owner}/{name}")
            or (isinstance(login, str) and login.lower() != owner)
            or (isinstance(entry_name, str) and entry_name.lower() != name)
        )
        if not any(isinstance(value, str) for value in (full_name, login, entry_name)):
            # No identity to compare: an unreadable answer, not a statement about access.
            raise GitHubAuthError("GitHub returned an unreadable repository list for the installation token")
        if mismatched:
            raise GitHubRepositoryAccessError(
                f"GitHub scoped the installation token to a different repository than "
                f"{repository[0]}/{repository[1]}; check that the App is installed on that account"
            )


_providers: dict[tuple[str, ...], GitHubCredentialsProvider] = {}
_providers_lock = threading.Lock()


def get_credentials_provider(settings: Settings | None = None) -> GitHubCredentialsProvider:
    """The process-wide provider for ``settings`` (default: ``get_settings()``); caches survive between calls."""
    if settings is None:
        from henchmen.config.settings import get_settings

        settings = get_settings()
    app = GitHubAppConfig.from_settings(settings)
    partial = partial_app_message(
        settings.github_app_id, settings.github_app_private_key_path, settings.github_app_installation_id
    )
    pat = settings.github_token if isinstance(settings.github_token, str) else ""
    api_url = settings.github_api_url if isinstance(settings.github_api_url, str) else GITHUB_API_URL
    key = (
        app.app_id if app else "",
        str(app.private_key_path) if app else "",
        app.installation_id if app else "",
        partial or "",
        api_url,
        # Keyed on a digest so the PAT itself is not held in a dict key.
        hashlib.sha256(pat.encode("utf-8")).hexdigest(),
    )
    with _providers_lock:
        provider = _providers.get(key)
        if provider is None:
            provider = GitHubCredentialsProvider(app=app, pat=pat, api_url=api_url, partial_app_problem=partial)
            _providers[key] = provider
        return provider


def reset_credentials_providers() -> None:
    """Drop every cached provider (tests; configuration reloads)."""
    with _providers_lock:
        _providers.clear()


def get_github_token(
    repo: str | None = None, *, settings: Settings | None = None, min_ttl_seconds: int = REFRESH_MARGIN_SECONDS
) -> str:
    """A GitHub token for ``repo``: an installation token when an App is configured, else the PAT (maybe empty)."""
    return get_credentials_provider(settings).token(repo, min_ttl_seconds=min_ttl_seconds)


async def get_github_token_async(
    repo: str | None = None, *, settings: Settings | None = None, min_ttl_seconds: int = REFRESH_MARGIN_SECONDS
) -> str:
    """Async :func:`get_github_token`."""
    return await get_credentials_provider(settings).token_async(repo, min_ttl_seconds=min_ttl_seconds)


async def get_installation_token_async(
    repo: str | None = None, *, settings: Settings | None = None, min_ttl_seconds: int = REFRESH_MARGIN_SECONDS
) -> InstallationToken:
    """An installation token with its expiry; raises :class:`GitHubAuthError` when no GitHub App is configured."""
    return await get_credentials_provider(settings).installation_token_async(repo, min_ttl_seconds=min_ttl_seconds)
