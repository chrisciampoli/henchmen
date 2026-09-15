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
    GitHubAppKeyError,
    GitHubAuthError,
    GitHubCredentialsProvider,
    GitHubRepositoryReferenceError,
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
    parse_repository,
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
    with pytest.raises(GitHubAppKeyError, match="could not sign") as exc_info:
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
    with pytest.raises(GitHubAppKeyError, match="missing or unreadable"):
        load_app_private_key(tmp_path / "absent.pem")


def test_load_app_private_key_refuses_a_directory(tmp_path: Path) -> None:
    with pytest.raises(GitHubAppKeyError, match="missing or unreadable") as exc_info:
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


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("acme/webapp", ("acme", "webapp")),
        ("  acme/web.app_1-x  ", ("acme", "web.app_1-x")),
        ("https://github.com/acme/webapp", ("acme", "webapp")),
        ("https://github.com/acme/webapp.git", ("acme", "webapp")),
        ("https://ghe.example.test:8443/acme/webapp.git", ("acme", "webapp")),
        ("git@github.com:acme/webapp.git", ("acme", "webapp")),
        ("git@github.com:acme/webapp", ("acme", "webapp")),
        (None, None),
        ("", None),
    ],
)
def test_parse_repository_accepted_shapes(repo: str | None, expected: tuple[str, str] | None) -> None:
    assert parse_repository(repo) == expected


@pytest.mark.parametrize(
    "repo",
    [
        "webapp",
        "acme/web app",
        "acme/..",
        "acme/we$b",
        "acme/webapp/tree/main",
        "https://github.com/acme/webapp/tree/main",
        "https://github.com/webapp.git",
        "http://github.com/acme/webapp.git",
        "https://x-access-token:ghs_x@github.com/acme/webapp.git",
        "ssh://git@github.com/acme/webapp.git",
        "git@github.com:acme/webapp/extra.git",
        "acme/webapp.git",
        "-acme/webapp",
    ],
)
def test_invalid_repository_never_reaches_github(key_file: Path, github: FakeGitHub, clock: _Clock, repo: str) -> None:
    with pytest.raises(GitHubRepositoryReferenceError, match="owner/name or a GitHub clone URL"):
        _provider(key_file, github, clock).token(repo)
    assert github.requests == []


@pytest.mark.parametrize("repo", ["https://github.com/acme/webapp.git", "git@github.com:acme/webapp.git"])
def test_clone_urls_are_scoped_to_their_repository(
    key_file: Path, github: FakeGitHub, clock: _Clock, repo: str
) -> None:
    provider = _provider(key_file, github, clock)
    assert provider.token(repo) == "ghs_fake0001"
    assert provider.token("acme/webapp") == "ghs_fake0001"  # same cache entry as owner/name
    assert [record["repositories"] for record in github.minted] == [["webapp"]]


def test_token_scoped_to_another_owner_is_refused(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    github.installations["99"] = FakeGitHub.installation("99", "someone-else")
    provider = _provider(key_file, github, clock)
    with pytest.raises(GitHubAuthError, match="different repository"):
        provider.token("acme/webapp")
    with pytest.raises(GitHubAuthError, match="different repository"):
        provider.token("acme/webapp")  # nothing was cached
    assert len(_mints(github)) == 2


@pytest.mark.parametrize(
    "repositories",
    [
        [{"full_name": "other/webapp"}],
        [{"owner": {"login": "other"}, "name": "webapp"}],
        [{"name": "api"}],
        [{"full_name": "acme/webapp"}, {"full_name": "acme/api"}],
        [{"id": 1}],
        [],
        "acme/webapp",
    ],
)
def test_unexpected_token_repositories_are_refused(key_file: Path, clock: _Clock, repositories: object) -> None:
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock.now + 3600))
    body = {"token": "ghs_x", "expires_at": expires, "repositories": repositories}
    provider = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id="4242", private_key_path=key_file, installation_id="99"),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(201, json=body))
        ),
        clock=clock,
    )
    with pytest.raises(GitHubAuthError):
        provider.token("acme/webapp")


