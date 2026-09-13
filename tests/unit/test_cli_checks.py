"""Unit tests for the live credential/service checks shared by ``henchmen doctor`` and ``henchmen init``.

Every check must return a ``CheckResult`` and never raise, regardless of
network errors, auth failures or a missing SDK.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from henchmen.cli import checks
from henchmen.cli.checks import (
    CheckResult,
    CheckStatus,
    SlackChannel,
    check_anthropic_key,
    check_github_repo,
    check_github_token,
    check_jira,
    check_ollama,
    check_openai_key,
    check_slack_app_token,
    check_slack_bot_token,
    check_vertex,
    join_slack_channel,
    list_anthropic_models,
    list_ollama_models,
    list_openai_models,
    list_slack_channels,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SlackApiError(Exception):
    """Stand-in with the same shape as slack_sdk.errors.SlackApiError."""

    def __init__(self, error: str, needed: str | None = None) -> None:
        super().__init__(error)
        self.response = {"ok": False, "error": error}
        if needed:
            self.response["needed"] = needed


def _slack_client(**methods: Any) -> Any:
    return SimpleNamespace(**methods)


# ---------------------------------------------------------------------------
# CheckResult
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_flags(self):
        assert CheckResult("n", CheckStatus.OK, "m").is_ok
        assert CheckResult("n", CheckStatus.FAIL, "m").is_failure
        warn = CheckResult("n", CheckStatus.WARN, "m")
        assert not warn.is_ok and not warn.is_failure


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class TestAnthropic:
    def test_empty_key_fails_without_network(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(checks, "_anthropic_client", lambda *a, **k: pytest.fail("must not build client"))
        result = check_anthropic_key("")
        assert result.status == CheckStatus.FAIL
        assert "HENCHMEN_ANTHROPIC_API_KEY" in (result.hint or "")

    def test_valid_key_lists_models(self, monkeypatch: pytest.MonkeyPatch):
        page = SimpleNamespace(data=[SimpleNamespace(id="claude-b"), SimpleNamespace(id="claude-a")])
        client = SimpleNamespace(models=SimpleNamespace(list=lambda **kw: page))
        monkeypatch.setattr(checks, "_anthropic_client", lambda api_key, timeout: client)

        result = check_anthropic_key("sk-ant-x")
        assert result.status == CheckStatus.OK
        assert "2 models" in result.message
        assert list_anthropic_models("sk-ant-x") == ["claude-a", "claude-b"]

    def test_auth_error_fails(self, monkeypatch: pytest.MonkeyPatch):
        def boom(**kw: Any) -> Any:
            raise RuntimeError("401 invalid x-api-key")

        client = SimpleNamespace(models=SimpleNamespace(list=boom))
        monkeypatch.setattr(checks, "_anthropic_client", lambda api_key, timeout: client)
        result = check_anthropic_key("sk-ant-bad")
        assert result.status == CheckStatus.FAIL
        assert "invalid x-api-key" in result.message
        assert list_anthropic_models("sk-ant-bad") == []

    def test_missing_sdk_warns_with_install_hint(self, monkeypatch: pytest.MonkeyPatch):
        def missing(api_key: str, timeout: float) -> Any:
            raise ImportError("No module named 'anthropic'")

        monkeypatch.setattr(checks, "_anthropic_client", missing)
        result = check_anthropic_key("sk-ant-x")
        assert result.status == CheckStatus.WARN
        assert ".[anthropic]" in (result.hint or "")


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


class TestOpenAI:
    def test_empty_key_fails(self):
        assert check_openai_key("").status == CheckStatus.FAIL

    def test_valid_key(self, monkeypatch: pytest.MonkeyPatch):
        page = SimpleNamespace(data=[SimpleNamespace(id="gpt-4.1"), SimpleNamespace(id="gpt-4.1-mini")])
        client = SimpleNamespace(models=SimpleNamespace(list=lambda **kw: page))
        monkeypatch.setattr(checks, "_openai_client", lambda api_key, timeout: client)
        assert check_openai_key("sk-x").status == CheckStatus.OK
        assert list_openai_models("sk-x") == ["gpt-4.1", "gpt-4.1-mini"]

    def test_missing_sdk(self, monkeypatch: pytest.MonkeyPatch):
        def missing(api_key: str, timeout: float) -> Any:
            raise ImportError("openai")

        monkeypatch.setattr(checks, "_openai_client", missing)
        result = check_openai_key("sk-x")
        assert result.status == CheckStatus.WARN
        assert ".[openai]" in (result.hint or "")


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


def _ollama_transport(status: int = 200, payload: dict[str, Any] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(status, json=payload or {"models": []})

    return httpx.MockTransport(handler)


class TestOllama:
    def test_reachable_lists_models(self, monkeypatch: pytest.MonkeyPatch):
        transport = _ollama_transport(payload={"models": [{"name": "qwen2.5-coder:7b"}, {"name": "llama3.2:latest"}]})
        monkeypatch.setattr(checks, "_http_client", lambda timeout: httpx.Client(transport=transport))
        result = check_ollama("http://localhost:11434")
        assert result.status == CheckStatus.OK
        assert "2 models" in result.message
        assert list_ollama_models("http://localhost:11434") == ["llama3.2:latest", "qwen2.5-coder:7b"]

    def test_reachable_but_no_models_warns(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(checks, "_http_client", lambda timeout: httpx.Client(transport=_ollama_transport()))
        result = check_ollama("http://localhost:11434")
        assert result.status == CheckStatus.WARN
        assert "ollama pull" in (result.hint or "")

    def test_connection_refused_fails(self, monkeypatch: pytest.MonkeyPatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(
            checks, "_http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler))
        )
        result = check_ollama("http://localhost:11434")
        assert result.status == CheckStatus.FAIL
        assert "ollama serve" in (result.hint or "")
        assert list_ollama_models("http://localhost:11434") == []


# ---------------------------------------------------------------------------
# Vertex AI
# ---------------------------------------------------------------------------


class TestVertex:
    def test_missing_project_fails(self):
        assert check_vertex("", "us-central1").status == CheckStatus.FAIL

    def test_adc_present(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(checks, "_google_default_credentials", lambda: (object(), "my-proj"))
        result = check_vertex("my-proj", "us-central1")
        assert result.status == CheckStatus.OK

    def test_adc_project_mismatch_warns(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(checks, "_google_default_credentials", lambda: (object(), "other"))
        result = check_vertex("my-proj", "us-central1")
        assert result.status == CheckStatus.WARN
        assert "other" in result.message

    def test_no_adc_fails_with_login_hint(self, monkeypatch: pytest.MonkeyPatch):
        def boom() -> Any:
            raise RuntimeError("Could not automatically determine credentials")

        monkeypatch.setattr(checks, "_google_default_credentials", boom)
        result = check_vertex("my-proj", "us-central1")
        assert result.status == CheckStatus.FAIL
        assert "application-default login" in (result.hint or "")

    def test_missing_sdk_warns(self, monkeypatch: pytest.MonkeyPatch):
        def missing() -> Any:
            raise ImportError("google.auth")

        monkeypatch.setattr(checks, "_google_default_credentials", missing)
        result = check_vertex("my-proj", "us-central1")
        assert result.status == CheckStatus.WARN
        assert ".[gcp]" in (result.hint or "")


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


class TestGitHub:
    def test_empty_token_fails(self):
        result = check_github_token("")
        assert result.status == CheckStatus.FAIL
        assert "HENCHMEN_GITHUB_TOKEN" in (result.hint or "")

    def test_valid_token_reports_login(self, monkeypatch: pytest.MonkeyPatch):
        client = SimpleNamespace(get_user=lambda: SimpleNamespace(login="octocat"))
        monkeypatch.setattr(checks, "_github_client", lambda token, timeout: client)
        result = check_github_token("ghp_x")
        assert result.status == CheckStatus.OK
        assert "octocat" in result.message

    def test_bad_token_fails(self, monkeypatch: pytest.MonkeyPatch):
        def boom() -> Any:
            raise RuntimeError("401 Bad credentials")

        client = SimpleNamespace(get_user=boom)
        monkeypatch.setattr(checks, "_github_client", lambda token, timeout: client)
        result = check_github_token("ghp_bad")
        assert result.status == CheckStatus.FAIL
        assert "Bad credentials" in result.message

    def test_repo_requires_owner_slash_name(self):
        result = check_github_repo("ghp_x", "not-a-slug")
        assert result.status == CheckStatus.FAIL
        assert "owner/repo" in result.message

    def test_repo_with_push_access(self, monkeypatch: pytest.MonkeyPatch):
        repo = SimpleNamespace(full_name="acme/app", permissions=SimpleNamespace(push=True), default_branch="main")
        client = SimpleNamespace(get_repo=lambda name: repo)
        monkeypatch.setattr(checks, "_github_client", lambda token, timeout: client)
        result = check_github_repo("ghp_x", "acme/app")
        assert result.status == CheckStatus.OK
        assert "main" in result.message

    def test_repo_without_push_access_warns(self, monkeypatch: pytest.MonkeyPatch):
        repo = SimpleNamespace(full_name="acme/app", permissions=SimpleNamespace(push=False), default_branch="main")
        client = SimpleNamespace(get_repo=lambda name: repo)
        monkeypatch.setattr(checks, "_github_client", lambda token, timeout: client)
        result = check_github_repo("ghp_x", "acme/app")
        assert result.status == CheckStatus.WARN
        assert "push" in result.message.lower()

    def test_repo_not_found_fails(self, monkeypatch: pytest.MonkeyPatch):
        def boom(name: str) -> Any:
            raise RuntimeError("404 Not Found")

        client = SimpleNamespace(get_repo=boom)
        monkeypatch.setattr(checks, "_github_client", lambda token, timeout: client)
        assert check_github_repo("ghp_x", "acme/missing").status == CheckStatus.FAIL


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


class TestSlack:
    def test_empty_bot_token_fails(self):
        result = check_slack_bot_token("")
        assert result.status == CheckStatus.FAIL
        assert "xoxb" in (result.hint or "")

    def test_bot_token_ok_reports_user_and_team(self, monkeypatch: pytest.MonkeyPatch):
        client = _slack_client(auth_test=lambda: {"ok": True, "user": "henchmen", "team": "Acme", "user_id": "U1"})
        monkeypatch.setattr(checks, "_slack_client", lambda token, timeout: client)
        result = check_slack_bot_token("xoxb-1")
        assert result.status == CheckStatus.OK
        assert "henchmen" in result.message and "Acme" in result.message

    def test_bot_token_invalid_auth(self, monkeypatch: pytest.MonkeyPatch):
        def boom() -> Any:
            raise _SlackApiError("invalid_auth")

        monkeypatch.setattr(checks, "_slack_client", lambda token, timeout: _slack_client(auth_test=boom))
        result = check_slack_bot_token("xoxb-bad")
        assert result.status == CheckStatus.FAIL
        assert "invalid_auth" in result.message

    def test_app_token_ok(self, monkeypatch: pytest.MonkeyPatch):
        client = _slack_client(apps_connections_open=lambda app_token: {"ok": True, "url": "wss://x"})
        monkeypatch.setattr(checks, "_slack_client", lambda token, timeout: client)
        assert check_slack_app_token("xapp-1").status == CheckStatus.OK

    def test_app_token_wrong_prefix_fails_fast(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(checks, "_slack_client", lambda token, timeout: pytest.fail("no network"))
        result = check_slack_app_token("xoxb-1")
        assert result.status == CheckStatus.FAIL
        assert "xapp-" in result.message

    def test_list_channels_paginates_and_sorts(self, monkeypatch: pytest.MonkeyPatch):
        pages = {
            None: {
                "channels": [
                    {"id": "C2", "name": "zeta", "is_private": False, "is_member": False},
                    {"id": "C1", "name": "alpha", "is_private": True, "is_member": True},
                ],
                "response_metadata": {"next_cursor": "abc"},
            },
            "abc": {
                "channels": [{"id": "C3", "name": "beta", "is_private": False, "is_member": False}],
                "response_metadata": {"next_cursor": ""},
            },
        }
        calls: list[dict[str, Any]] = []

        def conversations_list(**kw: Any) -> dict[str, Any]:
            calls.append(kw)
            return pages[kw.get("cursor")]

        monkeypatch.setattr(
            checks, "_slack_client", lambda token, timeout: _slack_client(conversations_list=conversations_list)
        )
        channels = list_slack_channels("xoxb-1")
        assert [c.name for c in channels] == ["alpha", "beta", "zeta"]
        assert channels[0] == SlackChannel(id="C1", name="alpha", is_private=True, is_member=True)
        assert calls[0]["types"] == "public_channel,private_channel"
        assert calls[0]["exclude_archived"] is True

    def test_list_channels_missing_scope_raises_with_needed_scope(self, monkeypatch: pytest.MonkeyPatch):
        def boom(**kw: Any) -> Any:
            raise _SlackApiError("missing_scope", needed="channels:read")

        monkeypatch.setattr(checks, "_slack_client", lambda token, timeout: _slack_client(conversations_list=boom))
        with pytest.raises(checks.SlackScopeError) as excinfo:
            list_slack_channels("xoxb-1")
        assert "channels:read" in str(excinfo.value)

    def test_join_channel_ok(self, monkeypatch: pytest.MonkeyPatch):
        client = _slack_client(conversations_join=lambda channel: {"ok": True, "channel": {"name": "eng"}})
        monkeypatch.setattr(checks, "_slack_client", lambda token, timeout: client)
        result = join_slack_channel("xoxb-1", "C1")
        assert result.status == CheckStatus.OK
        assert "eng" in result.message

    def test_join_channel_already_member_is_ok(self, monkeypatch: pytest.MonkeyPatch):
        def boom(channel: str) -> Any:
            raise _SlackApiError("already_in_channel")

        monkeypatch.setattr(checks, "_slack_client", lambda token, timeout: _slack_client(conversations_join=boom))
        assert join_slack_channel("xoxb-1", "C1").status == CheckStatus.OK

    def test_join_private_channel_explains_invite(self, monkeypatch: pytest.MonkeyPatch):
        def boom(channel: str) -> Any:
            raise _SlackApiError("method_not_supported_for_channel_type")

        monkeypatch.setattr(checks, "_slack_client", lambda token, timeout: _slack_client(conversations_join=boom))
        result = join_slack_channel("xoxb-1", "C1")
        assert result.status == CheckStatus.WARN
        assert "/invite" in (result.hint or "")

    def test_join_missing_scope_reports_scope(self, monkeypatch: pytest.MonkeyPatch):
        def boom(channel: str) -> Any:
            raise _SlackApiError("missing_scope", needed="channels:join")

        monkeypatch.setattr(checks, "_slack_client", lambda token, timeout: _slack_client(conversations_join=boom))
        result = join_slack_channel("xoxb-1", "C1")
        assert result.status == CheckStatus.FAIL
        assert "channels:join" in result.message

    def test_missing_sdk_warns(self, monkeypatch: pytest.MonkeyPatch):
        def missing(token: str, timeout: float) -> Any:
            raise ImportError("slack_sdk")

        monkeypatch.setattr(checks, "_slack_client", missing)
        result = check_slack_bot_token("xoxb-1")
        assert result.status == CheckStatus.WARN
        assert ".[slack]" in (result.hint or "")
        assert list_slack_channels("xoxb-1") == []


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------


class TestJira:
    def test_missing_fields_fail(self):
        assert check_jira("", "a@b", "t").status == CheckStatus.FAIL
        assert check_jira("https://x.atlassian.net", "", "t").status == CheckStatus.FAIL
        assert check_jira("https://x.atlassian.net", "a@b", "").status == CheckStatus.FAIL

    def test_myself_ok(self, monkeypatch: pytest.MonkeyPatch):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/rest/api/3/myself"
            assert request.headers.get("authorization", "").startswith("Basic ")
            return httpx.Response(200, json={"displayName": "Jane Doe"})

        monkeypatch.setattr(
            checks, "_http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler))
        )
        result = check_jira("https://x.atlassian.net/", "a@b", "tok")
        assert result.status == CheckStatus.OK
        assert "Jane Doe" in result.message

    def test_unauthorized_fails(self, monkeypatch: pytest.MonkeyPatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={})

        monkeypatch.setattr(
            checks, "_http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler))
        )
        result = check_jira("https://x.atlassian.net", "a@b", "tok")
        assert result.status == CheckStatus.FAIL
        assert "401" in result.message

    def test_network_error_fails(self, monkeypatch: pytest.MonkeyPatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(
            checks, "_http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(handler))
        )
        assert check_jira("https://x.atlassian.net", "a@b", "tok").status == CheckStatus.FAIL
