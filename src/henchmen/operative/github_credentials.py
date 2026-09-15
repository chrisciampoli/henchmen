"""GitHub credentials inside an operative: refresh the installation token before it expires (amendment A5).

A GitHub App installation token lasts one hour. On a desktop install
LairManager starts each operative with a token scoped to its task's repository
and the token's expiry (``github_token_expires_at``). :class:`OperativeGitHubCredentials`
keeps that token fresh in two ways:

* :meth:`~OperativeGitHubCredentials.ensure_fresh` is awaited before every push
  and every GitHub API tool call;
* :meth:`~OperativeGitHubCredentials.run_refresh_loop` runs in the background
  for the whole node (:func:`start_refresh_task`), and is cancelled when the
  operative finishes.

Within :data:`REFRESH_BEFORE_EXPIRY_SECONDS` of expiry either path calls
``POST /mastermind/internal/tasks/{TASK_ID}/github-token`` with the operative's
task token and its ``NODE_ID``/``LAIR_ID``. On success the new token replaces
the old one in memory and in ``Settings`` (``github_token`` and
``github_token_expires_at``), and the workspace's ``origin`` remote (which has
embedded the token since the clone) is pointed at it with ``git remote
set-url``. That puts the token in the argv of a git process inside the
operative container -- the same exposure as the clone itself (B9) -- so the
command line is never logged and git's error output is redacted.

Refreshing is best effort and never raises: a failed refresh is logged
(without any token) and the current token is kept until it expires, so a push
or API call after expiry fails with GitHub's own error and the node fails
closed. There is no PAT to fall back to. Operatives started with a PAT (no
expiry), or outside a desktop install (no task token), never refresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from henchmen.config.settings import Settings, get_settings
from henchmen.models.operative import OperativeGitHubToken
from henchmen.operative.git_helpers import run_git
from henchmen.utils.git import build_clone_url
from henchmen.utils.github_auth import GitHubAuthError, parse_expiry
from henchmen.utils.redaction import redact
from henchmen.utils.repositories import is_owner_name

logger = logging.getLogger(__name__)

#: Refresh once the current token has this little time left.
REFRESH_BEFORE_EXPIRY_SECONDS = 10 * 60
#: After a failed refresh, the background loop tries again this much later.
REFRESH_RETRY_SECONDS = 60
#: The background loop never sleeps longer than this in one go, so a suspended clock is re-read.
_MAX_LOOP_SLEEP_SECONDS = 5 * 60
_HTTP_TIMEOUT = 15.0

AsyncClientFactory = Callable[[], httpx.AsyncClient]
Sleep = Callable[[float], Awaitable[None]]


def _default_client() -> httpx.AsyncClient:
    # trust_env=False: never read HTTP(S)_PROXY / NO_PROXY from the environment for this
    # call to Mastermind. A configured proxy would otherwise receive the task token.
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT, trust_env=False)


def _read_expiry(raw: str) -> float | None:
    """The configured expiry as a Unix timestamp, or ``None`` for a PAT (blank) or an unreadable value."""
    if not raw.strip():
        return None
    try:
        return parse_expiry(raw)
    except GitHubAuthError:
        logger.warning("Ignoring an unreadable GitHub token expiry; the token will not be refreshed")
        return None


class OperativeGitHubCredentials:
    """The operative's current GitHub token, refreshed through the internal API near expiry."""

    def __init__(
        self,
        *,
        settings: Settings,
        task_id: str,
        node_id: str,
        lair_id: str,
        repo_slug: str,
        client_factory: AsyncClientFactory | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._token = settings.github_token
        self._expires_at = _read_expiry(settings.github_token_expires_at)
        self._task_token = settings.operative_task_token.strip()
        self._base_url = settings.local_forward_base_url.strip().rstrip("/")
        self._task_id = task_id
        self._node_id = node_id
        self._lair_id = lair_id
        self._repo_slug = repo_slug if is_owner_name(repo_slug) else ""
        self._client_factory = client_factory or _default_client
        self._clock = clock
        self._sleep = sleep
        self._lock: asyncio.Lock | None = None
        self._refused = False
        # Set when a refresh replaced ``self._token`` but the origin remote (for lack of a
        # workspace_dir, or a failed ``git remote set-url``) does not point at it yet. Cleared
        # once the repoint succeeds, from this refresh or a later ``ensure_fresh`` call.
        self._needs_repoint = False

    def __repr__(self) -> str:
        return f"OperativeGitHubCredentials(refreshable={self.refreshable})"

    @property
    def token(self) -> str:
        """The token to use now (await :meth:`ensure_fresh` first)."""
        return self._token

    @property
    def expires_at(self) -> float | None:
        """The current token's expiry as a Unix timestamp; ``None`` for a PAT."""
        return self._expires_at

    @property
    def refreshable(self) -> bool:
        """True for an installation token in a desktop-install operative that can reach Mastermind."""
        return (
            self._expires_at is not None
            and not self._refused
            and all((self._task_token, self._base_url, self._task_id, self._node_id, self._lair_id))
        )

    def _seconds_left(self) -> float:
        return (self._expires_at or 0.0) - self._clock()

    def _expiring(self) -> bool:
        return self._expires_at is not None and self._seconds_left() <= REFRESH_BEFORE_EXPIRY_SECONDS

    async def ensure_fresh(self, workspace_dir: str | None = None) -> str:
        """Refresh the token if it expires within :data:`REFRESH_BEFORE_EXPIRY_SECONDS`; return the token to use.

        Never raises. ``workspace_dir``, when given, is the clone whose
        ``origin`` remote is pointed at a refreshed token. When a *previous*
        refresh updated the token but could not repoint that remote (no
        ``workspace_dir`` yet, or a failed ``git remote set-url``), this call
        retries only the repoint — using the token already held, with no new
        request to Mastermind — before returning.
        """
        if self.refreshable and self._expiring():
            if self._lock is None:
                self._lock = asyncio.Lock()
            async with self._lock:
                if self.refreshable and self._expiring():
                    try:
                        await self._refresh(workspace_dir)
                    except Exception as exc:  # never raise into a push or a tool call
                        logger.warning(
                            "GitHub token refresh failed (%s); keeping the current token", type(exc).__name__
                        )
        elif workspace_dir and self._needs_repoint:
            await self._retry_pending_repoint(workspace_dir)
        return self._token

    async def run_refresh_loop(self, workspace_dir: str | None = None) -> None:
        """Keep the token fresh until cancelled; returns early when it can no longer be refreshed."""
        while self.refreshable:
            wait = self._seconds_left() - REFRESH_BEFORE_EXPIRY_SECONDS
            if wait > 0:
                await self._sleep(min(wait, _MAX_LOOP_SLEEP_SECONDS))
                continue
            await self.ensure_fresh(workspace_dir)
            if self.refreshable and self._expiring():
                await self._sleep(REFRESH_RETRY_SECONDS)

    async def _refresh(self, workspace_dir: str | None) -> None:
        url = f"{self._base_url}/mastermind/internal/tasks/{quote(self._task_id, safe='')}/github-token"
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self._task_token}"},
                    json={"node_id": self._node_id, "operative_id": self._lair_id},
                )
        except httpx.HTTPError as exc:
            logger.warning("Could not reach Mastermind to refresh the GitHub token (%s)", type(exc).__name__)
            return
        if response.status_code == 409:
            # No GitHub App, the task finished, or this lair is no longer the active one: asking again cannot help.
            self._refused = True
            logger.warning("Mastermind will not refresh this operative's GitHub token (HTTP 409); keeping it")
            return
        if response.status_code != 200:
            logger.warning(
                "GitHub token refresh was refused (HTTP %s); keeping the current token", response.status_code
            )
            return
        try:
            issued = OperativeGitHubToken.model_validate(response.json())
        except (ValueError, ValidationError):
            logger.warning("GitHub token refresh returned an unreadable response; keeping the current token")
            return
        unchanged = issued.token == self._token
        self._token = issued.token
        self._expires_at = issued.expires_at.timestamp()
        expires_iso = issued.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Settings is where the clone and any later reader find the token.
        self._settings.github_token = issued.token
        self._settings.github_token_expires_at = expires_iso
        if unchanged:
            # The token itself didn't change, but an earlier refresh may still have a
            # pending repoint (no workspace_dir yet, or a failed set-url) — retry that
            # now rather than silently dropping it, since ensure_fresh's own retry
            # branch is only reached when this refresh path is *not* taken.
            if workspace_dir and self._needs_repoint:
                await self._retry_pending_repoint(workspace_dir)
            return
        logger.info("Refreshed the GitHub token; it now expires at %s", expires_iso)
        # Not "fresh" until the remote actually points at the new token: a
        # missing workspace_dir or a failed set-url leaves this set so the
        # next ensure_fresh(workspace_dir) retries just the repoint.
        self._needs_repoint = True
        if workspace_dir and self._repo_slug:
            await self._retry_pending_repoint(workspace_dir)

    async def _retry_pending_repoint(self, workspace_dir: str) -> None:
        """Point ``origin`` at the current token; clears :attr:`_needs_repoint` only on success."""
        if not self._repo_slug:
            return
        if await self._repoint_origin(workspace_dir, self._token):
            self._needs_repoint = False

    async def _repoint_origin(self, workspace_dir: str, token: str) -> bool:
        """``git remote set-url`` the workspace's origin to *token*; True on success."""
        # The command line carries the token (same exposure as the clone, B9): never log it.
        clone_url = build_clone_url(self._repo_slug, token)
        _, stderr, returncode = await run_git(workspace_dir, "remote", "set-url", "origin", clone_url)
        if returncode != 0:
            detail = redact(stderr.replace(token, "***"))[:300]
            logger.warning("Could not point the origin remote at the refreshed GitHub token: %s", detail)
            return False
        return True