def test_matching_token_repositories_are_accepted_case_insensitively(key_file: Path, clock: _Clock) -> None:
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock.now + 3600))
    body = {
        "token": "ghs_x",
        "expires_at": expires,
        "repositories": [{"full_name": "ACME/WebApp", "owner": {"login": "Acme"}, "name": "webapp"}],
    }
    provider = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id="4242", private_key_path=key_file, installation_id="99"),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(201, json=body))
        ),
        clock=clock,
    )
    assert provider.token("acme/webapp") == "ghs_x"


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
    clock.now += 3600 - MAX_MIN_TTL_SECONDS + 1  # just under the capped minimum left
    assert provider.token(min_ttl_seconds=7200) == "ghs_fake0002"


def test_ttl_cap_leaves_ten_minutes_for_clock_skew() -> None:
    assert MAX_MIN_TTL_SECONDS == 3000
    assert 3600 - MAX_MIN_TTL_SECONDS >= 600


@pytest.mark.parametrize("min_ttl", [3000, 7200])
def test_maximum_lifetime_request_tolerates_clock_skew(key_file: Path, clock: _Clock, min_ttl: int) -> None:
    """Our clock 500 s ahead of GitHub's: a fresh one-hour token has about 3100 s left here, still enough."""
    github = FakeGitHub(clock=lambda: clock.now - 500)
    github.installations["99"] = FakeGitHub.installation("99", "acme")
    provider = _provider(key_file, github, clock)
    assert provider.token("acme/webapp", min_ttl_seconds=min_ttl) == "ghs_fake0001"
    assert provider.token("acme/webapp", min_ttl_seconds=min_ttl) == "ghs_fake0001"
    assert len(_mints(github)) == 1


def test_a_token_that_is_already_too_short_lived_is_refused(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    github.token_lifetime_seconds = 60
    with pytest.raises(GitHubAuthError, match="check the server clock"):
        _provider(key_file, github, clock).token()


def test_a_token_shorter_than_the_capped_minimum_is_refused(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    github.token_lifetime_seconds = MAX_MIN_TTL_SECONDS - 10
    provider = _provider(key_file, github, clock)
    with pytest.raises(GitHubAuthError, match="check the server clock"):
        provider.token("acme/webapp", min_ttl_seconds=7200)
    assert provider.token("acme/webapp") == "ghs_fake0002"  # enough for the default minimum


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


@pytest.mark.parametrize(
    "api_url", ["http://api.github.com", "ftp://api.github.com", "https://user:pw@api.github.com", "not a url"]
)
def test_provider_refuses_an_insecure_api_url(api_url: str) -> None:
    with pytest.raises(GitHubAuthError, match="GitHub API URL"):
        GitHubCredentialsProvider(app=None, api_url=api_url)


@pytest.mark.parametrize("api_url", ["http://127.0.0.1:9000", "http://localhost:9000/", "http://fakes:9000/github/api"])
def test_provider_accepts_loopback_and_compose_http_urls(api_url: str, key_file: Path, clock: _Clock) -> None:
    seen: list[str] = []
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock.now + 3600))

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(201, json={"token": "ghs_x", "expires_at": expires})

    provider = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id="4242", private_key_path=key_file, installation_id="99"),
        api_url=api_url,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock,
    )
    assert provider.token() == "ghs_x"
    assert seen == [api_url.rstrip("/") + "/app/installations/99/access_tokens"]


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


_APP_FIELDS = {
    "github_app_id": ("HENCHMEN_GITHUB_APP_ID", "4242"),
    "github_app_installation_id": ("HENCHMEN_GITHUB_APP_INSTALLATION_ID", "99"),
    "github_app_private_key_path": ("HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH", "<key file>"),
}
_PARTIAL_COMBINATIONS = [
    ("github_app_id",),
    ("github_app_installation_id",),
    ("github_app_private_key_path",),
    ("github_app_id", "github_app_installation_id"),
    ("github_app_id", "github_app_private_key_path"),
    ("github_app_installation_id", "github_app_private_key_path"),
]


