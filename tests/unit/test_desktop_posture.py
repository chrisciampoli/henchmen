"""Desktop (data-directory) installs never take a development-only fail-open path (D-P1, amendment A1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from henchmen.config import paths
from henchmen.config.posture import fail_open_allowed, is_desktop_posture
from henchmen.config.settings import Environment, Settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"provider": "local", "environment": Environment.DEV}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    return tmp_path


def _pubsub_request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/pubsub/example",
            "headers": raw,
            "query_string": b"",
            "client": ("172.18.0.5", 40000),
        }
    )


class TestPredicate:
    def test_repo_checkout_dev_may_fail_open(self) -> None:
        assert paths.is_desktop_install() is False
        assert is_desktop_posture(_settings()) is False
        assert fail_open_allowed(_settings()) is True

    def test_data_dir_install_never_fails_open(self, desktop: Path) -> None:
        assert paths.is_desktop_install() is True
        assert is_desktop_posture(_settings()) is True
        assert fail_open_allowed(_settings()) is False

    def test_operative_launched_by_a_desktop_install_inherits_the_posture(self) -> None:
        settings = _settings(operative_task_token="t" * 64)
        assert is_desktop_posture(settings) is True
        assert fail_open_allowed(settings) is False

    def test_blank_operative_token_is_not_desktop(self) -> None:
        assert fail_open_allowed(_settings(operative_task_token="   ")) is True

    @pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PROD])
    def test_staging_and_prod_never_fail_open(self, environment: Environment) -> None:
        assert fail_open_allowed(_settings(environment=environment)) is False

    def test_environment_value_is_read_from_mocked_settings(self) -> None:
        settings = MagicMock()
        settings.environment.value = "dev"
        settings.operative_task_token = ""
        assert fail_open_allowed(settings) is True


class TestPubsubPush:
    @pytest.mark.asyncio
    async def test_desktop_dev_rejects_an_unauthenticated_push(self, desktop: Path) -> None:
        from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc

        with pytest.raises(HTTPException) as exc:
            await verify_pubsub_oidc(_pubsub_request(), _settings())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_repo_checkout_dev_still_allows_it(self) -> None:
        from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc

        assert await verify_pubsub_oidc(_pubsub_request(), _settings()) is None


class TestDispatchIntake:
    def test_desktop_task_api_without_a_token_fails_closed(
        self, desktop: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from henchmen.config.settings import get_settings
        from henchmen.dispatch.server import app

        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "acme/api")
        get_settings.cache_clear()
        with TestClient(app) as client:
            assert client.post("/api/v1/tasks", json={"title": "T"}).status_code == 401

    def test_desktop_webhooks_require_a_signing_secret(self, desktop: Path) -> None:
        from henchmen.dispatch.server import _require_signing_secret

        with pytest.raises(HTTPException) as exc:
            _require_signing_secret(
                Settings(_env_file=None, environment=Environment.DEV),
                "",
                integration="github",  # type: ignore[call-arg]
            )
        assert exc.value.status_code == 401


class TestForge:
    @pytest.mark.asyncio
    async def test_desktop_ci_without_a_github_token_fails_closed(self, desktop: Path) -> None:
        from henchmen.forge.server import ForgeCIError, _run_ci_for_pr

        with (
            patch("henchmen.forge.server.get_settings", return_value=_settings(github_token="")),
            patch("henchmen.forge.server._publish_ci_failure", new_callable=AsyncMock),
            pytest.raises(ForgeCIError) as exc,
        ):
            await _run_ci_for_pr("https://github.com/acme/repo/pull/7", "task-1", "req-1")
        assert "missing-github-token" in str(exc.value)
        assert exc.value.retriable is False


class TestOperativeStore:
    def test_operative_with_a_task_token_refuses_to_run_without_a_store(self) -> None:
        from henchmen.operative.bootstrap import _get_document_store

        registry = MagicMock()
        registry.get_document_store.side_effect = RuntimeError("no store")
        with pytest.raises(RuntimeError, match="Document store unavailable"):
            _get_document_store(registry, _settings(operative_task_token="t" * 64))

    def test_data_dir_process_refuses_too(self, desktop: Path) -> None:
        from henchmen.operative.bootstrap import _get_document_store

        registry = MagicMock()
        registry.get_document_store.side_effect = RuntimeError("no store")
        with pytest.raises(RuntimeError, match="Document store unavailable"):
            _get_document_store(registry, _settings())


class TestMetrics:
    def test_desktop_metrics_router_without_a_token_is_closed(self, desktop: Path) -> None:
        from henchmen.observability.api import create_metrics_router

        tracker = MagicMock()
        tracker.get_recent_tasks = AsyncMock(return_value=[])
        app = FastAPI()
        app.include_router(create_metrics_router(tracker, settings=_settings(metrics_auth_token="")))
        assert TestClient(app).get("/metrics/summary").status_code == 401

    @pytest.mark.asyncio
    async def test_require_metrics_auth_follows_the_posture(self, desktop: Path) -> None:
        from henchmen.observability.api import require_metrics_auth

        with (
            patch("henchmen.config.settings.get_settings", return_value=_settings(metrics_auth_token="")),
            pytest.raises(HTTPException) as exc,
        ):
            await require_metrics_auth(authorization="")
        assert exc.value.status_code == 401


class TestFinalReviewPostureProofs:
    """C2/C4/D1/D2: the shared predicates and parsers change no decision."""

    @pytest.mark.asyncio
    async def test_forge_off_a_desktop_install_in_dev_without_a_token_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D2: the repository-checkout fail-open still runs CI unauthenticated (it reaches the GitHub lookup)."""
        from henchmen.forge.server import ForgeCIError, _run_ci_for_pr

        monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
        client = MagicMock()
        client.get_repo.side_effect = RuntimeError("404 Not Found")
        with (
            patch("henchmen.forge.server.get_settings", return_value=_settings(github_token="")),
            patch("henchmen.forge.server._publish_ci_failure", new_callable=AsyncMock) as publish,
            patch("github.Github", return_value=client) as github_cls,
            pytest.raises(ForgeCIError) as exc,
        ):
            await _run_ci_for_pr("https://github.com/acme/repo/pull/7", "task-1", "req-1")
        assert "github-api-error" in str(exc.value)
        github_cls.assert_called_once_with()  # unauthenticated client, not a refusal
        assert publish.await_args.kwargs["reason"] == "github-api-error"

    @pytest.mark.parametrize(
        ("environment", "desktop_install", "open_without_token"),
        [(Environment.DEV, False, True), (Environment.DEV, True, False), (Environment.STAGING, False, False)],
    )
    def test_metrics_auth_decision_is_unchanged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        environment: Environment,
        desktop_install: bool,
        open_without_token: bool,
    ) -> None:
        from henchmen.observability.api import create_metrics_router

        if desktop_install:
            monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
        else:
            monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
        tracker = MagicMock()
        tracker.get_recent_tasks = AsyncMock(return_value=[])
        app = FastAPI()
        settings = _settings(environment=environment, metrics_auth_token="", gcp_project_id="p")
        app.include_router(create_metrics_router(tracker, settings=settings))
        assert TestClient(app).get("/metrics/summary").status_code == (200 if open_without_token else 401)

    @pytest.mark.parametrize(
        ("header", "status"),
        [
            ("Bearer metrics-secret", 200),
            ("Bearer  metrics-secret ", 200),
            ("bearer metrics-secret", 200),
            ("Bearer wrong", 401),
            ("Basic metrics-secret", 401),
            ("metrics-secret", 401),
            ("", 401),
        ],
    )
    def test_metrics_bearer_goes_through_the_shared_parser(self, header: str, status: int) -> None:
        """C4: split_bearer + tokens_match; the RFC 7235 case-insensitive scheme is the one widening."""
        from henchmen.observability.api import create_metrics_router

        tracker = MagicMock()
        tracker.get_recent_tasks = AsyncMock(return_value=[])
        app = FastAPI()
        app.include_router(create_metrics_router(tracker, settings=_settings(metrics_auth_token="metrics-secret")))
        headers = {"Authorization": header} if header else {}
        assert TestClient(app).get("/metrics/summary", headers=headers).status_code == status

    @pytest.mark.parametrize(
        ("header", "status"),
        [("Bearer api-secret", 200), ("bearer api-secret", 200), ("Bearer api-secre", 401), ("Token api-secret", 401)],
    )
    def test_dispatch_api_bearer_goes_through_the_shared_parser(
        self, monkeypatch: pytest.MonkeyPatch, header: str, status: int
    ) -> None:
        from henchmen.config.settings import get_settings
        from henchmen.dispatch.server import app

        monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
        monkeypatch.setenv("HENCHMEN_DISPATCH_API_TOKEN", "api-secret")
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "acme/api")
        get_settings.cache_clear()
        with TestClient(app) as client:
            assert (
                client.post("/api/v1/tasks", json={"title": "T"}, headers={"Authorization": header}).status_code
                == status
            )

    def test_dispatch_lifespan_warns_on_a_desktop_install_without_a_token(
        self, desktop: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D1/D2: the warning names the desktop install, not "dev"."""
        import logging

        from henchmen.config.settings import get_settings
        from henchmen.dispatch.server import app

        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
        get_settings.cache_clear()
        with caplog.at_level(logging.WARNING, logger="henchmen.dispatch.server"), TestClient(app):
            pass
        lines = [r.getMessage() for r in caplog.records if "HENCHMEN_DISPATCH_API_TOKEN is empty" in r.getMessage()]
        assert lines == [
            "[dispatch] Configuration problem: HENCHMEN_DISPATCH_API_TOKEN is empty, so POST /api/v1/tasks "
            "returns 401 on a desktop install"
        ]

    def test_dispatch_lifespan_does_not_warn_when_fail_open_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from henchmen.config.settings import get_settings
        from henchmen.dispatch.server import app

        monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
        get_settings.cache_clear()
        with caplog.at_level(logging.WARNING, logger="henchmen.dispatch.server"), TestClient(app):
            pass
        assert not any("returns 401" in r.getMessage() for r in caplog.records)
