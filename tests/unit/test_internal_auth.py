"""Internal push token and task-scoped operative tokens on a desktop install (D-P3, D-P4)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from henchmen.config.internal_auth import (
    INTERNAL_PUSH_TOKEN_FILE_NAME,
    OPERATIVE_TASK_KEY_FILE_NAME,
    clear_cache,
    desktop_internal_auth,
    load_internal_auth,
)
from henchmen.config.settings import Settings
from henchmen.utils.redaction import redact


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"provider": "local"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _request(headers: dict[str, str] | None = None, body: bytes = b"") -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/pubsub/example",
        "headers": raw,
        "query_string": b"",
        "client": ("172.18.0.5", 40000),
    }
    return Request(scope, receive)


@pytest.fixture(autouse=True)
def _clear_internal_auth_cache():
    """Ruling P7/4: the load cache is keyed by resolved directory and must not leak between tests."""
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    return load_internal_auth(tmp_path / "secrets")


def test_secrets_are_created_owner_only_and_reused(tmp_path: Path) -> None:
    first = load_internal_auth(tmp_path / "secrets")
    push_file = tmp_path / "secrets" / INTERNAL_PUSH_TOKEN_FILE_NAME
    key_file = tmp_path / "secrets" / OPERATIVE_TASK_KEY_FILE_NAME
    assert len(push_file.read_bytes()) >= 32
    assert len(key_file.read_bytes()) >= 32
    assert push_file.read_bytes() != key_file.read_bytes()
    assert len(first.push_token) >= 43
    if sys.platform != "win32":
        assert oct(os.stat(push_file).st_mode & 0o777) == "0o600"
        assert oct(os.stat(key_file).st_mode & 0o777) == "0o600"


def test_push_token_verification_is_exact(tmp_path: Path) -> None:
    auth = load_internal_auth(tmp_path / "secrets")
    assert auth.verify_push_token(auth.push_token)
    for wrong in (None, "", auth.push_token[:-1], auth.push_token + "x", "é" * 43):
        assert not auth.verify_push_token(wrong)


def test_task_tokens_are_bound_to_one_task_and_one_install(tmp_path: Path) -> None:
    auth = load_internal_auth(tmp_path / "a")
    other_install = load_internal_auth(tmp_path / "b")
    token = auth.task_token("task-1")
    assert len(token) == 64
    assert auth.verify_task_token("task-1", token)
    assert not auth.verify_task_token("task-2", token)
    assert not auth.verify_task_token("", token)
    assert not auth.verify_task_token("task-1", None)
    assert not other_install.verify_task_token("task-1", token)
    assert not auth.verify_push_token(token)
    assert auth.push_token not in repr(auth)


def test_desktop_internal_auth_needs_a_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
    assert desktop_internal_auth() is None
    monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
    assert desktop_internal_auth() == load_internal_auth(tmp_path / "secrets")


class TestPubsubOnDesktop:
    @pytest.mark.asyncio
    async def test_push_token_is_accepted(self, desktop) -> None:
        from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc

        request = _request({"Authorization": f"Bearer {desktop.push_token}"})
        await verify_pubsub_oidc(request, _settings())
        assert request.state.pubsub_internal_caller is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic abc", "Bearer "])
    async def test_missing_or_wrong_token_is_rejected(self, desktop, header: str | None) -> None:
        from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc

        headers = {"Authorization": header} if header is not None else {}
        with pytest.raises(HTTPException) as exc:
            await verify_pubsub_oidc(_request(headers), _settings())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_a_task_token_is_not_a_push_credential(self, desktop) -> None:
        from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc

        request = _request({"Authorization": f"Bearer {desktop.task_token('task-1')}"})
        with pytest.raises(HTTPException) as exc:
            await verify_pubsub_oidc(request, _settings())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_non_local_broker_uses_oidc_and_fails_closed(self, desktop) -> None:
        from henchmen.dispatch.pubsub_auth import local_push_auth, verify_pubsub_oidc

        settings = _settings(message_broker_provider="gcp")
        assert local_push_auth(settings) is None
        with pytest.raises(HTTPException) as exc:
            await verify_pubsub_oidc(_request({"Authorization": f"Bearer {desktop.push_token}"}), settings)
        assert exc.value.status_code == 401


class TestMaintenanceGuardIgnoresBrokerProvider:
    """Ruling P4: require_internal_caller guards every desktop install, whatever the broker provider.

    Unlike ``verify_pubsub_oidc``, it is based on ``desktop_internal_auth()`` directly rather than
    ``local_push_auth(...)``, so it does not consult the message broker setting at all.
    """

    @pytest.mark.asyncio
    async def test_non_local_broker_still_requires_the_token(self, desktop) -> None:
        from henchmen.dispatch.pubsub_auth import local_push_auth, require_internal_caller

        settings = _settings(message_broker_provider="gcp")
        assert local_push_auth(settings) is None  # confirms the broker really is non-local here
        with pytest.raises(HTTPException) as exc:
            await require_internal_caller(_request())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_non_local_broker_still_accepts_the_push_token(self, desktop) -> None:
        from henchmen.dispatch.pubsub_auth import require_internal_caller

        await require_internal_caller(_request({"Authorization": f"Bearer {desktop.push_token}"}))

    @pytest.mark.asyncio
    async def test_without_a_data_dir_it_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from henchmen.dispatch.pubsub_auth import require_internal_caller

        monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
        await require_internal_caller(_request())


def test_bearer_tokens_are_redacted_from_logs() -> None:
    line = "POST failed with Authorization: Bearer " + "a" * 43
    assert "a" * 43 not in redact(line)
