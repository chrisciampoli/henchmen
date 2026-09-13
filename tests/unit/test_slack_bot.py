"""Unit tests for the Slack Socket Mode bot in ``henchmen.dispatch.slack_bot``.

Covers the three defects the module shipped with:

* ``_sync_publish`` used ``asyncio.get_event_loop()``, which raises on a Bolt
  worker thread, so every @mention was silently dropped.
* credentials were read from raw ``os.environ`` instead of ``Settings``.
* the bot was the container's main process, so the HTTP intake never ran; it
  now starts from the Dispatch FastAPI lifespan via ``start_socket_mode``.
"""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from henchmen.dispatch import slack_bot
from henchmen.models.task import HenchmenTask, TaskContext, TaskSource


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str):
    from henchmen.config.settings import get_settings

    monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
    monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


def _task() -> HenchmenTask:
    return HenchmenTask(
        source=TaskSource.SLACK,
        source_id="C1/1.1",
        title="Fix the thing",
        description="Fix the thing",
        context=TaskContext(repo="acme/api"),
        created_by="U1",
    )


# ---------------------------------------------------------------------------
# _sync_publish
# ---------------------------------------------------------------------------


class TestSyncPublish:
    def test_publishes_from_a_worker_thread(self, monkeypatch):
        """Bolt listeners run on threads with no event loop; this must still work."""
        settings = _settings(monkeypatch)
        broker = MagicMock()
        broker.publish = AsyncMock(return_value="msg-1")

        results: dict[str, object] = {}

        def run() -> None:
            try:
                results["value"] = slack_bot._sync_publish(_task(), settings, broker=broker)
            except BaseException as exc:  # pragma: no cover - failure path
                results["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()

        assert "error" not in results, results.get("error")
        assert results["value"] == "msg-1"
        broker.publish.assert_awaited_once()
        assert broker.publish.await_args[0][0] == settings.pubsub_topic_task_intake

    def test_builds_a_broker_when_none_is_supplied(self, monkeypatch):
        settings = _settings(monkeypatch)
        broker = MagicMock()
        broker.publish = AsyncMock(return_value="msg-2")

        with patch("henchmen.providers.registry.ProviderRegistry.get_message_broker", return_value=broker):
            assert slack_bot._sync_publish(_task(), settings) == "msg-2"


# ---------------------------------------------------------------------------
# create_slack_app
# ---------------------------------------------------------------------------


class TestCreateSlackApp:
    def test_reads_credentials_from_settings_not_os_environ(self, monkeypatch):
        """A HENCHMEN_-prefixed .env.local must configure the bot."""
        settings = _settings(
            monkeypatch,
            HENCHMEN_SLACK_BOT_TOKEN="xoxb-from-settings",
            HENCHMEN_SLACK_SIGNING_SECRET="signing-from-settings",
        )
        captured: dict[str, object] = {}

        class _FakeApp:
            def __init__(self, token: str = "", signing_secret: str = "") -> None:
                captured["token"] = token
                captured["signing_secret"] = signing_secret

            def event(self, _name: str):
                return lambda func: func

        with (
            patch.dict("sys.modules", {"slack_bolt": MagicMock(App=_FakeApp)}),
            patch("henchmen.providers.registry.ProviderRegistry.get_message_broker", return_value=MagicMock()),
        ):
            slack_bot.create_slack_app(settings)

        assert captured["token"] == "xoxb-from-settings"
        assert captured["signing_secret"] == "signing-from-settings"

    def test_bare_slack_env_names_still_work(self, monkeypatch):
        """Terraform injects SLACK_BOT_TOKEN; the alias must still resolve."""
        settings = _settings(monkeypatch, SLACK_BOT_TOKEN="xoxb-bare")
        assert settings.slack_bot_token == "xoxb-bare"


# ---------------------------------------------------------------------------
# start_socket_mode
# ---------------------------------------------------------------------------


class TestStartSocketMode:
    def test_returns_none_when_tokens_are_missing(self, monkeypatch, caplog):
        settings = _settings(monkeypatch, HENCHMEN_SLACK_BOT_TOKEN="", HENCHMEN_SLACK_APP_TOKEN="")
        with caplog.at_level("INFO"):
            assert slack_bot.start_socket_mode(settings) is None
        assert any("Socket Mode disabled" in record.message for record in caplog.records)

    def test_returns_none_when_only_the_app_token_is_set(self, monkeypatch):
        settings = _settings(monkeypatch, HENCHMEN_SLACK_APP_TOKEN="xapp-1", HENCHMEN_SLACK_BOT_TOKEN="")
        assert slack_bot.start_socket_mode(settings) is None

    def test_connects_without_blocking(self, monkeypatch):
        settings = _settings(
            monkeypatch,
            HENCHMEN_SLACK_APP_TOKEN="xapp-1",
            HENCHMEN_SLACK_BOT_TOKEN="xoxb-1",
            HENCHMEN_SLACK_NOTIFICATION_CHANNEL="",
        )
        handler = MagicMock()
        adapter = MagicMock()
        adapter.SocketModeHandler.return_value = handler

        with (
            patch.dict("sys.modules", {"slack_bolt.adapter.socket_mode": adapter}),
            patch.object(slack_bot, "create_slack_app", return_value=MagicMock()),
        ):
            result = slack_bot.start_socket_mode(settings)

        assert result is handler
        handler.connect.assert_called_once()
        handler.start.assert_not_called()

    def test_connection_failure_does_not_break_startup(self, monkeypatch):
        settings = _settings(
            monkeypatch,
            HENCHMEN_SLACK_APP_TOKEN="xapp-1",
            HENCHMEN_SLACK_BOT_TOKEN="xoxb-1",
        )
        with patch.object(slack_bot, "create_slack_app", side_effect=RuntimeError("boom")):
            assert slack_bot.start_socket_mode(settings) is None

    def test_joins_the_notification_channel(self, monkeypatch):
        settings = _settings(
            monkeypatch,
            HENCHMEN_SLACK_APP_TOKEN="xapp-1",
            HENCHMEN_SLACK_BOT_TOKEN="xoxb-1",
            HENCHMEN_SLACK_NOTIFICATION_CHANNEL="C0123CHANNEL",
        )
        slack_app = MagicMock()
        adapter = MagicMock()

        with (
            patch.dict("sys.modules", {"slack_bolt.adapter.socket_mode": adapter}),
            patch.object(slack_bot, "create_slack_app", return_value=slack_app),
        ):
            slack_bot.start_socket_mode(settings)

        slack_app.client.conversations_join.assert_called_once_with(channel="C0123CHANNEL")


class TestJoinNotificationChannel:
    def _error(self, code: str) -> Exception:
        exc = RuntimeError("slack error")
        exc.response = MagicMock(data={"ok": False, "error": code})  # type: ignore[attr-defined]
        return exc

    def test_already_in_channel_is_not_a_warning(self, caplog):
        app = MagicMock()
        app.client.conversations_join.side_effect = self._error("already_in_channel")
        with caplog.at_level("WARNING"):
            slack_bot._join_notification_channel(app, "C1")
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_private_channel_gets_an_actionable_message(self, caplog):
        app = MagicMock()
        app.client.conversations_join.side_effect = self._error("method_not_supported_for_channel_type")
        with caplog.at_level("WARNING"):
            slack_bot._join_notification_channel(app, "C1")
        assert any("invite the bot" in record.message for record in caplog.records)

    def test_no_channel_configured_is_a_no_op(self):
        app = MagicMock()
        slack_bot._join_notification_channel(app, "")
        app.client.conversations_join.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatch lifespan integration
# ---------------------------------------------------------------------------


class TestLifespanStartsSocketMode:
    def test_lifespan_starts_and_closes_the_handler(self, monkeypatch):
        from fastapi.testclient import TestClient

        _settings(monkeypatch, HENCHMEN_SLACK_APP_TOKEN="xapp-1", HENCHMEN_SLACK_BOT_TOKEN="xoxb-1")
        handler = MagicMock()

        import henchmen.dispatch.server as server

        with patch.object(slack_bot, "start_socket_mode", return_value=handler) as start:
            with TestClient(server.app) as client:
                assert client.get("/health").status_code == 200
            start.assert_called_once()
        handler.close.assert_called_once()
