"""Tests for the GitHub credentials provider: App installation tokens and the PAT fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import jwt
import pytest

from henchmen.config.settings import Settings
from henchmen.utils import github_auth
from henchmen.utils.github_auth import (
    JWT_BACKDATE_SECONDS,
    JWT_LIFETIME_SECONDS,
    MAX_MIN_TTL_SECONDS,
    GitHubAppConfig,
    GitHubAuthError,
    GitHubCredentialsProvider,
    InstallationToken,
    app_jwt_for,
    build_app_jwt,
    get_credentials_provider,
    get_github_token,
    get_github_token_async,
    get_installation_token_async,
    github_error_detail,
    load_app_private_key,
    parse_expiry,
)
from tests.unit.github_fakes import FakeGitHub, app_key_pair


class _Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "github-app.pem"
    path.write_bytes(app_key_pair()[0])
    return path


@pytest.fixture
def github(clock: _Clock) -> FakeGitHub:
    fake = FakeGitHub(clock=clock)
    fake.installations["99"] = FakeGitHub.installation("99", "acme")
    return fake


def _provider(key_file: Path, github: FakeGitHub, clock: _Clock) -> GitHubCredentialsProvider:
    return GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id=github.app_id, private_key_path=key_file, installation_id="99"),
        pat="ghp_must_not_be_used",
        client_factory=github.client,
        async_client_factory=github.async_client,
        clock=clock,
    )


def _mints(github: FakeGitHub) -> list[httpx.Request]:
    return [request for request in github.requests if request.url.path.endswith("/access_tokens")]


def _decode(token: str) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(
        token, app_key_pair()[1], algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )
    return claims


# -- app JWTs and key loading ---------------------------------------------------


def test_app_jwt_claims() -> None:
    now = 1_900_000_000.0
    token = build_app_jwt("4242", app_key_pair()[0], now=now)
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    assert _decode(token) == {"iat": int(now) - 60, "exp": int(now) + 540, "iss": "4242"}
    assert (JWT_BACKDATE_SECONDS, JWT_LIFETIME_SECONDS) == (60, 540)
    assert JWT_BACKDATE_SECONDS + JWT_LIFETIME_SECONDS <= 600


def test_unusable_private_key_raises() -> None:
    with pytest.raises(GitHubAuthError, match="could not sign") as exc_info:
        build_app_jwt("4242", b"not a pem", now=time.time())
    assert exc_info.value.__cause__ is None


def test_app_jwt_for_loads_the_key_file(key_file: Path) -> None:
    now = 1_900_000_000.0
    assert _decode(app_jwt_for("4242", key_file, now=now)) == {
        "iat": int(now) - 60,
        "exp": int(now) + 540,
        "iss": "4242",
    }
    assert abs(_decode(app_jwt_for("4242", key_file))["iat"] - (time.time() - 60)) < 5


def test_load_app_private_key_reads_a_regular_file(key_file: Path) -> None:
    assert load_app_private_key(key_file) == app_key_pair()[0]


def test_load_app_private_key_missing_file(tmp_path: Path) -> None:
    with pytest.raises(GitHubAuthError, match="missing or unreadable"):
        load_app_private_key(tmp_path / "absent.pem")


def test_load_app_private_key_refuses_a_directory(tmp_path: Path) -> None:
    with pytest.raises(GitHubAuthError, match="missing or unreadable") as exc_info:
        load_app_private_key(tmp_path)
    assert exc_info.value.__cause__ is None


def test_load_app_private_key_refuses_a_symlink(key_file: Path, tmp_path: Path) -> None:
    link = tmp_path / "link.pem"
    try:
        os.symlink(key_file, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links cannot be created here")
    with pytest.raises(GitHubAuthError, match="missing or unreadable") as exc_info:
        load_app_private_key(link)
    assert "PRIVATE KEY" not in str(exc_info.value)


def test_app_config_needs_all_three_values(key_file: Path) -> None:
    assert GitHubAppConfig.from_values("1", str(key_file), "2") is not None
    assert GitHubAppConfig.from_values("1", str(key_file), "") is None
    assert GitHubAppConfig.from_values("", str(key_file), "2") is None
    assert GitHubAppConfig.from_values(MagicMock(), MagicMock(), MagicMock()) is None


def test_app_config_from_settings(key_file: Path) -> None:
    settings = Settings(
        **{
            "_env_file": None,
            "github_app_id": " 7 ",
            "github_app_installation_id": "8",
            "github_app_private_key_path": str(key_file),
        }
    )
    assert GitHubAppConfig.from_settings(settings) == GitHubAppConfig(
        app_id="7", private_key_path=key_file, installation_id="8"
    )


# -- expiry parsing -------------------------------------------------------------


def test_parse_expiry() -> None:
    expected = datetime(2026, 9, 15, 10, 0, tzinfo=UTC).timestamp()
    assert parse_expiry("2026-09-15T10:00:00Z") == expected
    assert parse_expiry("2026-09-15T10:00:00+00:00") == expected
    assert parse_expiry("2026-09-15T10:00:00") == expected  # no zone: UTC


@pytest.mark.parametrize("raw", [None, "", "  ", 17, "tomorrow"])
def test_parse_expiry_rejects_unusable_values(raw: object) -> None:
    with pytest.raises(GitHubAuthError, match="expiry"):
        parse_expiry(raw)


# -- PAT fallback ---------------------------------------------------------------


def test_without_an_app_the_pat_is_returned_and_github_is_not_called() -> None:
    provider = GitHubCredentialsProvider(
        app=None, pat="ghp_personal", client_factory=lambda: pytest.fail("no HTTP without an App")
    )
    assert not provider.uses_app
    assert provider.app is None
    assert provider.token("acme/webapp") == "ghp_personal"
    assert "ghp_personal" not in repr(provider)


# -- minting and caching --------------------------------------------------------


def test_mints_an_installation_token_with_the_app_jwt(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    provider = _provider(key_file, github, clock)
    assert provider.token() == "ghs_fake0001"
    (request,) = _mints(github)
    assert request.url.path == "/app/installations/99/access_tokens"
    assert json.loads(request.content) == {}
    assert request.headers["x-github-api-version"] == "2022-11-28"


def test_token_is_cached(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    provider = _provider(key_file, github, clock)
    assert provider.token() == provider.token() == "ghs_fake0001"
    assert len(_mints(github)) == 1


def test_refreshes_five_minutes_before_expiry(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    provider = _provider(key_file, github, clock)
    provider.token()
    clock.now += 3600 - 301
    assert provider.token() == "ghs_fake0001"
    clock.now += 2
    assert provider.token() == "ghs_fake0002"
    assert len(_mints(github)) == 2


def test_repository_scoped_tokens_are_cached_separately(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    provider = _provider(key_file, github, clock)
    assert provider.token("acme/webapp") == "ghs_fake0001"
    assert provider.token("ACME/WebApp") == "ghs_fake0001"
    assert provider.token() == "ghs_fake0002"
    assert [record["repositories"] for record in github.minted] == [["webapp"], None]


def test_clone_url_is_scoped_to_its_repository(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    _provider(key_file, github, clock).token("https://github.com/acme/webapp.git")
    assert [record["repositories"] for record in github.minted] == [["webapp"]]


@pytest.mark.parametrize("repo", ["acme/web app", "acme/..", "acme/we$b"])
def test_invalid_repository_name_never_widens_to_the_installation(
    key_file: Path, github: FakeGitHub, clock: _Clock, repo: str
) -> None:
    with pytest.raises(GitHubAuthError, match="repository name"):
        _provider(key_file, github, clock).token(repo)
    assert github.requests == []


def test_minimum_lifetime_forces_a_fresh_token(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    provider = _provider(key_file, github, clock)
    provider.token()
    clock.now += 1500
    assert provider.token(min_ttl_seconds=1800) == "ghs_fake0001"
    assert provider.token(min_ttl_seconds=2400) == "ghs_fake0002"


def test_minimum_lifetime_above_an_hour_is_capped(
    key_file: Path, github: FakeGitHub, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    provider = _provider(key_file, github, clock)
    with caplog.at_level(logging.WARNING, logger="henchmen.utils.github_auth"):
        assert provider.token(min_ttl_seconds=7200) == "ghs_fake0001"
    assert str(MAX_MIN_TTL_SECONDS) in caplog.text
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 1
    clock.now += 3600 - MAX_MIN_TTL_SECONDS + 1  # 3299s or less left: below the capped minimum
    assert provider.token(min_ttl_seconds=7200) == "ghs_fake0002"


def test_a_token_that_is_already_too_short_lived_is_refused(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    github.token_lifetime_seconds = 60
    with pytest.raises(GitHubAuthError, match="check the server clock"):
        _provider(key_file, github, clock).token()


def test_invalidate_forgets_cached_tokens(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    provider = _provider(key_file, github, clock)
    provider.token()
    provider.invalidate()
    assert provider.token() == "ghs_fake0002"


def test_api_url_is_configurable(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    provider = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id=github.app_id, private_key_path=key_file, installation_id="99"),
        api_url="https://ghe.example.test/",
        client_factory=github.client,
        clock=clock,
    )
    provider.token()
    (request,) = _mints(github)
    assert str(request.url) == "https://ghe.example.test/app/installations/99/access_tokens"


def test_default_clients_ignore_the_environment_and_time_out() -> None:
    sync_client = github_auth._default_client()
    async_client = github_auth._default_async_client()
    try:
        for client in (sync_client, async_client):
            assert client.trust_env is False
            assert client.timeout.connect == github_auth._HTTP_TIMEOUT
            assert client.timeout.read == github_auth._HTTP_TIMEOUT
    finally:
        sync_client.close()
        asyncio.run(async_client.aclose())


# -- failures -------------------------------------------------------------------


def test_refusal_raises_without_leaking_the_jwt(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    github.token_status = 403
    with pytest.raises(GitHubAuthError, match="HTTP 403") as exc_info:
        _provider(key_file, github, clock).token()
    assert "eyJ" not in str(exc_info.value)
    assert "ghp_must_not_be_used" not in str(exc_info.value)


def test_missing_key_file_raises_before_calling_github(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    key_file.unlink()
    with pytest.raises(GitHubAuthError, match="missing or unreadable"):
        _provider(key_file, github, clock).token()
    assert github.requests == []


def test_network_failure_raises(key_file: Path, clock: _Clock) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    provider = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id="4242", private_key_path=key_file, installation_id="99"),
        pat="ghp_personal",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(unreachable)),
        clock=clock,
    )
    with pytest.raises(GitHubAuthError, match="Could not reach GitHub"):
        provider.token()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201, content=b"not json"),
        httpx.Response(201, json={"expires_at": "2099-01-01T00:00:00Z"}),
        httpx.Response(201, json={"token": "ghs_x", "expires_at": "soon"}),
    ],
)
def test_malformed_mint_responses_raise(key_file: Path, clock: _Clock, response: httpx.Response) -> None:
    provider = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id="4242", private_key_path=key_file, installation_id="99"),
        pat="ghp_personal",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(lambda request: response)),
        clock=clock,
    )
    with pytest.raises(GitHubAuthError):
        provider.token()


def test_error_detail_is_redacted_and_truncated() -> None:
    pem = app_key_pair()[0].decode()
    app_jwt = build_app_jwt("4242", app_key_pair()[0], now=time.time())
    detail = github_error_detail(httpx.Response(401, json={"message": f"bad {app_jwt} {pem}" + "x" * 500}))
    assert detail.startswith("HTTP 401: bad ")
    assert "eyJ" not in detail
    assert "PRIVATE KEY" not in detail
    assert len(detail) < 260
    assert github_error_detail(httpx.Response(500, content=b"<html>")) == "HTTP 500"


def test_logs_and_errors_never_contain_a_pem_or_jwt(
    key_file: Path, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    pem = app_key_pair()[0].decode()
    app_jwt = build_app_jwt("4242", app_key_pair()[0], now=clock.now)
    key_body_line = pem.splitlines()[1]
    github = FakeGitHub(clock=clock)
    github.installations["99"] = FakeGitHub.installation("99", "acme")

    def echoing(request: httpx.Request) -> httpx.Response:
        # A hostile or buggy upstream echoing credentials back in its error message.
        return httpx.Response(401, json={"message": f"rejected {request.headers['authorization']} {app_jwt}\n{pem}"})

    refusing = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id="4242", private_key_path=key_file, installation_id="99"),
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(echoing)),
        clock=clock,
    )
    minting = _provider(key_file, github, clock)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(GitHubAuthError) as exc_info:
            refusing.token("acme/webapp")
        issued = minting.installation_token("acme/webapp")

    records = [record for record in caplog.records if record.name == "henchmen.utils.github_auth"]
    assert len(records) == 2  # the refusal and the successful mint were both logged
    logged = caplog.text + str(exc_info.value)
    for secret in ("PRIVATE KEY", key_body_line, "eyJ", issued.token):
        assert secret not in logged


# -- async ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_variant(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    provider = _provider(key_file, github, clock)
    assert await provider.token_async("acme/webapp") == "ghs_fake0001"
    assert await provider.token_async("acme/webapp") == "ghs_fake0001"
    assert len(_mints(github)) == 1


def _slow_async_factory(github: FakeGitHub) -> github_auth.AsyncClientFactory:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)  # yield to the loop so concurrent callers really overlap
        return github(request)

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_concurrent_async_callers_share_one_mint(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    provider = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id=github.app_id, private_key_path=key_file, installation_id="99"),
        async_client_factory=_slow_async_factory(github),
        clock=clock,
    )
    tokens = await asyncio.gather(provider.token_async("acme/webapp"), provider.token_async("acme/webapp"))
    assert tokens == ["ghs_fake0001", "ghs_fake0001"]
    assert len(_mints(github)) == 1


@pytest.mark.asyncio
async def test_concurrent_async_callers_for_different_repositories_each_mint(
    key_file: Path, github: FakeGitHub, clock: _Clock
) -> None:
    provider = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id=github.app_id, private_key_path=key_file, installation_id="99"),
        async_client_factory=_slow_async_factory(github),
        clock=clock,
    )
    tokens = await asyncio.gather(provider.token_async("acme/webapp"), provider.token_async("acme/api"))
    assert sorted(tokens) == ["ghs_fake0001", "ghs_fake0002"]
    assert sorted(record["repositories"][0] for record in github.minted) == ["api", "webapp"]


def test_async_locks_work_across_event_loops(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    provider = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id=github.app_id, private_key_path=key_file, installation_id="99"),
        async_client_factory=_slow_async_factory(github),
        clock=clock,
    )

    async def pair() -> list[str]:
        return list(await asyncio.gather(provider.token_async("acme/webapp"), provider.token_async("acme/webapp")))

    assert asyncio.run(pair()) == ["ghs_fake0001", "ghs_fake0001"]
    clock.now += 3600
    assert asyncio.run(pair()) == ["ghs_fake0002", "ghs_fake0002"]
    assert len(_mints(github)) == 2


def test_concurrent_threads_share_one_mint(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        time.sleep(0.05)
        return github(request)

    provider = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id=github.app_id, private_key_path=key_file, installation_id="99"),
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(slow)),
        clock=clock,
    )
    results: list[str] = []
    threads = [threading.Thread(target=lambda: results.append(provider.token("acme/webapp"))) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == ["ghs_fake0001"] * 4
    assert len(_mints(github)) == 1


@pytest.mark.asyncio
async def test_installation_token_carries_its_expiry(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    issued = await _provider(key_file, github, clock).installation_token_async("acme/webapp")
    assert issued.token == "ghs_fake0001"
    assert abs(issued.expires_at - (clock.now + 3600)) < 1
    assert issued.expires_at_iso() == datetime.fromtimestamp(issued.expires_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert "ghs_fake0001" not in repr(issued)
    assert "ghs_fake0001" not in str(issued)


def test_installation_token_expiry_iso_format() -> None:
    issued = InstallationToken(token="t", expires_at=datetime(2026, 9, 15, 10, 0, tzinfo=UTC).timestamp())
    assert issued.expires_at_iso() == "2026-09-15T10:00:00Z"


def test_installation_token_requires_an_app() -> None:
    with pytest.raises(GitHubAuthError, match="No GitHub App"):
        GitHubCredentialsProvider(app=None, pat="ghp_personal").installation_token()


@pytest.mark.asyncio
async def test_async_installation_token_requires_an_app() -> None:
    with pytest.raises(GitHubAuthError, match="No GitHub App"):
        await GitHubCredentialsProvider(app=None, pat="ghp_personal").installation_token_async("acme/webapp")


# -- module-level helpers and Settings ------------------------------------------------


def test_settings_without_an_app_use_the_pat() -> None:
    settings = Settings(**{"_env_file": None, "github_token": "ghp_personal"})
    assert get_github_token(settings=settings) == "ghp_personal"
    assert get_credentials_provider(settings) is get_credentials_provider(settings)


def test_changed_settings_get_a_different_provider() -> None:
    first = Settings(**{"_env_file": None, "github_token": "ghp_one"})
    second = Settings(**{"_env_file": None, "github_token": "ghp_two"})
    assert get_credentials_provider(first) is not get_credentials_provider(second)
    assert get_github_token(settings=second) == "ghp_two"


def test_reset_drops_cached_providers() -> None:
    settings = Settings(**{"_env_file": None, "github_token": "ghp_personal"})
    before = get_credentials_provider(settings)
    github_auth.reset_credentials_providers()
    assert get_credentials_provider(settings) is not before


@pytest.mark.asyncio
async def test_settings_with_an_app_mint_installation_tokens(
    monkeypatch: pytest.MonkeyPatch, key_file: Path, github: FakeGitHub
) -> None:
    monkeypatch.setattr(github_auth, "_default_async_client", github.async_client)
    settings = Settings(
        **{
            "_env_file": None,
            "github_token": "ghp_personal",
            "github_app_id": github.app_id,
            "github_app_installation_id": "99",
            "github_app_private_key_path": str(key_file),
        }
    )
    assert get_credentials_provider(settings).uses_app
    assert await get_github_token_async("acme/webapp", settings=settings) == "ghs_fake0001"
    issued = await get_installation_token_async("acme/webapp", settings=settings)
    assert issued.token == "ghs_fake0001"
    assert [record["repositories"] for record in github.minted] == [["webapp"]]


@pytest.mark.asyncio
async def test_a_configured_app_that_fails_never_falls_back_to_the_pat(
    monkeypatch: pytest.MonkeyPatch, key_file: Path, github: FakeGitHub
) -> None:
    github.token_status = 401
    monkeypatch.setattr(github_auth, "_default_async_client", github.async_client)
    settings = Settings(
        **{
            "_env_file": None,
            "github_token": "ghp_personal",
            "github_app_id": github.app_id,
            "github_app_installation_id": "99",
            "github_app_private_key_path": str(key_file),
        }
    )
    with pytest.raises(GitHubAuthError, match="HTTP 401"):
        await get_github_token_async("acme/webapp", settings=settings)


def test_partly_configured_app_is_a_runtime_problem() -> None:
    problems = Settings(**{"_env_file": None, "github_app_id": "1"}).validate_for_runtime()
    assert any("GitHub App is only partly configured" in problem for problem in problems)


def test_fully_configured_app_is_not_a_runtime_problem(key_file: Path) -> None:
    settings = Settings(
        **{
            "_env_file": None,
            "github_app_id": "1",
            "github_app_installation_id": "2",
            "github_app_private_key_path": str(key_file),
        }
    )
    assert not any(
        "GitHub App" in problem or "PRIVATE_KEY_PATH" in problem for problem in settings.validate_for_runtime()
    )


def test_missing_key_file_is_a_runtime_problem(tmp_path: Path) -> None:
    settings = Settings(
        **{
            "_env_file": None,
            "github_app_id": "1",
            "github_app_installation_id": "2",
            "github_app_private_key_path": str(tmp_path / "missing.pem"),
        }
    )
    assert any("HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH" in problem for problem in settings.validate_for_runtime())