def _partial_settings(present: tuple[str, ...], key_file: Path) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "github_token": "ghp_personal"}
    for field in _APP_FIELDS:
        if field in present:
            values[field] = str(key_file) if field == "github_app_private_key_path" else _APP_FIELDS[field][1]
        else:
            values[field] = "   "  # blank counts as unset
    return Settings(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("present", _PARTIAL_COMBINATIONS)
async def test_partly_configured_app_never_uses_the_pat(
    monkeypatch: pytest.MonkeyPatch, key_file: Path, present: tuple[str, ...]
) -> None:
    def no_http() -> httpx.Client:
        raise AssertionError("no HTTP for a partly configured App")

    monkeypatch.setattr(github_auth, "_default_client", no_http)
    monkeypatch.setattr(github_auth, "_default_async_client", no_http)
    settings = _partial_settings(present, key_file)
    provider = get_credentials_provider(settings)
    missing = sorted(_APP_FIELDS[field][0] for field in _APP_FIELDS if field not in present)

    calls = [
        lambda: provider.token("acme/webapp"),
        lambda: provider.installation_token("acme/webapp"),
        lambda: get_github_token("acme/webapp", settings=settings),
    ]
    errors: list[GitHubAuthError] = []
    for call in calls:
        with pytest.raises(GitHubAuthError, match="only partly configured") as exc_info:
            call()
        errors.append(exc_info.value)
    for coroutine_call in (
        lambda: provider.token_async("acme/webapp"),
        lambda: provider.installation_token_async("acme/webapp"),
        lambda: get_github_token_async("acme/webapp", settings=settings),
        lambda: get_installation_token_async("acme/webapp", settings=settings),
    ):
        with pytest.raises(GitHubAuthError, match="only partly configured") as exc_info:
            await coroutine_call()
        errors.append(exc_info.value)

    for error in errors:
        message = str(error)
        assert sorted(name for name in (entry[0] for entry in _APP_FIELDS.values()) if name in message) == missing
        for value in ("ghp_personal", "4242", "99", str(key_file)):
            assert value not in message
    assert not provider.uses_app


@pytest.mark.parametrize("present", _PARTIAL_COMBINATIONS)
def test_partly_configured_app_is_a_runtime_problem(key_file: Path, present: tuple[str, ...]) -> None:
    problems = _partial_settings(present, key_file).validate_for_runtime()
    (problem,) = [problem for problem in problems if "only partly configured" in problem]
    for field, (env_name, _value) in _APP_FIELDS.items():
        assert (env_name in problem) is (field not in present)


def test_partial_app_message_ignores_non_strings() -> None:
    assert github_auth.partial_app_message(MagicMock(), MagicMock(), MagicMock()) is None
    assert github_auth.partial_app_message("", "", "") is None
    assert github_auth.partial_app_message("1", "/k.pem", "2") is None


def test_key_file_validation_matches_the_loader(key_file: Path, tmp_path: Path) -> None:
    """validate_for_runtime refuses exactly what load_app_private_key refuses."""
    link = tmp_path / "link.pem"
    candidates = [key_file, tmp_path / "missing.pem", tmp_path]
    try:
        os.symlink(key_file, link)
        candidates.append(link)
    except (OSError, NotImplementedError):
        pass  # symbolic links cannot be created here; the other cases still compare both checks
    for path in candidates:
        settings = Settings(
            **{
                "_env_file": None,
                "github_app_id": "1",
                "github_app_installation_id": "2",
                "github_app_private_key_path": str(path),
            }
        )
        flagged = any("HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH" in problem for problem in settings.validate_for_runtime())
        try:
            load_app_private_key(path)
            loads = True
        except GitHubAuthError:
            loads = False
        assert flagged is not loads, path


def test_symlinked_key_file_is_a_runtime_problem(key_file: Path, tmp_path: Path) -> None:
    link = tmp_path / "link.pem"
    try:
        os.symlink(key_file, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links cannot be created here")
    settings = Settings(
        **{
            "_env_file": None,
            "github_app_id": "1",
            "github_app_installation_id": "2",
            "github_app_private_key_path": str(link),
        }
    )
    assert any("symbolic link" in problem for problem in settings.validate_for_runtime())


@pytest.mark.parametrize("field", ["github_api_url", "github_web_url"])
@pytest.mark.parametrize(
    "url",
    [
        "https://api.github.com",
        "https://ghe.example.test/api/v3/",
        "http://127.0.0.1:9000",
        "http://localhost:9000/github",
        "http://[::1]:9000",
        "http://127.0.0.2:9000",
        "http://127.255.255.254",
        "http://fakes:9000/github/api",
        "http://fake-github",
    ],
)
def test_github_urls_accept_https_loopback_and_compose_hosts(field: str, url: str) -> None:
    assert getattr(Settings(**{"_env_file": None, field: url}), field) == url


@pytest.mark.parametrize("field", ["github_api_url", "github_web_url"])
@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com",
        "http://10.0.0.5:9000",
        "http://fakes.internal:9000",
        "ftp://api.github.com",
        "https://user:secret@api.github.com",
        "https://",
        "api.github.com",
        "https://api.github.com:99999",
        # Plain http to an IP literal that is not loopback, in any spelling, or a non-service name.
        "http://2130706433",
        "http://0x7f000001",
        "http://0177.0.0.1",
        "http://127.1",
        "http://0.0.0.0:9000",
        "http://[::2]:9000",
        "http://[fe80::1]",
        "http://1fakes:9000",
        "http://fakes_internal:9000",
        "http://-fakes",
        # N1: a query string, a fragment or URL parameters are refused for GitHub too.
        "https://api.github.com?x=1",
        "https://api.github.com#frag",
        "https://api.github.com/path;param=1",
    ],
)
def test_github_urls_refuse_insecure_or_malformed_values(field: str, url: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        Settings(**{"_env_file": None, field: url})
    assert field in str(exc_info.value)


@pytest.mark.parametrize("field", ["github_api_url", "github_web_url"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_github_urls_fall_back_to_the_default(field: str, blank: str) -> None:
    settings = Settings(**{"_env_file": None, "provider": "local", field: blank})
    assert getattr(settings, field) == Settings.model_fields[field].default
    assert getattr(settings, field).startswith("https://")


@pytest.mark.parametrize("field", ["github_api_url", "github_web_url"])
@pytest.mark.parametrize(
    "url",
    ["https://chris:hunter2-pw@api.github.com", "http://ghp_leakedtoken123@127.0.0.1:9000", "https://:hunter2-pw@x.io"],
)
def test_github_urls_with_credentials_are_refused_without_echoing_them(field: str, url: str) -> None:
    from pydantic import ValidationError

    from henchmen.config.settings import require_secure_github_url

    with pytest.raises(ValueError, match="must not contain a user name or password") as plain:
        require_secure_github_url(url)
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{"_env_file": None, field: url})
    for text in (str(plain.value), str(exc_info.value)):
        assert "hunter2" not in text
        assert "ghp_leakedtoken123" not in text
        assert url not in text
    assert "user name or password" in str(exc_info.value)


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


# -- refusal kinds (Task 6 review carry-over) --------------------------------------------


def test_a_repository_outside_the_installation_is_an_access_error(
    key_file: Path, github: FakeGitHub, clock: _Clock
) -> None:
    from henchmen.utils.github_auth import GitHubRepositoryAccessError

    github.restrict_token_repositories = True
    github.repositories = [FakeGitHub.repository("acme/webapp")]
    with pytest.raises(GitHubRepositoryAccessError) as exc_info:
        _provider(key_file, github, clock).token("acme/elsewhere")
    assert exc_info.value.status_code == 422


def test_a_token_scoped_to_another_owner_is_an_access_error(key_file: Path, github: FakeGitHub, clock: _Clock) -> None:
    from henchmen.utils.github_auth import GitHubRepositoryAccessError

    with pytest.raises(GitHubRepositoryAccessError):
        _provider(key_file, github, clock).token("globex/webapp")


def test_a_token_repository_entry_without_identity_is_not_an_access_error(key_file: Path, clock: _Clock) -> None:
    from henchmen.utils.github_auth import GitHubRepositoryAccessError

    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock.now + 3600))
    body = {"token": "ghs_x", "expires_at": expires, "repositories": [{"id": 1}]}
    provider = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id="4242", private_key_path=key_file, installation_id="99"),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(201, json=body))
        ),
        clock=clock,
    )
    with pytest.raises(GitHubAuthError, match="unreadable repository list") as exc_info:
        provider.token("acme/webapp")
    assert not isinstance(exc_info.value, GitHubRepositoryAccessError)


