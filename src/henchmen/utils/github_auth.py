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
* **No GitHub App**: return ``github_token`` (the PAT) exactly as before, so
  engineers and existing deployments keep working.

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
# Installation tokens live one hour; a longer minimum lifetime cannot be met.
MAX_MIN_TTL_SECONDS = 55 * 60
_HTTP_TIMEOUT = 10.0
_ERROR_MESSAGE_LIMIT = 200
# A GitHub repository name (the part after ``owner/``).
_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")

SyncClientFactory = Callable[[], httpx.Client]
AsyncClientFactory = Callable[[], httpx.AsyncClient]


class GitHubAuthError(RuntimeError):
    """A GitHub App is configured but a usable token could not be produced."""


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
        raise GitHubAuthError("The GitHub App private key could not sign a token; reconnect GitHub") from None


def load_app_private_key(path: Path) -> bytes:
    """Read the GitHub App's PEM private key; the only loader of that file.

    The file must be a regular file this user owns (``check_secret_path``: a
    symbolic link or another user's file is refused). Any problem is a
    :class:`GitHubAuthError` naming the path, never the key's content.
    """
    try:
        check_secret_path(path)
        return path.read_bytes()
    except OSError:
        # SecretFileError is an OSError. ``from None`` keeps the raw OS error out of the chain.
        raise GitHubAuthError(f"The GitHub App private key at {path} is missing or unreadable") from None


def app_jwt_for(app_id: str, private_key_path: Path, *, now: float | None = None) -> str:
    """A fresh app JWT for ``app_id``, signed with the key at ``private_key_path``."""
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


def _repository_name(repo: str | None) -> str | None:
    """``owner/name`` (or a clone URL) → ``name``; ``None`` for no repository. Invalid names raise."""
    if repo is None or not repo.strip():
        return None
    name = repo.strip().rstrip("/").split("/")[-1]
    name = name.removesuffix(".git")
    if not _REPOSITORY_NAME.fullmatch(name) or name in {".", ".."}:
        # Fail closed: an unparseable name must never widen into an installation-wide token.
        raise GitHubAuthError("The repository name for a GitHub installation token is not valid")
    return name


def _default_client() -> httpx.Client:
    # trust_env=False: no proxy or netrc credentials from the environment see the app JWT.
    return httpx.Client(timeout=_HTTP_TIMEOUT, trust_env=False)


def _default_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT, trust_env=False)


