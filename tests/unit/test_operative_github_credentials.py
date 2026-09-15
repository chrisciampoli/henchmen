"""Tests for the operative-side GitHub token refresh (amendment A5)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from henchmen.config.settings import Settings
from henchmen.operative import github_credentials
from henchmen.operative.github_credentials import (
    REFRESH_BEFORE_EXPIRY_SECONDS,
    REFRESH_RETRY_SECONDS,
    OperativeGitHubCredentials,
)

NOW = 1_900_000_000.0
TASK_ID = "task-1"
NODE_ID = "implement_fix"
LAIR_ID = "lair-task-1-implement-fix-1a2b3c"
TASK_TOKEN = "task-token-abc-0123456789abcdef"
INITIAL = "ghs_initialTokenValue0123456789"
REFRESHED = "ghs_refreshedTokenValue0123456789"


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "github_token": INITIAL,
        "github_token_expires_at": _iso(NOW + REFRESH_BEFORE_EXPIRY_SECONDS - 60),
        "operative_task_token": TASK_TOKEN,
        "local_forward_base_url": "http://henchmen:8000",
    }
    values.update(overrides)
    return Settings(**values)


class _Server:
    def __init__(self, status: int = 200, expires_in: float = 3600) -> None:
        self.status = status
        self.expires_in = expires_in
        self.requests: list[httpx.Request] = []
        self.clients: list[httpx.AsyncClient] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"})
        return httpx.Response(200, json={"token": REFRESHED, "expires_at": _iso(NOW + self.expires_in)})

    def factory(self) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self))
        self.clients.append(client)
        return client


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _credentials(
    server: _Server,
    *,
    settings: Settings | None = None,
    clock: _Clock | None = None,
    sleep: Any = None,
    **overrides: Any,
) -> OperativeGitHubCredentials:
    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    return OperativeGitHubCredentials(
        settings=settings or _settings(**overrides),
        task_id=TASK_ID,
        node_id=NODE_ID,
        lair_id=LAIR_ID,
        repo_slug="acme/webapp",
        client_factory=server.factory,
        clock=clock or _Clock(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_refreshes_near_expiry_and_repoints_origin(caplog: pytest.LogCaptureFixture) -> None:
    server = _Server()
    settings = _settings()
    credentials = _credentials(server, settings=settings)
    with (
        caplog.at_level(logging.DEBUG),
        patch.object(github_credentials, "run_git", new=AsyncMock(return_value=("", "", 0))) as run_git,
    ):
        assert await credentials.ensure_fresh("/workspace/task-1") == REFRESHED
    (request,) = server.requests
    assert str(request.url) == f"http://henchmen:8000/mastermind/internal/tasks/{TASK_ID}/github-token"
    assert request.headers["authorization"] == f"Bearer {TASK_TOKEN}"
    assert json.loads(request.content) == {"node_id": NODE_ID, "operative_id": LAIR_ID}
    run_git.assert_awaited_once_with(
        "/workspace/task-1",
        "remote",
        "set-url",
        "origin",
        f"https://x-access-token:{REFRESHED}@github.com/acme/webapp.git",
    )
    assert credentials.token == REFRESHED
    assert credentials.expires_at == NOW + 3600
    # Settings is updated too, so every later reader sees the refreshed token.
    assert settings.github_token == REFRESHED
    assert settings.github_token_expires_at == _iso(NOW + 3600)
    for secret in (INITIAL, REFRESHED, TASK_TOKEN):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_refreshed_token_is_reused() -> None:
    server = _Server()
    credentials = _credentials(server)
    with patch.object(github_credentials, "run_git", new=AsyncMock(return_value=("", "", 0))):
        await credentials.ensure_fresh("/workspace/task-1")
        await credentials.ensure_fresh("/workspace/task-1")
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_refresh() -> None:
    server = _Server()
    credentials = _credentials(server)
    with patch.object(github_credentials, "run_git", new=AsyncMock(return_value=("", "", 0))):
        tokens = await asyncio.gather(*(credentials.ensure_fresh("/workspace/task-1") for _ in range(5)))
    assert tokens == [REFRESHED] * 5
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_no_refresh_while_the_token_has_time_left() -> None:
    server = _Server()
    credentials = _credentials(server, github_token_expires_at=_iso(NOW + REFRESH_BEFORE_EXPIRY_SECONDS + 1))
    assert credentials.refreshable
    assert await credentials.ensure_fresh() == INITIAL
    assert server.requests == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"operative_task_token": ""},  # not a desktop install
        {"local_forward_base_url": ""},
        {"github_token_expires_at": ""},  # a PAT
        {"github_token_expires_at": "not-a-date"},
    ],
)
@pytest.mark.asyncio
async def test_pat_and_cloud_operatives_never_refresh(overrides: dict[str, str]) -> None:
    server = _Server()
    credentials = _credentials(server, **overrides)
    assert not credentials.refreshable
    assert await credentials.ensure_fresh("/workspace/task-1") == INITIAL
    assert server.requests == []
    assert await asyncio.wait_for(credentials.run_refresh_loop("/workspace/task-1"), timeout=1) is None


@pytest.mark.parametrize("missing", ["task_id", "node_id", "lair_id"])
def test_without_the_runtime_contract_there_is_no_refresh(missing: str) -> None:
    values = {"task_id": TASK_ID, "node_id": NODE_ID, "lair_id": LAIR_ID, missing: ""}
    credentials = OperativeGitHubCredentials(settings=_settings(), repo_slug="acme/webapp", **values)
    assert not credentials.refreshable


@pytest.mark.asyncio
async def test_a_409_keeps_the_current_token_and_stops_asking() -> None:
    server = _Server(status=409)
    credentials = _credentials(server)
    with patch.object(github_credentials, "run_git", new=AsyncMock()) as run_git:
        assert await credentials.ensure_fresh("/workspace/task-1") == INITIAL
        assert await credentials.ensure_fresh("/workspace/task-1") == INITIAL
    run_git.assert_not_awaited()
    assert len(server.requests) == 1
    assert not credentials.refreshable


@pytest.mark.parametrize("status", [401, 500, 502])
@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_current_token_and_tries_again_later(status: int) -> None:
    server = _Server(status=status)
    settings = _settings()
    credentials = _credentials(server, settings=settings)
    with patch.object(github_credentials, "run_git", new=AsyncMock()) as run_git:
        assert await credentials.ensure_fresh("/workspace/task-1") == INITIAL
        assert await credentials.ensure_fresh("/workspace/task-1") == INITIAL
    run_git.assert_not_awaited()
    assert len(server.requests) == 2
    assert credentials.refreshable
    assert settings.github_token == INITIAL


@pytest.mark.parametrize(
    "payload",
    [b"not json", b'{"token": "ghs_x"}', b'{"token": "", "expires_at": "2030-01-01T00:00:00Z"}', b"[]"],
)
@pytest.mark.asyncio
async def test_an_unreadable_response_keeps_the_current_token(payload: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    credentials = OperativeGitHubCredentials(
        settings=_settings(),
        task_id=TASK_ID,
        node_id=NODE_ID,
        lair_id=LAIR_ID,
        repo_slug="acme/webapp",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=_Clock(),
    )
    assert await credentials.ensure_fresh() == INITIAL


@pytest.mark.asyncio
async def test_unreachable_server_keeps_the_current_token() -> None:
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    credentials = OperativeGitHubCredentials(
        settings=_settings(),
        task_id=TASK_ID,
        node_id=NODE_ID,
        lair_id=LAIR_ID,
        repo_slug="acme/webapp",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(offline)),
        clock=_Clock(),
    )
    assert await credentials.ensure_fresh() == INITIAL


@pytest.mark.asyncio
async def test_an_unexpected_error_never_reaches_the_caller() -> None:
    def broken() -> httpx.AsyncClient:
        raise RuntimeError("boom")

    credentials = OperativeGitHubCredentials(
        settings=_settings(),
        task_id=TASK_ID,
        node_id=NODE_ID,
        lair_id=LAIR_ID,
        repo_slug="acme/webapp",
        client_factory=broken,
        clock=_Clock(),
    )
    assert await credentials.ensure_fresh() == INITIAL


@pytest.mark.asyncio
async def test_a_failed_set_url_is_logged_without_the_token(caplog: pytest.LogCaptureFixture) -> None:
    server = _Server()
    credentials = _credentials(server)
    stderr = f"fatal: could not set 'https://x-access-token:{REFRESHED}@github.com/acme/webapp.git'"
    with (
        caplog.at_level(logging.DEBUG),
        patch.object(github_credentials, "run_git", new=AsyncMock(return_value=("", stderr, 128))),
    ):
        assert await credentials.ensure_fresh("/workspace/task-1") == REFRESHED
    assert "Could not point the origin remote" in caplog.text
    assert REFRESHED not in caplog.text


@pytest.mark.asyncio
async def test_no_workspace_means_no_remote_rewrite() -> None:
    server = _Server()
    credentials = _credentials(server)
    with patch.object(github_credentials, "run_git", new=AsyncMock()) as run_git:
        assert await credentials.ensure_fresh() == REFRESHED
    run_git.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_repoint_is_retried_on_the_next_ensure_fresh_without_a_new_fetch() -> None:
    """Carry-over from the Task 9 review (a): a failed ``git remote set-url`` is retried later,
    using the token already fetched — not by asking Mastermind again."""
    server = _Server()
    credentials = _credentials(server)
    attempts: list[tuple[str, ...]] = []

    async def flaky_run_git(workspace_dir: str, *args: str) -> tuple[str, str, int]:
        attempts.append(args)
        if len(attempts) == 1:
            return "", "fatal: could not set", 128
        return "", "", 0

    with patch.object(github_credentials, "run_git", new=AsyncMock(side_effect=flaky_run_git)):
        assert await credentials.ensure_fresh("/workspace/task-1") == REFRESHED
        assert credentials.token == REFRESHED  # updated even though the repoint failed
        # Not expiring any more (the new token's expiry is far away) — only the
        # pending repoint should be retried, with no second call to Mastermind.
        assert await credentials.ensure_fresh("/workspace/task-1") == REFRESHED
    assert len(server.requests) == 1
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_repoint_is_not_retried_once_it_succeeds() -> None:
    server = _Server()
    credentials = _credentials(server)
    with patch.object(github_credentials, "run_git", new=AsyncMock(return_value=("", "", 0))) as run_git:
        assert await credentials.ensure_fresh("/workspace/task-1") == REFRESHED
        assert await credentials.ensure_fresh("/workspace/task-1") == REFRESHED
    run_git.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_token_skips_the_log_and_the_repoint(caplog: pytest.LogCaptureFixture) -> None:
    """Carry-over from the Task 9 review (b): an unchanged token means nothing to repoint."""

    class _SameServer(_Server):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json={"token": INITIAL, "expires_at": _iso(NOW + 3600)})

    server = _SameServer()
    credentials = _credentials(server)
    with (
        caplog.at_level(logging.INFO),
        patch.object(github_credentials, "run_git", new=AsyncMock()) as run_git,
    ):
        assert await credentials.ensure_fresh("/workspace/task-1") == INITIAL
    run_git.assert_not_awaited()
    assert "Refreshed the GitHub token" not in caplog.text
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_a_pending_repoint_is_retried_even_when_the_next_refresh_returns_the_same_token() -> None:
    """Review carry-over: `_refresh` must not drop a pending repoint just because the newly
    fetched token happens to be unchanged (it returned early before checking `_needs_repoint`)."""

    class _StillExpiringThenSameServer(_Server):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if len(self.requests) == 1:
                # Still within the refresh window, so the next ensure_fresh call goes
                # through the full _refresh path again instead of the repoint-only retry.
                return httpx.Response(200, json={"token": REFRESHED, "expires_at": _iso(NOW + 60)})
            return httpx.Response(200, json={"token": REFRESHED, "expires_at": _iso(NOW + 3600)})

    server = _StillExpiringThenSameServer()
    credentials = _credentials(server)
    attempts: list[tuple[str, ...]] = []

    async def flaky_run_git(workspace_dir: str, *args: str) -> tuple[str, str, int]:
        attempts.append(args)
        if len(attempts) == 1:
            return "", "fatal: could not set", 128
        return "", "", 0

    with patch.object(github_credentials, "run_git", new=AsyncMock(side_effect=flaky_run_git)) as run_git:
        # First refresh: token changes, but the repoint fails — _needs_repoint stays set.
        assert await credentials.ensure_fresh("/workspace/task-1") == REFRESHED
        assert credentials.expires_at == NOW + 60  # still expiring
        # Second refresh: the server returns the *same* token (unchanged), but the pending
        # repoint from before must still be retried, not silently dropped.
        assert await credentials.ensure_fresh("/workspace/task-1") == REFRESHED

    assert len(server.requests) == 2
    assert run_git.await_count == 2
    for call in run_git.await_args_list:
        assert call.args[1:4] == ("remote", "set-url", "origin")
    assert credentials.expires_at == NOW + 3600
    assert credentials._needs_repoint is False


def test_refresh_client_ignores_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    client = github_credentials._default_client()
    assert client.trust_env is False


def test_repr_never_shows_a_token() -> None:
    credentials = _credentials(_Server())
    assert INITIAL not in repr(credentials)
    assert TASK_TOKEN not in repr(credentials)


class TestRefreshLoop:
    @pytest.mark.asyncio
    async def test_sleeps_until_the_refresh_point_then_refreshes(self) -> None:
        server = _Server()
        clock = _Clock()
        settings = _settings(github_token_expires_at=_iso(NOW + REFRESH_BEFORE_EXPIRY_SECONDS + 200))
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.now += seconds
            if len(sleeps) >= 3:
                raise asyncio.CancelledError

        credentials = _credentials(server, settings=settings, clock=clock, sleep=fake_sleep)
        with (
            patch.object(github_credentials, "run_git", new=AsyncMock(return_value=("", "", 0))) as run_git,
            pytest.raises(asyncio.CancelledError),
        ):
            await credentials.run_refresh_loop("/workspace/task-1")
        # Slept exactly to the refresh point, refreshed once, then slept towards the next refresh point.
        assert sleeps[0] == 200
        assert len(server.requests) == 1
        run_git.assert_awaited_once()
        assert credentials.token == REFRESHED
        assert all(seconds > 0 for seconds in sleeps)

    @pytest.mark.asyncio
    async def test_a_failed_refresh_is_retried_after_the_retry_interval(self) -> None:
        server = _Server(status=502)
        clock = _Clock()
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.now += seconds
            if len(sleeps) >= 2:
                raise asyncio.CancelledError

        credentials = _credentials(server, clock=clock, sleep=fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await credentials.run_refresh_loop()
        assert sleeps == [REFRESH_RETRY_SECONDS, REFRESH_RETRY_SECONDS]
        assert len(server.requests) == 2
        assert credentials.token == INITIAL

    @pytest.mark.asyncio
    async def test_a_409_ends_the_loop(self) -> None:
        server = _Server(status=409)
        sleep = AsyncMock()
        credentials = _credentials(server, sleep=sleep)
        await asyncio.wait_for(credentials.run_refresh_loop(), timeout=1)
        assert len(server.requests) == 1
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_background_task_is_cancelled_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        never = asyncio.Event()

        async def blocking_sleep(seconds: float) -> None:
            await never.wait()

        credentials = _credentials(_Server(), github_token_expires_at=_iso(NOW + 3600), sleep=blocking_sleep)
        monkeypatch.setattr(github_credentials, "get_operative_credentials", lambda: credentials)
        task = github_credentials.start_refresh_task("/workspace/task-1")
        assert task is not None
        await asyncio.sleep(0)
        assert not task.done()
        await github_credentials.stop_refresh_task(task)
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_nothing_is_started_for_a_pat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        credentials = _credentials(_Server(), github_token_expires_at="")
        monkeypatch.setattr(github_credentials, "get_operative_credentials", lambda: credentials)
        assert github_credentials.start_refresh_task("/workspace/task-1") is None
        await github_credentials.stop_refresh_task(None)


def test_get_operative_credentials_reads_the_runtime_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASK_ID", TASK_ID)
    monkeypatch.setenv("NODE_ID", NODE_ID)
    monkeypatch.setenv("LAIR_ID", LAIR_ID)
    monkeypatch.setenv("REPO_URL", "https://github.com/acme/webapp")
    monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN", INITIAL)
    monkeypatch.setenv("HENCHMEN_GITHUB_TOKEN_EXPIRES_AT", _iso(NOW))
    monkeypatch.setenv("HENCHMEN_OPERATIVE_TASK_TOKEN", TASK_TOKEN)
    monkeypatch.setenv("HENCHMEN_LOCAL_FORWARD_BASE_URL", "http://henchmen:8000")
    monkeypatch.setattr(github_credentials, "get_settings", lambda: _settings())
    credentials = github_credentials.get_operative_credentials()
    assert credentials is github_credentials.get_operative_credentials()
    assert credentials.refreshable
    assert credentials.token == INITIAL
    github_credentials.reset_operative_credentials()
    assert github_credentials.get_operative_credentials() is not credentials


@pytest.mark.asyncio
async def test_branch_push_refreshes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from henchmen.operative import bootstrap

    events: list[tuple[str, ...]] = []

    async def fake_run_git(workspace_dir: str, *args: str) -> tuple[str, str, int]:
        events.append(("git", *args))
        return "", "", 0

    async def ensure_fresh(workspace_dir: str | None = None) -> str:
        events.append(("refresh", str(workspace_dir)))
        return REFRESHED

    monkeypatch.setattr(bootstrap, "run_git", fake_run_git)
    monkeypatch.setattr(bootstrap, "get_operative_credentials", lambda: SimpleNamespace(ensure_fresh=ensure_fresh))

    await bootstrap._create_branch_and_push("/workspace/task-1", "henchmen/abcd1234", _settings())

    push_index = next(i for i, event in enumerate(events) if event[:2] == ("git", "push"))
    assert events.index(("refresh", "/workspace/task-1")) < push_index


@pytest.mark.asyncio
async def test_a_push_after_expiry_fails_closed_with_a_redacted_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from henchmen.operative import bootstrap

    stale = f"fatal: Authentication failed for 'https://x-access-token:{INITIAL}@github.com/acme/webapp.git/'"

    async def fake_run_git(workspace_dir: str, *args: str) -> tuple[str, str, int]:
        return ("", stale, 128) if args[:1] == ("push",) else ("", "", 0)

    async def ensure_fresh(workspace_dir: str | None = None) -> str:
        return INITIAL  # the refresh failed; the expired token is all there is

    monkeypatch.setattr(bootstrap, "run_git", fake_run_git)
    monkeypatch.setattr(bootstrap, "get_operative_credentials", lambda: SimpleNamespace(ensure_fresh=ensure_fresh))

    with pytest.raises(RuntimeError, match="git push failed") as raised:
        await bootstrap._create_branch_and_push("/workspace/task-1", "henchmen/abcd1234", _settings())
    assert INITIAL not in str(raised.value)


@pytest.mark.asyncio
async def test_run_operative_stops_the_refresh_loop_before_reporting(tmp_path: Path) -> None:
    from henchmen.operative import bootstrap

    never = asyncio.Event()
    loop_task: dict[str, asyncio.Task[None]] = {}

    def start(workspace_dir: str) -> asyncio.Task[None]:
        loop_task["task"] = asyncio.create_task(never.wait())  # type: ignore[arg-type]
        return loop_task["task"]

    async def publish(report: Any, settings: Any, broker: Any = None) -> None:
        assert loop_task["task"].cancelled()

    agent = MagicMock()
    agent.run = AsyncMock(return_value={"summary": "done"})
    agent.get_telemetry.return_value = {}
    with (
        patch.dict("os.environ", {"TASK_ID": "task-loop", "NODE_ID": NODE_ID, "SCHEME_ID": "bugfix_standard"}),
        patch.object(bootstrap, "get_settings", return_value=MagicMock()),
        patch.object(bootstrap, "ProviderRegistry", return_value=MagicMock()),
        patch.object(bootstrap, "_get_document_store", return_value=None),
        patch.object(bootstrap, "resolve_model_name", return_value="m"),
        patch.object(bootstrap, "initialize_workspace", new=AsyncMock(return_value=str(tmp_path))),
        patch.object(bootstrap, "_build_file_context", new=AsyncMock(return_value="")),
        patch.object(bootstrap, "build_operative_agent", new=AsyncMock(return_value=agent)),
        patch.object(bootstrap, "_check_for_changes", new=AsyncMock(return_value=False)),
        patch.object(bootstrap, "start_refresh_task", side_effect=start) as started,
        patch.object(bootstrap, "publish_report", side_effect=publish) as published,
    ):
        await bootstrap.run_operative()
    started.assert_called_once_with(str(tmp_path))
    published.assert_called_once()
    assert loop_task["task"].cancelled()


@pytest.mark.asyncio
async def test_git_push_tool_refreshes_before_pushing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from henchmen.arsenal.tools import git_ops

    monkeypatch.setattr(git_ops, "_resolve_working_dir", lambda working_dir: str(tmp_path))
    events: list[str] = []

    async def ensure_fresh(workspace_dir: str | None = None) -> str:
        events.append(f"refresh {workspace_dir}")
        return REFRESHED

    async def fake_git(*args: str, working_dir: str = "") -> dict[str, Any]:
        events.append(" ".join(args))
        return {"stdout": "", "stderr": "", "return_code": 0, "success": True}

    monkeypatch.setattr(
        github_credentials, "get_operative_credentials", lambda: SimpleNamespace(ensure_fresh=ensure_fresh)
    )
    monkeypatch.setattr(git_ops, "_run_git", fake_git)

    assert (await git_ops.git_push(branch="henchmen/task-1"))["success"] is True
    assert events == [f"refresh {tmp_path}", "push --set-upstream origin henchmen/task-1"]


@pytest.mark.asyncio
async def test_git_force_push_tool_refreshes_before_pushing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from henchmen.arsenal.tools import git_ops

    monkeypatch.setenv("HENCHMEN_ALLOW_FORCE_PUSH", "true")
    monkeypatch.setattr(git_ops, "_resolve_working_dir", lambda working_dir: str(tmp_path))
    events: list[str] = []

    async def ensure_fresh(workspace_dir: str | None = None) -> str:
        events.append("refresh")
        return REFRESHED

    async def fake_git(*args: str, working_dir: str = "") -> dict[str, Any]:
        events.append(" ".join(args))
        return {"stdout": "", "stderr": "", "return_code": 0, "success": True}

    monkeypatch.setattr(
        github_credentials, "get_operative_credentials", lambda: SimpleNamespace(ensure_fresh=ensure_fresh)
    )
    monkeypatch.setattr(git_ops, "_run_git", fake_git)

    assert (await git_ops.git_force_push(branch="henchmen/task-1"))["success"] is True
    assert events == ["refresh", "push --force-with-lease origin henchmen/task-1"]


@pytest.mark.parametrize(
    ("tool_name", "kwargs"),
    [
        ("create_pull_request", {"title": "t", "body": "b", "head_branch": "henchmen/x"}),
        ("comment_on_pr", {"pr_number": 7, "body": "Looks good"}),
        ("label_issue", {"issue_number": 3, "labels": ["bug"]}),
        ("assign_issue", {"issue_number": 3, "assignees": ["octocat"]}),
        ("fetch_issues", {}),
    ],
)
@pytest.mark.asyncio
async def test_github_tools_use_the_refreshed_token(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, kwargs: dict[str, Any]
) -> None:
    from henchmen.arsenal.tools import github as github_tools

    credentials = SimpleNamespace(token=INITIAL)
    seen: list[str | None] = []

    async def ensure_fresh(workspace_dir: str | None = None) -> str:
        seen.append(workspace_dir)
        credentials.token = REFRESHED
        return credentials.token

    credentials.ensure_fresh = ensure_fresh  # type: ignore[attr-defined]
    monkeypatch.setattr(github_credentials, "get_operative_credentials", lambda: credentials)
    monkeypatch.setattr(github_tools, "current_repo_slug", lambda: "acme/webapp")
    monkeypatch.setenv("WORKSPACE_DIR", "/workspace/task-1")

    with patch("github.Github"), patch("github.Auth.Token") as auth_token:
        await getattr(github_tools, tool_name)(**kwargs)

    assert seen == ["/workspace/task-1"]
    auth_token.assert_called_once_with(REFRESHED)