def test_a_partly_configured_app_raises_a_configuration_error() -> None:
    from henchmen.utils.github_auth import GitHubAppConfigurationError

    provider = GitHubCredentialsProvider(app=None, pat="ghp_personal", partial_app_problem="only partly configured")
    with pytest.raises(GitHubAppConfigurationError, match="only partly configured"):
        provider.token("acme/webapp")


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_other_refusals_are_not_access_errors(key_file: Path, github: FakeGitHub, clock: _Clock, status: int) -> None:
    from henchmen.utils.github_auth import GitHubRepositoryAccessError

    github.token_status = status
    with pytest.raises(GitHubAuthError) as exc_info:
        _provider(key_file, github, clock).token("acme/webapp")
    assert not isinstance(exc_info.value, GitHubRepositoryAccessError)
    assert exc_info.value.status_code == status


def test_an_unreachable_github_has_no_status(key_file: Path, clock: _Clock) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    provider = GitHubCredentialsProvider(
        app=GitHubAppConfig(app_id="4242", private_key_path=key_file, installation_id="99"),
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(unreachable)),
        clock=clock,
    )
    with pytest.raises(GitHubAuthError) as exc_info:
        provider.token("acme/webapp")
    assert exc_info.value.status_code is None


# -- server-side consumers (Task 7) ------------------------------------------------------