_credentials: OperativeGitHubCredentials | None = None


def get_operative_credentials() -> OperativeGitHubCredentials:
    """The operative's credentials, built once from Settings and the runtime contract.

    ``TASK_ID``, ``NODE_ID``, ``LAIR_ID`` and ``REPO_URL`` are operative
    runtime-contract variables the Lair injects.
    """
    global _credentials
    if _credentials is None:
        from henchmen.arsenal._repo import current_repo_slug

        _credentials = OperativeGitHubCredentials(
            settings=get_settings(),
            task_id=os.environ.get("TASK_ID", ""),
            node_id=os.environ.get("NODE_ID", ""),
            lair_id=os.environ.get("LAIR_ID", ""),
            repo_slug=current_repo_slug(),
        )
    return _credentials


def start_refresh_task(workspace_dir: str) -> asyncio.Task[None] | None:
    """Start the background refresh loop for this operative; ``None`` when its token is not refreshable."""
    credentials = get_operative_credentials()
    if not credentials.refreshable:
        return None
    return asyncio.create_task(credentials.run_refresh_loop(workspace_dir), name="github-token-refresh")


async def stop_refresh_task(task: asyncio.Task[None] | None) -> None:
    """Cancel the background refresh loop and wait for it to finish."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def reset_operative_credentials() -> None:
    """Forget the cached credentials (tests)."""
    global _credentials
    _credentials = None