class GitHubCredentialsProvider:
    """Hands out GitHub tokens: cached installation tokens for an App, else the PAT.

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
    ) -> None:
        self._app = app
        self._pat = pat
        self._api_url = api_url.strip().rstrip("/") or GITHUB_API_URL
        self._client_factory = client_factory or _default_client
        self._async_client_factory = async_client_factory or _default_async_client
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: dict[str, InstallationToken] = {}
        self._private_key: bytes | None = None
        self._sync_locks: dict[str, threading.Lock] = {}
        self._async_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
            weakref.WeakKeyDictionary()
        )

    def __repr__(self) -> str:
        return f"GitHubCredentialsProvider(uses_app={self.uses_app})"

    @property
    def uses_app(self) -> bool:
        """True when tokens come from a GitHub App rather than the PAT."""
        return self._app is not None

    @property
    def app(self) -> GitHubAppConfig | None:
        """The GitHub App this provider mints tokens for, if any."""
        return self._app

    def app_jwt(self) -> str:
        """A fresh app JWT (for ``/app/*`` endpoints)."""
        app = self._require_app()
        return build_app_jwt(app.app_id, self._load_private_key(app), now=self._clock())

    def installation_token(
        self, repo: str | None = None, *, min_ttl_seconds: int = REFRESH_MARGIN_SECONDS
    ) -> InstallationToken:
        """An installation token for ``repo`` (or the whole installation) that stays valid long enough."""
        app = self._require_app()
        name = _repository_name(repo)
        cache_key = (name or "*").lower()
        margin = self._required_lifetime(min_ttl_seconds)
        cached = self._cached(cache_key, margin)
        if cached is not None:
            return cached
        with self._sync_lock(cache_key):
            cached = self._cached(cache_key, margin)
            if cached is not None:
                return cached
            url, body = self._token_request(app, name)
            headers = api_headers(self.app_jwt())
            try:
                with self._client_factory() as client:
                    response = client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise self._unreachable(exc) from None
            return self._remember(cache_key, name, response, min_ttl_seconds)

    async def installation_token_async(
        self, repo: str | None = None, *, min_ttl_seconds: int = REFRESH_MARGIN_SECONDS
    ) -> InstallationToken:
        """Async :meth:`installation_token`."""
        app = self._require_app()
        name = _repository_name(repo)
        cache_key = (name or "*").lower()
        margin = self._required_lifetime(min_ttl_seconds)
        cached = self._cached(cache_key, margin)
        if cached is not None:
            return cached
        async with self._async_lock(cache_key):
            cached = self._cached(cache_key, margin)
            if cached is not None:
                return cached
            url, body = self._token_request(app, name)
            headers = api_headers(self.app_jwt())
            try:
                async with self._async_client_factory() as client:
                    response = await client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise self._unreachable(exc) from None
            return self._remember(cache_key, name, response, min_ttl_seconds)

    def token(self, repo: str | None = None, *, min_ttl_seconds: int = REFRESH_MARGIN_SECONDS) -> str:
        """A token for ``repo``: an installation token with an App, else the PAT (maybe empty)."""
        if self._app is None:
            return self._pat
        return self.installation_token(repo, min_ttl_seconds=min_ttl_seconds).token

    async def token_async(self, repo: str | None = None, *, min_ttl_seconds: int = REFRESH_MARGIN_SECONDS) -> str:
        """Async :meth:`token` for code on the event loop."""
        if self._app is None:
            return self._pat
        return (await self.installation_token_async(repo, min_ttl_seconds=min_ttl_seconds)).token

    def invalidate(self) -> None:
        """Forget cached tokens and the loaded key (e.g. after reconnecting GitHub)."""
        with self._lock:
            self._tokens.clear()
            self._private_key = None

    # -- internals -------------------------------------------------------------

    def _require_app(self) -> GitHubAppConfig:
        if self._app is None:
            raise GitHubAuthError("No GitHub App is configured")
        return self._app

    def _load_private_key(self, app: GitHubAppConfig) -> bytes:
        with self._lock:
            if self._private_key is None:
                self._private_key = load_app_private_key(app.private_key_path)
            return self._private_key

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
        """Seconds a cached token must still be valid for: at least the refresh margin, at most 55 minutes."""
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

    def _token_request(self, app: GitHubAppConfig, name: str | None) -> tuple[str, dict[str, Any]]:
        body: dict[str, Any] = {"repositories": [name]} if name else {}
        return f"{self._api_url}/app/installations/{app.installation_id}/access_tokens", body

    @staticmethod
    def _unreachable(exc: httpx.HTTPError) -> GitHubAuthError:
        logger.warning("Could not reach GitHub for an installation token (%s)", type(exc).__name__)
        return GitHubAuthError(f"Could not reach GitHub for an installation token ({type(exc).__name__})")

    def _remember(
        self, cache_key: str, name: str | None, response: httpx.Response, min_ttl_seconds: int
    ) -> InstallationToken:
        scope = name or "the whole installation"
        if response.status_code != 201:
            detail = github_error_detail(response)
            logger.warning("GitHub refused an installation token for %s (%s)", scope, detail)
            raise GitHubAuthError(f"GitHub refused to issue an installation token ({detail})")
        try:
            payload = response.json()
        except ValueError:
            raise GitHubAuthError("GitHub returned an unreadable installation token response") from None
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise GitHubAuthError("GitHub returned no installation token")
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


_providers: dict[tuple[str, ...], GitHubCredentialsProvider] = {}
_providers_lock = threading.Lock()


def get_credentials_provider(settings: Settings | None = None) -> GitHubCredentialsProvider:
    """The process-wide provider for ``settings`` (default: ``get_settings()``); caches survive between calls."""
    if settings is None:
        from henchmen.config.settings import get_settings

        settings = get_settings()
    app = GitHubAppConfig.from_settings(settings)
    pat = settings.github_token if isinstance(settings.github_token, str) else ""
    api_url = settings.github_api_url if isinstance(settings.github_api_url, str) else GITHUB_API_URL
    key = (
        app.app_id if app else "",
        str(app.private_key_path) if app else "",
        app.installation_id if app else "",
        api_url,
        # Keyed on a digest so the PAT itself is not held in a dict key.
        hashlib.sha256(pat.encode("utf-8")).hexdigest(),
    )
    with _providers_lock:
        provider = _providers.get(key)
        if provider is None:
            provider = GitHubCredentialsProvider(app=app, pat=pat, api_url=api_url)
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