def test_server_components_do_not_read_the_pat_directly() -> None:
    """Every server-side GitHub consumer goes through the credentials provider (spec §5.1)."""
    import re

    import henchmen
    import henchmen.utils.git

    allowed = {
        "config/settings.py",  # defines the field
        "utils/github_auth.py",  # the provider's PAT fallback
        "cli/doctor.py",  # reports whether a PAT is configured
        "operative/bootstrap.py",  # operative side: reads the token LairManager injected
        "operative/github_credentials.py",  # operative side: the injected token, refreshed via the internal API
    }
    # Attribute reads, string-keyed reads (getattr / model_dump()["github_token"]) and raw environment reads.
    # ``\benviron\b`` / ``\bgetenv\b`` so prose such as "environment sets ... GITHUB_TOKEN" is not a read.
    patterns = (
        re.compile(r"\.github_token\b"),
        re.compile(r"[\"']github_token[\"']"),
        re.compile(r"\benviron\b.*GITHUB_TOKEN"),
        re.compile(r"\bgetenv\b.*GITHUB_TOKEN"),
    )
    root = Path(henchmen.__file__).parent
    offenders = sorted(
        f"{path.relative_to(root).as_posix()}: {pattern.pattern}"
        for path in root.rglob("*.py")
        if path.relative_to(root).as_posix() not in allowed
        for pattern in patterns
        if pattern.search(path.read_text(encoding="utf-8"))
    )
    assert offenders == []
    # The patterns themselves still catch what they are meant to.
    samples = (
        "settings.github_token",
        'getattr(settings, "github_token")',
        'os.environ.get("GITHUB_TOKEN")',
        'os.getenv("GITHUB_TOKEN")',
        "getenv('HENCHMEN_GITHUB_TOKEN')",
    )
    assert all(any(pattern.search(sample) for pattern in patterns) for sample in samples)
    assert not any(pattern.search("environment sets ``GITHUB_TOKEN``") for pattern in patterns)
    assert not hasattr(henchmen.utils.git, "get_github_token")
