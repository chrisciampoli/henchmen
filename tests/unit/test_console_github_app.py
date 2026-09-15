"""Tests for the GitHub App manifest and the GitHub API calls the GitHub step makes."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from henchmen.console import github_app
from henchmen.console.state import SetupStep
from henchmen.console.steps import STEP_ROUTE_PREFIX, StepRoutes, validate_step_routes
from henchmen.console.steps.github import PUBLIC_ROUTE_PATHS, router
from henchmen.utils.github_auth import build_app_jwt
from tests.unit.github_fakes import FakeGitHub, app_key_pair

API = "https://api.github.com"


def test_manifest_contents() -> None:
    manifest = github_app.build_manifest(
        console_base_url="http://127.0.0.1:8123/", machine_name="Chris's MacBook Pro", suffix="ab12"
    )
    assert manifest == {
        "name": "Henchmen (Chris-s-MacBook-Pr-ab12)",
        "url": "https://github.com/chrisciampoli/henchmen",
        "description": "Henchmen opens pull requests for the tasks you give it.",
        "public": False,
        "redirect_url": "http://127.0.0.1:8123/console/api/steps/github/manifest-callback",
        "setup_url": "http://127.0.0.1:8123/console/api/steps/github/installed",
        "setup_on_update": False,
        "request_oauth_on_install": False,
        "hook_attributes": {"url": "http://127.0.0.1:8123/dispatch/webhooks/github", "active": False},
        "default_permissions": {
            "contents": "write",
            "pull_requests": "write",
            "issues": "write",
            "metadata": "read",
            "checks": "read",
            "actions": "read",
        },
        "default_events": [],
    }


def test_callback_paths_derive_from_the_step_prefix_and_pass_route_validation() -> None:
    assert f"{STEP_ROUTE_PREFIX}/github/manifest-callback" == github_app.MANIFEST_CALLBACK_PATH
    assert f"{STEP_ROUTE_PREFIX}/github/installed" == github_app.INSTALLED_CALLBACK_PATH
    validate_step_routes(SetupStep.GITHUB, StepRoutes(router=router, public_paths=PUBLIC_ROUTE_PATHS))
    assert all(path.startswith(f"{STEP_ROUTE_PREFIX}/github/") for path in PUBLIC_ROUTE_PATHS)


def test_manifest_never_requests_workflow_access() -> None:
    """Operatives must not modify CI workflows (A4): no ``workflows`` permission, at any level."""
    manifest = github_app.build_manifest(console_base_url="http://127.0.0.1:8000", machine_name="host")
    assert "workflows" not in manifest["default_permissions"]
    assert "workflows" not in github_app.APP_PERMISSIONS
    assert all(level != "admin" for level in manifest["default_permissions"].values())


@pytest.mark.parametrize("machine", ["", "x" * 80, "!!!", "a-very-long-host-name.example.internal"])
def test_app_names_fit_githubs_limit(machine: str) -> None:
    name = github_app.app_name(machine, "ab12")
    assert name.startswith("Henchmen (")
    assert name.endswith("ab12)")
    assert len(name) <= 34


def test_random_suffix_makes_names_unique() -> None:
    first = github_app.build_manifest(console_base_url="http://127.0.0.1:8000", machine_name="host")
    second = github_app.build_manifest(console_base_url="http://127.0.0.1:8000", machine_name="host")
    assert first["name"] != second["name"]


def test_form_actions() -> None:
    assert (
        github_app.manifest_form_action("https://github.com", None, "s-1")
        == "https://github.com/settings/apps/new?state=s-1"
    )
    assert (
        github_app.manifest_form_action("https://github.com", "acme", "s-1")
        == "https://github.com/organizations/acme/settings/apps/new?state=s-1"
    )
    assert (
        github_app.installation_url("https://github.com", "henchmen-x", "s 2")
        == "https://github.com/apps/henchmen-x/installations/new?state=s+2"
    )


@pytest.mark.parametrize("slug", ["henchmen-x", "a", "0abc"])
def test_valid_slugs(slug: str) -> None:
    assert github_app.is_valid_slug(slug)


@pytest.mark.parametrize("slug", ["", "-x", "Henchmen", "a/b", "a..b", "a b", "x" * 101, "henchmen-x\n"])
def test_invalid_slugs(slug: str) -> None:
    assert not github_app.is_valid_slug(slug)


@pytest.mark.parametrize("code", ["", "../../app", "a/b", "a b", "c" * 129, "code\n"])
def test_invalid_codes(code: str) -> None:
    assert not github_app.is_valid_code(code)


@pytest.mark.asyncio
async def test_convert_manifest() -> None:
    github = FakeGitHub()
    github.conversions["code-1"] = FakeGitHub.conversion(github.app_id, "henchmen-x", app_key_pair()[0].decode())
    async with github.async_client() as client:
        conversion = await github_app.convert_manifest(client, API, "code-1")
    assert conversion.app_id == github.app_id
    assert conversion.slug == "henchmen-x"
    assert conversion.webhook_secret == "whsec-fake"
    assert conversion.owner_login == "chris"
    assert "PRIVATE KEY" in conversion.pem
    assert "PRIVATE KEY" not in repr(conversion)
    assert "whsec-fake" not in repr(conversion)
    assert github.requests[0].method == "POST"
    assert str(github.requests[0].url) == f"{API}/app-manifests/code-1/conversions"
    assert "authorization" not in github.requests[0].headers


@pytest.mark.asyncio
async def test_convert_manifest_failures() -> None:
    github = FakeGitHub()
    async with github.async_client() as client:
        with pytest.raises(github_app.GitHubAppApiError, match="HTTP 404"):
            await github_app.convert_manifest(client, API, "unknown")
        with pytest.raises(github_app.GitHubAppApiError, match="malformed"):
            await github_app.convert_manifest(client, API, "../../app")
    assert len(github.requests) == 1


@pytest.mark.asyncio
async def test_conversion_error_text_never_carries_a_private_key() -> None:
    pem = app_key_pair()[0].decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": f"Invalid manifest {pem}", "pem": pem})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(github_app.GitHubAppApiError) as exc_info:
            await github_app.convert_manifest(client, API, "code-1")
    assert "HTTP 422" in str(exc_info.value)
    assert "PRIVATE KEY" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_unreachable_github_is_reported_without_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(github_app.GitHubAppApiError, match="ConnectError") as exc_info:
            await github_app.convert_manifest(client, API, "secret-code-1")
    assert "secret-code-1" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"id": 1, "slug": "henchmen-x", "pem": ""},
        {"id": True, "slug": "henchmen-x", "pem": "-----BEGIN PRIVATE KEY-----"},
        {"id": 1, "slug": "../evil", "pem": "-----BEGIN PRIVATE KEY-----"},
        {"id": "1x", "slug": "henchmen-x", "pem": "-----BEGIN PRIVATE KEY-----"},
        {"id": 1, "slug": "henchmen-x", "pem": ["-----BEGIN PRIVATE KEY-----"]},
        [],
    ],
)
async def test_incomplete_conversion_is_refused(body: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(github_app.GitHubAppApiError, match="incomplete"):
            await github_app.convert_manifest(client, API, "code-1")


@pytest.mark.asyncio
async def test_a_webhook_secret_with_a_line_break_is_refused() -> None:
    github = FakeGitHub()
    body = FakeGitHub.conversion(github.app_id, "henchmen-x", app_key_pair()[0].decode())
    body["webhook_secret"] = "whsec\nHENCHMEN_PROVIDER=gcp"
    github.conversions["code-1"] = body
    async with github.async_client() as client:
        with pytest.raises(github_app.GitHubAppApiError, match="unreadable"):
            await github_app.convert_manifest(client, API, "code-1")


@pytest.mark.asyncio
async def test_installation_lookups() -> None:
    github = FakeGitHub()
    github.installations["77"] = FakeGitHub.installation("77", "acme", permissions={"contents": "read"})
    app_jwt = build_app_jwt(github.app_id, app_key_pair()[0], now=time.time())
    async with github.async_client() as client:
        installation = await github_app.get_installation(client, API, app_jwt, "77")
        missing = await github_app.get_installation(client, API, app_jwt, "78")
        malformed = await github_app.get_installation(client, API, app_jwt, "../app")
        listed = await github_app.list_installations(client, API, app_jwt)
        slug = await github_app.get_app_slug(client, API, app_jwt)
    assert installation is not None
    assert installation.installation_id == "77"
    assert installation.account_login == "acme"
    assert installation.missing_write_permissions() == ["contents", "pull_requests"]
    assert missing is None
    assert malformed is None
    assert [item.installation_id for item in listed] == ["77"]
    assert slug == "henchmen-test"
    assert github_app.installation_settings_url("https://github.com", installation) == (
        "https://github.com/organizations/acme/settings/installations/77"
    )


@pytest.mark.asyncio
async def test_repository_listing_follows_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        count = 100 if page == 1 else 1
        repositories = [
            {"full_name": f"acme/repo-{page}-{index:03d}", "default_branch": "main"} for index in range(count)
        ]
        return httpx.Response(200, json={"repositories": repositories})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repositories = await github_app.list_installation_repositories(client, API, "ghs_x")
    assert len(repositories) == 101
    assert repositories[0].full_name == "acme/repo-1-000"


@pytest.mark.asyncio
async def test_bot_identity() -> None:
    github = FakeGitHub()
    github.users["henchmen-x[bot]"] = {"id": 123456, "login": "henchmen-x[bot]"}
    async with github.async_client() as client:
        assert await github_app.bot_identity(client, API, "henchmen-x") == (
            "henchmen-x[bot]",
            "123456+henchmen-x[bot]@users.noreply.github.com",
        )
        with pytest.raises(github_app.GitHubAppApiError):
            await github_app.bot_identity(client, API, "unknown-app")


def _conversion_handler(body: object) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(201, json=body))


def _ec_private_key_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pem",
    [
        "-----BEGIN PRIVATE KEY-----\nbm90IGEga2V5\n-----END PRIVATE KEY-----\n",
        "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 64 + "\n-----END RSA PRIVATE KEY-----\n",
        "garbage PRIVATE KEY garbage",
    ],
)
async def test_a_pem_that_does_not_parse_is_an_incomplete_conversion(pem: str) -> None:
    body = {"id": 1, "slug": "henchmen-x", "pem": pem, "owner": {"login": "chris"}}
    async with httpx.AsyncClient(transport=_conversion_handler(body)) as client:
        with pytest.raises(github_app.GitHubAppApiError, match="incomplete") as exc_info:
            await github_app.convert_manifest(client, API, "code-1")
    assert "PRIVATE KEY" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_a_non_rsa_private_key_is_an_incomplete_conversion() -> None:
    body = {"id": 1, "slug": "henchmen-x", "pem": _ec_private_key_pem()}
    async with httpx.AsyncClient(transport=_conversion_handler(body)) as client:
        with pytest.raises(github_app.GitHubAppApiError, match="incomplete"):
            await github_app.convert_manifest(client, API, "code-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "expected"),
    [
        ({"login": "chris"}, "chris"),
        ({"login": "Acme-Corp-2"}, "Acme-Corp-2"),
        ({"login": "-bad"}, ""),
        ({"login": "a" * 40}, ""),
        ({"login": "evil/../x"}, ""),
        ({"login": "chris\nHENCHMEN_X=1"}, ""),
        ({"login": 42}, ""),
        ("chris", ""),
        (None, ""),
    ],
)
async def test_owner_login_is_accepted_only_as_a_github_login(owner: object, expected: str) -> None:
    body = {"id": 1, "slug": "henchmen-x", "pem": app_key_pair()[0].decode(), "owner": owner}
    async with httpx.AsyncClient(transport=_conversion_handler(body)) as client:
        conversion = await github_app.convert_manifest(client, API, "code-1")
    assert conversion.owner_login == expected


def test_private_key_file_names_are_per_app() -> None:
    assert github_app.private_key_file_name("4242") == "github-app-4242.pem"
    for bad in ("", "12a", "../1", "1" * 21):
        with pytest.raises(ValueError):
            github_app.private_key_file_name(bad)


def test_unreferenced_key_cleanup_removes_only_unreferenced_app_keys(tmp_path: Path) -> None:
    import os

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    referenced = secrets / "github-app-2.pem"
    env_referenced = secrets / "github-app-3.pem"
    stale = [secrets / "github-app-1.pem", secrets / "github-app.pem"]
    others = [
        secrets / "github-app-1.pem.0a1b.tmp",
        secrets / "github-app-x.pem",
        secrets / "github-callback-states.json",
        secrets / "session-key",
    ]
    for path in [referenced, env_referenced, *stale, *others]:
        path.write_bytes(b"x")
    outside = tmp_path / "github-app-9.pem"
    outside.write_bytes(b"x")

    removed = github_app.remove_unreferenced_app_keys(secrets, [str(referenced), "", env_referenced])

    assert sorted(removed) == sorted(stale)
    assert referenced.exists()
    assert env_referenced.exists()
    assert not any(path.exists() for path in stale)
    assert all(path.exists() for path in others)
    assert outside.exists()

    link = secrets / "github-app-5.pem"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        return
    assert github_app.remove_unreferenced_app_keys(secrets, [referenced, env_referenced]) == []
    assert link.is_symlink()


def test_unreferenced_key_cleanup_tolerates_a_missing_directory(tmp_path: Path) -> None:
    assert github_app.remove_unreferenced_app_keys(tmp_path / "missing", []) == []


def test_startup_cleanup_keeps_a_relative_key_reference_resolved_against_the_config_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    secrets = data_dir / "secrets"
    secrets.mkdir(parents=True)
    referenced = secrets / "github-app-2.pem"
    stale = secrets / "github-app-1.pem"
    for path in (referenced, stale):
        path.write_bytes(b"x")
    config_file = data_dir / "henchmen.env"
    config_file.write_text("HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH=secrets/github-app-2.pem\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the working directory is not the data directory

    removed = github_app.remove_unused_app_keys_at_startup(config_file, secrets, "")

    assert removed == [stale]
    assert referenced.exists()


def test_startup_cleanup_keeps_a_relative_effective_key_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    referenced = secrets / "github-app-3.pem"
    referenced.write_bytes(b"x")
    monkeypatch.chdir(tmp_path.parent)
    assert (
        github_app.remove_unused_app_keys_at_startup(tmp_path / "henchmen.env", secrets, "secrets/github-app-3.pem")
        == []
    )
    assert referenced.exists()


@pytest.mark.parametrize(
    ("installation", "app_id", "slug", "expected"),
    [
        ({"app_id": "4242", "app_slug": "henchmen-test"}, "4242", "henchmen-test", True),
        ({"app_id": "4242", "app_slug": ""}, "4242", "henchmen-test", True),
        ({"app_id": "", "app_slug": "Henchmen-Test"}, "4242", "henchmen-test", True),
        ({"app_id": "4242", "app_slug": "henchmen-test"}, "4242", "", True),
        ({"app_id": "999", "app_slug": "henchmen-test"}, "4242", "henchmen-test", False),
        ({"app_id": "4242", "app_slug": "other-app"}, "4242", "henchmen-test", False),
        ({"app_id": "", "app_slug": ""}, "4242", "henchmen-test", False),
        ({"app_id": "4242", "app_slug": "henchmen-test"}, "", "", False),
    ],
)
def test_an_installation_belongs_to_an_app_only_when_github_says_so(
    installation: dict[str, str], app_id: str, slug: str, expected: bool
) -> None:
    item = github_app.Installation(installation_id="77", account_login="acme", **installation)
    assert item.belongs_to(app_id=app_id, slug=slug) is expected


@pytest.mark.asyncio
async def test_installation_carries_the_app_identity_github_returns() -> None:
    github = FakeGitHub()
    github.installations["77"] = FakeGitHub.installation("77", "acme")
    github.installations["78"] = {**FakeGitHub.installation("78", "acme"), "app_id": True}
    app_jwt = build_app_jwt(github.app_id, app_key_pair()[0], now=time.time())
    async with github.async_client() as client:
        installation = await github_app.get_installation(client, API, app_jwt, "77")
        odd = await github_app.get_installation(client, API, app_jwt, "78")
    assert installation is not None
    assert (installation.app_id, installation.app_slug) == ("4242", "henchmen-test")
    assert odd is not None
    assert odd.app_id == ""
