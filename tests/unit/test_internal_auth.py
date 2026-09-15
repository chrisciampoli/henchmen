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
    async def test_gcp_broker_still_accepts_only_the_push_token(self, desktop) -> None:
        """Controller ruling: on any desktop install, verify_pubsub_oidc accepts ONLY the internal
        push token and never falls back to OIDC or fail-open, whatever the broker resolves to.

        ``henchmen serve`` always wires the shared in-memory broker as the actual transport for a
        desktop install's ``/pubsub/*`` pushes, so a ``henchmen.env`` naming a cloud broker must not
        reopen the OIDC/fail-open path.
        """
        from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc

        settings = _settings(message_broker_provider="gcp")

        request = _request({"Authorization": f"Bearer {desktop.push_token}"})
        await verify_pubsub_oidc(request, settings)
        assert request.state.pubsub_internal_caller is True

        with pytest.raises(HTTPException) as exc:
            await verify_pubsub_oidc(_request(), settings)
        assert exc.value.status_code == 401

        # An OIDC-looking bearer (three dot-separated base64url segments, the shape of a real
        # Google-signed ID token) must still be refused outright, not handed to the OIDC verifier.
        oidc_looking = "eyJhbGciOiJSUzI1NiJ9." + "e" * 40 + "." + "s" * 40
        with pytest.raises(HTTPException) as exc:
            await verify_pubsub_oidc(_request({"Authorization": f"Bearer {oidc_looking}"}), settings)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_secrets_directory_failure_is_a_503_not_a_crash(
        self, desktop, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ruling: an OSError loading the internal auth inside a request fails closed as 503, and
        the log line never carries secret content."""
        import henchmen.dispatch.pubsub_auth as pubsub_auth_module

        def _boom() -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(pubsub_auth_module, "desktop_internal_auth", _boom)
        request = _request({"Authorization": f"Bearer {desktop.push_token}"})
        with pytest.raises(HTTPException) as exc:
            await pubsub_auth_module.verify_pubsub_oidc(request, _settings())
        assert exc.value.status_code == 503
        assert desktop.push_token not in str(exc.value.detail)


class TestMaintenanceGuardIgnoresBrokerProvider:
    """Ruling P4: require_internal_caller guards every desktop install, whatever the broker provider.

    Unlike ``verify_pubsub_oidc``, it is based on ``desktop_internal_auth()`` directly rather than
    ``local_push_auth(...)``, so it does not consult the message broker setting at all.
    """

    @pytest.mark.asyncio
    async def test_non_local_broker_still_requires_the_token(self, desktop, monkeypatch: pytest.MonkeyPatch) -> None:
        from henchmen.dispatch.pubsub_auth import require_internal_caller

        monkeypatch.setenv("HENCHMEN_MESSAGE_BROKER_PROVIDER", "gcp")
        with pytest.raises(HTTPException) as exc:
            await require_internal_caller(_request())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_non_local_broker_still_accepts_the_push_token(
        self, desktop, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from henchmen.dispatch.pubsub_auth import require_internal_caller

        monkeypatch.setenv("HENCHMEN_MESSAGE_BROKER_PROVIDER", "gcp")
        await require_internal_caller(_request({"Authorization": f"Bearer {desktop.push_token}"}))

    @pytest.mark.asyncio
    async def test_without_a_data_dir_it_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from henchmen.dispatch.pubsub_auth import require_internal_caller

        monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
        await require_internal_caller(_request())

    @pytest.mark.asyncio
    async def test_secrets_directory_failure_is_a_503_not_a_crash(
        self, desktop, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import henchmen.dispatch.pubsub_auth as pubsub_auth_module

        def _boom() -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(pubsub_auth_module, "desktop_internal_auth", _boom)
        with pytest.raises(HTTPException) as exc:
            await pubsub_auth_module.require_internal_caller(
                _request({"Authorization": f"Bearer {desktop.push_token}"})
            )
        assert exc.value.status_code == 503
        assert desktop.push_token not in str(exc.value.detail)


def _report_body(task_id: str) -> bytes:
    import base64
    import json

    data = base64.b64encode(json.dumps({"task_id": task_id}).encode()).decode()
    return json.dumps({"message": {"data": data, "messageId": "m-1"}}).encode()


class TestOperativeReportAuth:
    @pytest.mark.asyncio
    async def test_task_token_for_the_reported_task_is_accepted(self, desktop) -> None:
        from henchmen.dispatch.pubsub_auth import verify_operative_report

        request = _request({"Authorization": f"Bearer {desktop.task_token('task-1')}"}, _report_body("task-1"))
        await verify_operative_report(request, _settings())
        assert request.state.operative_task_id == "task-1"
        assert (await request.json())["message"]["messageId"] == "m-1", "the body stays readable for the handler"

    @pytest.mark.asyncio
    async def test_task_token_for_another_task_is_rejected(self, desktop) -> None:
        from henchmen.dispatch.pubsub_auth import verify_operative_report

        request = _request({"Authorization": f"Bearer {desktop.task_token('task-1')}"}, _report_body("task-2"))
        with pytest.raises(HTTPException) as exc:
            await verify_operative_report(request, _settings())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [b"", b"not json", b'{"message": {"data": "!!!"}}'])
    async def test_undecodable_report_with_a_task_token_is_rejected(self, desktop, body: bytes) -> None:
        from henchmen.dispatch.pubsub_auth import verify_operative_report

        request = _request({"Authorization": f"Bearer {desktop.task_token('task-1')}"}, body)
        with pytest.raises(HTTPException) as exc:
            await verify_operative_report(request, _settings())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_push_token_is_accepted_and_no_token_is_rejected(self, desktop) -> None:
        from henchmen.dispatch.pubsub_auth import verify_operative_report

        await verify_operative_report(
            _request({"Authorization": f"Bearer {desktop.push_token}"}, _report_body("task-1")), _settings()
        )
        with pytest.raises(HTTPException):
            await verify_operative_report(_request({}, _report_body("task-1")), _settings())


def test_bearer_tokens_are_redacted_from_logs() -> None:
    line = "POST failed with Authorization: Bearer " + "a" * 43
    assert "a" * 43 not in redact(line)


@pytest.mark.parametrize(
    "separator",
    [" ", "\t", "   "],
    ids=["single-space", "tab", "several-spaces"],
)
@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_bearer_redaction_is_case_and_whitespace_insensitive(scheme: str, separator: str) -> None:
    token = "b" * 43
    line = f"Authorization:{separator}{scheme}{separator}{token}"
    redacted = redact(line)
    assert token not in redacted
    assert scheme in redacted
