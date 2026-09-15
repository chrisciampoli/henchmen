"""Unit tests for Dispatch: TaskNormalizer, handlers, and server routes."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from henchmen.dispatch.api_models import CreateTaskRequest
from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.models.task import HenchmenTask, TaskPriority, TaskSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings():
    """Build a real ``Settings`` instance with test-safe defaults.

    The handler tests in this module don't assert on the specific topic
    name, so the real Settings class (which applies the ``henchmen-dev-``
    prefix) works without any further overrides.
    """
    import os

    from henchmen.config.settings import get_settings

    os.environ.setdefault("HENCHMEN_GCP_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    return get_settings()


# ---------------------------------------------------------------------------
# TaskNormalizer.from_cli
# ---------------------------------------------------------------------------


class TestTaskNormalizerFromCli:
    def test_creates_correct_henchmen_task(self):
        n = TaskNormalizer()
        data = {
            "title": "Fix login bug",
            "description": "Users can't log in",
            "repo": "acme/backend",
            "branch": "main",
            "priority": "high",
            "created_by": "devuser",
        }
        task = n.from_cli(data)
        assert isinstance(task, HenchmenTask)
        assert task.source == TaskSource.CLI
        assert task.title == "Fix login bug"
        assert task.description == "Users can't log in"
        assert task.context.repo == "acme/backend"
        assert task.context.branch == "main"
        assert task.priority == TaskPriority.HIGH
        assert task.created_by == "devuser"

    def test_defaults_applied_when_optional_fields_missing(self):
        n = TaskNormalizer()
        task = n.from_cli({"title": "Minimal task"})
        assert task.source == TaskSource.CLI
        assert task.description == ""
        assert task.context.repo == ""
        assert task.context.branch is None
        assert task.priority == TaskPriority.NORMAL
        assert task.created_by == "cli"
        assert task.task_type is None

    def test_explicit_task_type_is_carried_through_intake(self):
        """CreateTaskRequest -> from_cli -> HenchmenTask keeps the requester's type, and it survives Pub/Sub JSON."""
        from henchmen.models.task import TaskType

        request = CreateTaskRequest(title="Add null check", repo="acme/api", task_type="bugfix")
        task = TaskNormalizer().from_cli(request.model_dump())
        assert task.task_type == TaskType.BUGFIX
        assert HenchmenTask.model_validate_json(task.model_dump_json()).task_type == TaskType.BUGFIX

    def test_unknown_task_type_is_rejected_by_the_api_model(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            CreateTaskRequest(title="T", task_type="chore")

    def test_source_id_is_uuid_when_not_provided(self):
        n = TaskNormalizer()
        task = n.from_cli({"title": "Auto ID"})
        assert task.source_id != ""
        assert len(task.source_id) == 36  # UUID4 format

    def test_explicit_source_id_is_preserved(self):
        n = TaskNormalizer()
        task = n.from_cli({"title": "T", "id": "MY-CUSTOM-ID"})
        assert task.source_id == "MY-CUSTOM-ID"

    def test_task_has_unique_id(self):
        n = TaskNormalizer()
        t1 = n.from_cli({"title": "T1"})
        t2 = n.from_cli({"title": "T2"})
        assert t1.id != t2.id


# ---------------------------------------------------------------------------
# TaskNormalizer.from_slack
# ---------------------------------------------------------------------------


class TestTaskNormalizerFromSlack:
    def _make_event(self, text="@henchmen fix auth", user="U123", channel="C456", ts="1700000.000001"):
        return {
            "event": {
                "type": "app_mention",
                "user": user,
                "channel": channel,
                "ts": ts,
                "text": text,
            }
        }

    def test_extracts_user_from_slack_event(self):
        n = TaskNormalizer()
        task = n.from_slack(self._make_event(user="U999"))
        assert task.created_by == "U999"

    def test_source_set_to_slack(self):
        n = TaskNormalizer()
        task = n.from_slack(self._make_event())
        assert task.source == TaskSource.SLACK

    def test_source_id_includes_channel_and_thread_ts(self):
        n = TaskNormalizer()
        task = n.from_slack(self._make_event(channel="C456", ts="1700000.000001"))
        assert "C456" in task.source_id
        assert "1700000.000001" in task.source_id

    def test_thread_messages_populated_from_text(self):
        n = TaskNormalizer()
        task = n.from_slack(self._make_event(text="@henchmen do stuff"))
        assert task.context.thread_messages is not None
        assert len(task.context.thread_messages) >= 1
        assert any("do stuff" in m for m in task.context.thread_messages)

    def test_additional_thread_messages_included(self):
        n = TaskNormalizer()
        payload = self._make_event(text="@henchmen fix it")
        payload["messages"] = [
            {"text": "Previous message 1"},
            {"text": "Previous message 2"},
        ]
        task = n.from_slack(payload)
        texts = task.context.thread_messages or []
        assert any("Previous message 1" in m for m in texts)
        assert any("Previous message 2" in m for m in texts)

    def test_title_strips_henchmen_mention(self):
        n = TaskNormalizer()
        task = n.from_slack(self._make_event(text="@henchmen fix auth bug"))
        assert "@henchmen" not in task.title
        assert "fix auth bug" in task.title


# ---------------------------------------------------------------------------
# TaskNormalizer.from_github
# ---------------------------------------------------------------------------


class TestTaskNormalizerFromGithub:
    def _issue_labeled_payload(self, label="henchmen"):
        return {
            "action": "labeled",
            "label": {"name": label},
            "issue": {
                "number": 42,
                "title": "Bug: auth fails",
                "body": "Users cannot login",
                "user": {"login": "alice"},
                "labels": [{"name": label}],
                "state": "open",
            },
            "repository": {
                "full_name": "acme/backend",
                "default_branch": "main",
            },
        }

    def _pr_comment_payload(self):
        return {
            "action": "created",
            "pull_request": {
                "number": 7,
                "title": "Add feature X",
                "body": "Feature description",
                "user": {"login": "bob"},
                "labels": [],
                "state": "open",
                "head": {"ref": "feature-x"},
            },
            "comment": {
                "body": "@henchmen fix this",
                "user": {"login": "reviewer"},
                "author_association": "COLLABORATOR",
            },
            "repository": {"full_name": "acme/backend"},
        }

    def test_issue_labeled_creates_task(self):
        n = TaskNormalizer()
        task = n.from_github(self._issue_labeled_payload())
        assert task.source == TaskSource.GITHUB
        assert task.title == "Bug: auth fails"
        assert task.context.repo == "acme/backend"
        assert task.context.issue_fields is not None
        assert task.context.issue_fields["number"] == "42"

    def test_issue_labeled_source_id_contains_issue_number(self):
        n = TaskNormalizer()
        task = n.from_github(self._issue_labeled_payload())
        assert "42" in task.source_id

    def test_pr_comment_creates_task(self):
        n = TaskNormalizer()
        task = n.from_github(self._pr_comment_payload())
        assert task.source == TaskSource.GITHUB
        assert task.context.branch == "feature-x"

    def test_pr_comment_source_id_contains_pr_number(self):
        n = TaskNormalizer()
        task = n.from_github(self._pr_comment_payload())
        assert "7" in task.source_id

    def test_pr_comment_created_by_is_commenter(self):
        n = TaskNormalizer()
        task = n.from_github(self._pr_comment_payload())
        assert task.created_by == "reviewer"

    def test_issue_labels_captured_in_issue_fields(self):
        n = TaskNormalizer()
        task = n.from_github(self._issue_labeled_payload())
        assert task.context.issue_fields is not None
        assert "henchmen" in task.context.issue_fields["labels"]  # comma-separated string


# ---------------------------------------------------------------------------
# TaskNormalizer.from_jira
# ---------------------------------------------------------------------------


class TestTaskNormalizerFromJira:
    def _jira_payload(self, transition_name="Ready for Henchmen", priority="Major"):
        return {
            "transition": {"transitionName": transition_name},
            "issue": {
                "key": "PROJ-123",
                "fields": {
                    "summary": "Implement OAuth login",
                    "description": "Use OAuth 2.0 for authentication",
                    "assignee": {"emailAddress": "dev@acme.com"},
                    "priority": {"name": priority},
                    "status": {"name": transition_name},
                    "labels": ["backend"],
                },
            },
        }

    def test_creates_task_from_jira_payload(self):
        n = TaskNormalizer()
        task = n.from_jira(self._jira_payload())
        assert task.source == TaskSource.JIRA
        assert task.title == "Implement OAuth login"
        assert task.source_id == "PROJ-123"

    def test_created_by_is_assignee_email(self):
        n = TaskNormalizer()
        task = n.from_jira(self._jira_payload())
        assert task.created_by == "dev@acme.com"

    def test_transition_name_stored_in_issue_fields(self):
        n = TaskNormalizer()
        task = n.from_jira(self._jira_payload())
        assert task.context.issue_fields is not None
        assert task.context.issue_fields["transition"] == "Ready for Henchmen"

    def test_priority_mapping_major_to_high(self):
        n = TaskNormalizer()
        task = n.from_jira(self._jira_payload(priority="Major"))
        assert task.priority == TaskPriority.HIGH

    def test_priority_mapping_blocker_to_critical(self):
        n = TaskNormalizer()
        task = n.from_jira(self._jira_payload(priority="Blocker"))
        assert task.priority == TaskPriority.CRITICAL

    def test_priority_mapping_trivial_to_low(self):
        n = TaskNormalizer()
        task = n.from_jira(self._jira_payload(priority="Trivial"))
        assert task.priority == TaskPriority.LOW


# ---------------------------------------------------------------------------
# CLI handler
# ---------------------------------------------------------------------------


class TestCliHandler:
    @pytest.mark.asyncio
    async def test_returns_correct_response_shape(self):
        from henchmen.dispatch.handlers.cli import handle_cli_request

        normalizer = TaskNormalizer()
        settings = _mock_settings()

        with patch.object(normalizer, "publish_task", new=AsyncMock(return_value="msg-001")):
            result = await handle_cli_request(
                CreateTaskRequest(title="Do something", repo="acme/api"),
                normalizer,
                settings,
                broker=AsyncMock(),
            )

        assert result["status"] == "dispatched"
        assert result["message_id"] == "msg-001"
        assert "task_id" in result

    @pytest.mark.asyncio
    async def test_task_id_is_uuid(self):
        from henchmen.dispatch.handlers.cli import handle_cli_request

        normalizer = TaskNormalizer()
        settings = _mock_settings()

        with patch.object(normalizer, "publish_task", new=AsyncMock(return_value="x")):
            result = await handle_cli_request(
                CreateTaskRequest(title="T", repo="acme/api"), normalizer, settings, broker=AsyncMock()
            )

        assert len(result["task_id"]) == 36


# ---------------------------------------------------------------------------
# Slack handler
# ---------------------------------------------------------------------------


class TestSlackHandler:
    @pytest.mark.asyncio
    async def test_processes_app_mention(self):
        from henchmen.dispatch.handlers.slack import handle_slack_event

        normalizer = TaskNormalizer()
        settings = _mock_settings()

        payload = {
            "event": {
                "type": "app_mention",
                "user": "U123",
                "channel": "C001",
                "ts": "1700.001",
                "text": "@henchmen fix the bug",
            }
        }

        with patch.object(normalizer, "publish_task", new=AsyncMock(return_value="msg-slack-1")):
            result = await handle_slack_event(payload, normalizer, settings, broker=AsyncMock())

        assert result["status"] == "dispatched"
        assert result["message_id"] == "msg-slack-1"

    @pytest.mark.asyncio
    async def test_ignores_non_mention_events(self):
        from henchmen.dispatch.handlers.slack import handle_slack_event

        normalizer = TaskNormalizer()
        settings = _mock_settings()

        payload = {
            "event": {
                "type": "message",
                "user": "U123",
                "text": "Just a regular message",
            }
        }

        result = await handle_slack_event(payload, normalizer, settings, broker=AsyncMock())
        assert result["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_processes_at_henchmen_text_mention(self):
        from henchmen.dispatch.handlers.slack import handle_slack_event

        normalizer = TaskNormalizer()
        settings = _mock_settings()

        payload = {
            "event": {
                "type": "message",
                "user": "U555",
                "channel": "C002",
                "ts": "1700.002",
                "text": "@henchmen please do this",
            }
        }

        with patch.object(normalizer, "publish_task", new=AsyncMock(return_value="msg-2")):
            result = await handle_slack_event(payload, normalizer, settings, broker=AsyncMock())

        assert result["status"] == "dispatched"


# ---------------------------------------------------------------------------
# GitHub handler
# ---------------------------------------------------------------------------


class TestGithubHandler:
    @pytest.mark.asyncio
    async def test_routes_issue_labeled_event(self):
        from henchmen.dispatch.handlers.github import handle_github_webhook

        normalizer = TaskNormalizer()
        settings = _mock_settings()

        payload = {
            "action": "labeled",
            "label": {"name": "henchmen"},
            "issue": {
                "number": 5,
                "title": "Bug report",
                "body": "Something is broken",
                "user": {"login": "alice"},
                "labels": [{"name": "henchmen"}],
                "state": "open",
            },
            "repository": {"full_name": "acme/api", "default_branch": "main"},
        }

        with patch.object(normalizer, "publish_task", new=AsyncMock(return_value="msg-gh-1")):
            result = await handle_github_webhook(payload, normalizer, settings, broker=AsyncMock())

        assert result["status"] == "dispatched"
        assert result["trigger"] == "issue_labeled"

    @pytest.mark.asyncio
    async def test_routes_pr_comment_event(self):
        from henchmen.dispatch.handlers.github import handle_github_webhook

        normalizer = TaskNormalizer()
        settings = _mock_settings()

        payload = {
            "action": "created",
            "pull_request": {
                "number": 3,
                "title": "PR title",
                "body": "PR body",
                "user": {"login": "dev"},
                "labels": [],
                "state": "open",
                "head": {"ref": "feature"},
            },
            "comment": {
                "body": "@henchmen fix this",
                "user": {"login": "reviewer"},
                "author_association": "MEMBER",
            },
            "repository": {"full_name": "acme/api"},
        }

        with patch.object(normalizer, "publish_task", new=AsyncMock(return_value="msg-gh-2")):
            result = await handle_github_webhook(payload, normalizer, settings, broker=AsyncMock())

        assert result["status"] == "dispatched"
        assert result["trigger"] == "pr_comment"

    @pytest.mark.asyncio
    async def test_ignores_unrelated_event(self):
        from henchmen.dispatch.handlers.github import handle_github_webhook

        normalizer = TaskNormalizer()
        settings = _mock_settings()

        payload = {
            "action": "opened",
            "issue": {
                "number": 99,
                "title": "No label",
                "body": "",
                "user": {"login": "x"},
                "labels": [],
                "state": "open",
            },
            "repository": {"full_name": "acme/api", "default_branch": "main"},
        }

        result = await handle_github_webhook(payload, normalizer, settings, broker=AsyncMock())
        assert result["status"] == "ignored"


# ---------------------------------------------------------------------------
# Dispatch server route registration
# ---------------------------------------------------------------------------


class TestDispatchServerRoutes:
    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        # `_isolate_settings` (conftest.py) already clears the lru_cache on
        # both sides of the test, so the only thing this fixture needs to do
        # is set the env vars this test class cares about.
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        yield

    @pytest.fixture
    def client(self):
        from henchmen.dispatch.server import app

        with TestClient(app) as c:
            yield c

    def test_health_route_registered(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_create_task_route_registered(self, client):
        """Route /api/v1/tasks must exist; missing 'title' returns 422."""
        response = client.post("/api/v1/tasks", json={})
        assert response.status_code == 422

    def test_slack_webhook_route_url_verification(self, client):
        response = client.post(
            "/webhooks/slack",
            json={"type": "url_verification", "challenge": "test-challenge"},
        )
        assert response.status_code == 200
        assert response.json()["challenge"] == "test-challenge"

    def test_github_webhook_route_registered(self, client):
        response = client.post("/webhooks/github", json={"action": "opened"})
        assert response.status_code != 404

    def test_jira_webhook_route_registered(self, client):
        response = client.post(
            "/webhooks/jira",
            json={"transition": {"transitionName": "Other"}, "issue": {"key": "X-1", "fields": {}}},
        )
        assert response.status_code != 404

    def test_task_planned_stub_route_is_gone(self, client):
        """Nothing publishes task-planned; Dispatch must not expose an ack-everything stub."""
        response = client.post("/pubsub/task-planned", json={"message": {"data": ""}})
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GitHub push webhook for embedding
# ---------------------------------------------------------------------------


class TestGitHubPushEmbed:
    def test_detects_push_to_default_branch(self):
        from henchmen.dispatch.handlers.github import _is_push_to_default_branch

        payload = {
            "ref": "refs/heads/main",
            "repository": {"default_branch": "main", "full_name": "org/repo"},
        }
        assert _is_push_to_default_branch(payload) is True

    def test_ignores_push_to_feature_branch(self):
        from henchmen.dispatch.handlers.github import _is_push_to_default_branch

        payload = {
            "ref": "refs/heads/feature/foo",
            "repository": {"default_branch": "main", "full_name": "org/repo"},
        }
        assert _is_push_to_default_branch(payload) is False

    def test_ignores_non_push_events(self):
        from henchmen.dispatch.handlers.github import _is_push_to_default_branch

        payload = {"action": "labeled", "label": {"name": "henchmen"}}
        assert _is_push_to_default_branch(payload) is False

    @pytest.mark.asyncio
    async def test_handle_push_embed_publishes_message(self):
        from henchmen.dispatch.handlers.github import handle_push_embed

        settings = MagicMock()
        settings.pubsub_topic_embed_request = "henchmen-dev-embed-request"

        payload = {
            "ref": "refs/heads/main",
            "after": "abc123",
            "repository": {"full_name": "org/repo", "default_branch": "main"},
        }

        mock_broker = AsyncMock()
        mock_broker.publish = AsyncMock(return_value="msg-embed-1")
        result = await handle_push_embed(payload, settings, broker=mock_broker)

        assert result["status"] == "embed_requested"
        mock_broker.publish.assert_awaited_once()
        # The body is the contract Mastermind's /pubsub/embed-request validates.
        from henchmen.dossier.embed_pipeline import EmbedRequest

        topic, body = mock_broker.publish.await_args.args
        assert topic == "henchmen-dev-embed-request"
        assert EmbedRequest.model_validate_json(body) == EmbedRequest(
            repo="org/repo", commit_sha="abc123", mode="incremental"
        )

    @pytest.mark.asyncio
    async def test_push_without_repository_is_ignored(self):
        from henchmen.dispatch.handlers.github import handle_push_embed

        broker = AsyncMock()
        result = await handle_push_embed({"ref": "refs/heads/main"}, MagicMock(), broker=broker)

        assert result["status"] == "ignored"
        broker.publish.assert_not_awaited()

    def test_dispatch_no_longer_hosts_the_embedding_pipeline(self):
        """Dispatch normalizes and publishes; cloning and indexing live in dossier.embed_pipeline."""
        import henchmen.dispatch.handlers.cli as cli_handlers

        for name in ("run_embedding_pipeline", "handle_embed_command", "_collect_all_files", "clone_repo"):
            assert not hasattr(cli_handlers, name), name


# ---------------------------------------------------------------------------
# Slack Bot — create_slack_app, event parsing, mention detection
# ---------------------------------------------------------------------------


class TestSlackBotCreateApp:
    """Test Slack bot app creation and event handler registration."""

    def test_create_slack_app_returns_app(self):
        from slack_bolt import App

        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_SIGNING_SECRET": "test-secret"}),
            patch.object(App, "__init__", return_value=None),
            patch.object(App, "event", return_value=lambda f: f),
        ):
            from henchmen.dispatch.slack_bot import create_slack_app

            app = create_slack_app()
        assert app is not None

    def test_create_slack_app_with_empty_tokens(self):
        """App can be created even with empty tokens when auth is mocked."""
        from slack_bolt import App

        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "", "SLACK_SIGNING_SECRET": ""}, clear=False),
            patch.object(App, "__init__", return_value=None),
            patch.object(App, "event", return_value=lambda f: f),
        ):
            from henchmen.dispatch.slack_bot import create_slack_app

            app = create_slack_app()
        assert app is not None


class TestSlackBotEventParsing:
    """Test Slack event parsing via the normalizer (as the bot delegates to it)."""

    def test_app_mention_extracts_text(self):
        normalizer = TaskNormalizer()
        event = {
            "event": {
                "type": "app_mention",
                "user": "U123",
                "channel": "C456",
                "ts": "1700000.000001",
                "text": "<@henchmen> fix the login bug in auth.py",
            }
        }
        task = normalizer.from_slack(event)
        assert "fix the login bug" in task.title
        assert task.created_by == "U123"
        assert task.source == TaskSource.SLACK

    def test_mention_stripped_from_title(self):
        normalizer = TaskNormalizer()
        event = {
            "event": {
                "type": "app_mention",
                "user": "U123",
                "channel": "C456",
                "ts": "1700000.000001",
                "text": "@henchmen please refactor the utils module",
            }
        }
        task = normalizer.from_slack(event)
        assert "@henchmen" not in task.title
        assert "refactor" in task.title

    def test_empty_text_gets_default_title(self):
        normalizer = TaskNormalizer()
        event = {
            "event": {
                "type": "app_mention",
                "user": "U123",
                "channel": "C456",
                "ts": "1700000.000001",
                "text": "",
            }
        }
        task = normalizer.from_slack(event)
        assert task.title == "Slack task"

    def test_long_title_truncated_to_200(self):
        normalizer = TaskNormalizer()
        long_text = "x" * 300
        event = {
            "event": {
                "type": "app_mention",
                "user": "U123",
                "channel": "C456",
                "ts": "1700000.000001",
                "text": long_text,
            }
        }
        task = normalizer.from_slack(event)
        assert len(task.title) <= 200


class TestSlackBotMentionDetection:
    """Test that @henchmen mentions are properly stripped and detected."""

    def test_angle_bracket_mention_stripped(self):
        normalizer = TaskNormalizer()
        event = {
            "event": {
                "user": "U123",
                "channel": "C456",
                "ts": "1700000.000001",
                "text": "<@henchmen> deploy to staging",
            }
        }
        task = normalizer.from_slack(event)
        assert "<@henchmen>" not in task.title

    def test_plain_mention_stripped(self):
        normalizer = TaskNormalizer()
        event = {
            "event": {
                "user": "U123",
                "channel": "C456",
                "ts": "1700000.000001",
                "text": "@henchmen run the tests",
            }
        }
        task = normalizer.from_slack(event)
        assert "@henchmen" not in task.title
        assert "run the tests" in task.title


class TestSlackBotMalformedEvents:
    """Test error handling for malformed Slack events."""

    def test_missing_event_key_falls_back(self):
        normalizer = TaskNormalizer()
        payload = {
            "user": "U123",
            "channel": "C456",
            "ts": "1700000.000001",
            "text": "do something",
        }
        task = normalizer.from_slack(payload)
        assert task.title is not None
        assert task.source == TaskSource.SLACK

    def test_missing_user_defaults_to_unknown(self):
        normalizer = TaskNormalizer()
        event = {
            "event": {
                "type": "app_mention",
                "channel": "C456",
                "ts": "1700000.000001",
                "text": "hello",
            }
        }
        task = normalizer.from_slack(event)
        assert task.created_by == "unknown"

    def test_missing_channel_and_ts_generates_uuid_source_id(self):
        normalizer = TaskNormalizer()
        event = {"event": {"type": "app_mention", "text": "do work"}}
        task = normalizer.from_slack(event)
        assert len(task.source_id) == 36

    def test_thread_messages_from_event_text(self):
        normalizer = TaskNormalizer()
        event = {
            "event": {
                "type": "app_mention",
                "user": "U123",
                "channel": "C456",
                "ts": "1700000.000001",
                "thread_ts": "1699999.000001",
                "text": "fix the auth module",
                "thread_messages": ["previous context message"],
            }
        }
        task = normalizer.from_slack(event)
        assert task.context.thread_messages is not None
        assert any("fix the auth module" in m for m in task.context.thread_messages)


# ---------------------------------------------------------------------------
# Normalizer regressions (real Slack markup, thread context, repo fallback)
# ---------------------------------------------------------------------------


class TestNormalizerRegressions:
    def test_real_slack_mention_markup_stripped_from_title(self):
        """Slack sends ``<@U0BOT123>``, never the literal ``<@henchmen>``."""
        n = TaskNormalizer()
        event = {
            "event": {
                "type": "app_mention",
                "user": "U123",
                "channel": "C456",
                "ts": "1700000.000001",
                "text": "<@U0BOT123> fix the login bug",
            }
        }
        task = n.from_slack(event)
        assert "<@" not in task.title
        assert task.title == "fix the login bug"

    def test_slack_mention_markup_with_label_stripped(self):
        n = TaskNormalizer()
        event = {"event": {"type": "app_mention", "text": "<@U0BOT123|henchmen> deploy staging"}}
        task = n.from_slack(event)
        assert "<@" not in task.title
        assert task.title == "deploy staging"

    def test_fetched_thread_messages_reach_the_task(self):
        """The bot puts fetched replies on ``event.thread_messages`` (strings)."""
        n = TaskNormalizer()
        event = {
            "event": {
                "type": "app_mention",
                "user": "U123",
                "channel": "C456",
                "ts": "1700000.000001",
                "thread_ts": "1699999.000001",
                "text": "fix the auth module",
                "thread_messages": ["previous context message", "fix the auth module"],
            }
        }
        task = n.from_slack(event)
        messages = task.context.thread_messages or []
        assert any("previous context message" in m for m in messages)
        # The mention text must not be duplicated.
        assert messages.count("fix the auth module") == 1

    def test_slack_repo_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "acme/fallback")
        settings = _mock_settings()
        n = TaskNormalizer()
        task = n.from_slack({"event": {"type": "app_mention", "text": "do it"}}, settings)
        assert task.context.repo == "acme/fallback"

    def test_a_bare_default_repo_is_qualified_with_the_default_org(self):
        from henchmen.config.settings import Settings

        settings = Settings(**{"_env_file": None, "github_default_org": "acme", "github_default_repo": "webapp"})
        n = TaskNormalizer()
        assert n.from_cli({"title": "T"}, settings).context.repo == "acme/webapp"
        assert n.from_slack({"event": {"type": "app_mention", "text": "do it"}}, settings).context.repo == "acme/webapp"
        assert n.from_jira({"issue": {"key": "P-1", "fields": {"summary": "S"}}}, settings).context.repo == (
            "acme/webapp"
        )
        # A task that names its repository keeps it.
        assert n.from_cli({"title": "T", "repo": "globex/api"}, settings).context.repo == "globex/api"

    def test_a_bare_default_repo_without_an_org_is_not_used(self, caplog):
        from henchmen.config.settings import Settings

        settings = Settings(**{"_env_file": None, "github_default_org": "", "github_default_repo": "webapp"})
        with caplog.at_level("WARNING", logger="henchmen.dispatch.normalizer"):
            task = TaskNormalizer().from_cli({"title": "T"}, settings)
        assert task.context.repo == ""
        assert "default repository must be owner/name" in caplog.text

    def test_create_task_refuses_a_bare_default_repo(self, monkeypatch):
        from henchmen.config.settings import Settings
        from henchmen.dispatch import server

        # Explicit instance: a developer's .env.local must not supply an org.
        settings = Settings(
            **{
                "_env_file": None,
                "provider": "local",
                "gcp_project_id": "test-project",
                "github_default_org": "",
                "github_default_repo": "webapp",
            }
        )
        with (
            patch.object(server, "get_settings", return_value=settings),
            patch.object(server, "handle_cli_request", AsyncMock(side_effect=AssertionError("not published"))),
            TestClient(server.app) as client,
        ):
            response = client.post("/api/v1/tasks", json={"title": "T"})
        assert response.status_code == 422
        assert "owner/name" in response.json()["detail"]

    def test_cli_repo_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "acme/fallback")
        settings = _mock_settings()
        n = TaskNormalizer()
        task = n.from_cli({"title": "T"}, settings)
        assert task.context.repo == "acme/fallback"

    def test_jira_repo_from_plain_field_then_default(self, monkeypatch):
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "acme/fallback")
        settings = _mock_settings()
        n = TaskNormalizer()

        explicit = n.from_jira(
            {"issue": {"key": "P-1", "fields": {"summary": "S", "repo": "acme/explicit"}}},
            settings,
        )
        assert explicit.context.repo == "acme/explicit"

        fallback = n.from_jira({"issue": {"key": "P-2", "fields": {"summary": "S"}}}, settings)
        assert fallback.context.repo == "acme/fallback"

    def test_jira_repo_and_branch_from_configured_custom_field_ids(self, monkeypatch):
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "acme/fallback")
        monkeypatch.setenv("HENCHMEN_JIRA_REPO_FIELD", "customfield_10042")
        monkeypatch.setenv("HENCHMEN_JIRA_BRANCH_FIELD", "customfield_10043")
        settings = _mock_settings()
        fields = {
            "summary": "S",
            "customfield_10042": "acme/api",
            # A select-list custom field arrives as an option object.
            "customfield_10043": {"value": "develop", "id": "10001"},
            # The configured field id wins over the plain name.
            "repo": "acme/plain",
        }

        task = TaskNormalizer().from_jira({"issue": {"key": "P-3", "fields": fields}}, settings)

        assert task.context.repo == "acme/api"
        assert task.context.branch == "develop"

    def test_jira_unconfigured_custom_field_ids_are_not_guessed(self, monkeypatch):
        """``customfield_repo`` cannot exist in Jira; without a configured id only plain names count."""
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "acme/fallback")
        monkeypatch.setenv("HENCHMEN_JIRA_REPO_FIELD", "")
        monkeypatch.setenv("HENCHMEN_JIRA_BRANCH_FIELD", "")
        settings = _mock_settings()
        fields = {"summary": "S", "customfield_repo": "acme/impossible", "customfield_branch": "nope"}

        task = TaskNormalizer().from_jira({"issue": {"key": "P-4", "fields": fields}}, settings)

        assert task.context.repo == "acme/fallback"
        assert task.context.branch is None

    def test_issue_comment_on_pr_uses_comment_body_and_pr_source_id(self):
        n = TaskNormalizer()
        payload = {
            "action": "created",
            "issue": {
                "number": 12,
                "title": "Broken login",
                "body": "issue body",
                "pull_request": {"url": "https://api.github.com/repos/acme/api/pulls/12"},
                "user": {"login": "alice"},
                "labels": [],
                "state": "open",
            },
            "comment": {"body": "@henchmen fix this", "user": {"login": "bob"}},
            "repository": {"full_name": "acme/api", "default_branch": "main"},
        }
        task = n.from_github(payload)
        assert task.source_id == "pr-12"
        assert task.description == "@henchmen fix this"
        assert task.created_by == "bob"

    @pytest.mark.asyncio
    async def test_publish_task_attaches_dedup_key(self):
        n = TaskNormalizer()
        settings = _mock_settings()
        task = n.from_cli({"title": "T", "repo": "acme/api"})
        broker = AsyncMock()
        broker.publish = AsyncMock(return_value="m1")

        await n.publish_task(task, settings, broker=broker, dedup_key="github:abc")

        kwargs = broker.publish.call_args[1]
        assert kwargs["dedup_key"] == "github:abc"
        assert kwargs["task_id"] == task.id


# ---------------------------------------------------------------------------
# GitHub trigger predicates (action filter, authorization, CI conclusions)
# ---------------------------------------------------------------------------


def _pr_comment(action="created", association="COLLABORATOR", body="@henchmen fix this"):
    return {
        "action": action,
        "pull_request": {
            "number": 3,
            "title": "PR title",
            "body": "PR body",
            "user": {"login": "dev"},
            "labels": [],
            "state": "open",
            "head": {"ref": "feature"},
        },
        "comment": {"body": body, "user": {"login": "reviewer"}, "author_association": association},
        "repository": {"full_name": "acme/api"},
    }


class TestGithubTriggerFilters:
    @pytest.mark.asyncio
    async def test_edited_comment_does_not_dispatch(self):
        from henchmen.dispatch.handlers.github import handle_github_webhook

        normalizer = TaskNormalizer()
        with patch.object(normalizer, "publish_task", new=AsyncMock()) as publish:
            result = await handle_github_webhook(
                _pr_comment(action="edited"), normalizer, _mock_settings(), broker=AsyncMock()
            )
        assert result["status"] == "ignored"
        publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deleted_comment_does_not_dispatch(self):
        from henchmen.dispatch.handlers.github import handle_github_webhook

        normalizer = TaskNormalizer()
        result = await handle_github_webhook(
            _pr_comment(action="deleted"), normalizer, _mock_settings(), broker=AsyncMock()
        )
        assert result["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_unauthorized_commenter_is_rejected(self):
        from henchmen.dispatch.handlers.github import handle_github_webhook

        normalizer = TaskNormalizer()
        with patch.object(normalizer, "publish_task", new=AsyncMock()) as publish:
            result = await handle_github_webhook(
                _pr_comment(association="NONE"), normalizer, _mock_settings(), broker=AsyncMock()
            )
        assert result == {"status": "ignored", "reason": "unauthorized commenter"}
        publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_author_association_is_rejected(self):
        """Fail closed: a payload without author_association never dispatches."""
        from henchmen.dispatch.handlers.github import handle_github_webhook

        payload = _pr_comment()
        del payload["comment"]["author_association"]
        normalizer = TaskNormalizer()
        result = await handle_github_webhook(payload, normalizer, _mock_settings(), broker=AsyncMock())
        assert result["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_issue_comment_on_pr_dispatches(self):
        from henchmen.dispatch.handlers.github import handle_github_webhook

        payload = {
            "action": "created",
            "issue": {
                "number": 12,
                "title": "Broken login",
                "body": "",
                "pull_request": {"url": "https://api.github.com/repos/acme/api/pulls/12"},
                "user": {"login": "alice"},
                "labels": [],
                "state": "open",
            },
            "comment": {
                "body": "@henchmen fix this",
                "user": {"login": "alice"},
                "author_association": "OWNER",
            },
            "repository": {"full_name": "acme/api", "default_branch": "main"},
        }
        normalizer = TaskNormalizer()
        with patch.object(normalizer, "publish_task", new=AsyncMock(return_value="m")):
            result = await handle_github_webhook(payload, normalizer, _mock_settings(), broker=AsyncMock())
        assert result["status"] == "dispatched"
        assert result["trigger"] == "pr_comment"

    def test_red_check_suite_conclusions_count_as_ci_failure(self):
        from henchmen.dispatch.handlers.github import _is_ci_failure_on_henchmen_branch

        for conclusion in ("failure", "timed_out", "startup_failure", "action_required"):
            payload = {
                "action": "completed",
                "check_suite": {"conclusion": conclusion, "head_branch": "henchmen/abc123"},
            }
            assert _is_ci_failure_on_henchmen_branch(payload) is True, conclusion

    def test_success_and_cancelled_are_not_ci_failures(self):
        from henchmen.dispatch.handlers.github import _is_ci_failure_on_henchmen_branch

        for conclusion in ("success", "cancelled", "neutral", "skipped", "stale", None):
            payload = {
                "action": "completed",
                "check_suite": {"conclusion": conclusion, "head_branch": "henchmen/abc123"},
            }
            assert _is_ci_failure_on_henchmen_branch(payload) is False, conclusion

    @pytest.mark.asyncio
    async def test_ci_failure_payload_includes_conclusion(self):
        import json

        from henchmen.dispatch.handlers.github import handle_ci_failure_webhook

        payload = {
            "action": "completed",
            "check_suite": {
                "conclusion": "timed_out",
                "head_branch": "henchmen/task-1",
                "id": 7,
                "head_sha": "deadbeef",
            },
            "repository": {"full_name": "acme/api"},
        }
        broker = AsyncMock()
        broker.publish = AsyncMock(return_value="m")
        result = await handle_ci_failure_webhook(payload, _mock_settings(), broker=broker)
        assert result["conclusion"] == "timed_out"
        published = json.loads(broker.publish.call_args[0][1].decode())
        assert published["conclusion"] == "timed_out"


# ---------------------------------------------------------------------------
# Jira transition detection
# ---------------------------------------------------------------------------


class TestJiraTransitionDetection:
    @pytest.mark.asyncio
    async def test_changelog_status_change_dispatches(self):
        from henchmen.dispatch.handlers.jira import handle_jira_webhook

        payload = {
            "webhookEvent": "jira:issue_updated",
            "changelog": {"items": [{"field": "status", "toString": "Ready for Henchmen"}]},
            "issue": {"key": "PROJ-9", "fields": {"summary": "Do it", "repo": "acme/api"}},
        }
        normalizer = TaskNormalizer()
        with patch.object(normalizer, "publish_task", new=AsyncMock(return_value="m")):
            result = await handle_jira_webhook(payload, normalizer, _mock_settings(), broker=AsyncMock())
        assert result["status"] == "dispatched"

    @pytest.mark.asyncio
    async def test_unrelated_changelog_is_ignored(self):
        from henchmen.dispatch.handlers.jira import handle_jira_webhook

        payload = {
            "webhookEvent": "jira:issue_updated",
            "changelog": {"items": [{"field": "assignee", "toString": "someone"}]},
            "issue": {"key": "PROJ-9", "fields": {"summary": "Do it"}},
        }
        result = await handle_jira_webhook(payload, TaskNormalizer(), _mock_settings(), broker=AsyncMock())
        assert result["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_post_function_transition_still_dispatches(self):
        from henchmen.dispatch.handlers.jira import handle_jira_webhook

        payload = {
            "transition": {"transitionName": "Ready for Henchmen"},
            "issue": {"key": "PROJ-9", "fields": {"summary": "Do it"}},
        }
        normalizer = TaskNormalizer()
        with patch.object(normalizer, "publish_task", new=AsyncMock(return_value="m")):
            result = await handle_jira_webhook(payload, normalizer, _mock_settings(), broker=AsyncMock())
        assert result["status"] == "dispatched"


# ---------------------------------------------------------------------------
# Slack mention detection via the bot's own user id
# ---------------------------------------------------------------------------


class TestSlackMentionDetection:
    @pytest.mark.asyncio
    async def test_message_event_with_bot_user_id_dispatches(self):
        from henchmen.dispatch.handlers.slack import handle_slack_event

        payload = {
            "authorizations": [{"user_id": "U0BOT123"}],
            "event": {
                "type": "message",
                "user": "U1",
                "channel": "C1",
                "ts": "1700.1",
                "text": "<@U0BOT123> please fix this",
            },
        }
        normalizer = TaskNormalizer()
        with patch.object(normalizer, "publish_task", new=AsyncMock(return_value="m")):
            result = await handle_slack_event(payload, normalizer, _mock_settings(), broker=AsyncMock())
        assert result["status"] == "dispatched"

    @pytest.mark.asyncio
    async def test_message_mentioning_another_user_is_ignored(self):
        from henchmen.dispatch.handlers.slack import handle_slack_event

        payload = {
            "authorizations": [{"user_id": "U0BOT123"}],
            "event": {"type": "message", "user": "U1", "text": "<@U9999999> ping"},
        }
        result = await handle_slack_event(payload, TaskNormalizer(), _mock_settings(), broker=AsyncMock())
        assert result["status"] == "ignored"


# ---------------------------------------------------------------------------
# Webhook signature verification (fail-closed intake)
# ---------------------------------------------------------------------------


def json_dumps(obj) -> bytes:
    """Serialize *obj* exactly as it will be signed and sent."""
    import json

    return json.dumps(obj).encode("utf-8")


def _sign_sha256(secret: str, body: bytes) -> str:
    import hashlib
    import hmac

    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _sign_slack(secret: str, timestamp: str, body: bytes) -> str:
    import hashlib
    import hmac

    basestring = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


class TestWebhookSignatures:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
        monkeypatch.setenv("HENCHMEN_GITHUB_WEBHOOK_SECRET", "gh-secret")
        monkeypatch.setenv("HENCHMEN_SLACK_SIGNING_SECRET", "slack-secret")
        monkeypatch.setenv("HENCHMEN_JIRA_WEBHOOK_SECRET", "jira-secret")
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "acme/api")
        yield

    @pytest.fixture
    def client(self):
        import henchmen.dispatch.server as server

        server._delivery_guard.clear()
        with TestClient(server.app) as c:
            yield c

    def test_github_valid_signature_accepted(self, client):
        body = json_dumps({"action": "opened"})
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign_sha256("gh-secret", body),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_github_bad_signature_rejected(self, client):
        body = json_dumps({"action": "opened"})
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=deadbeef"},
        )
        assert resp.status_code == 401

    def test_github_missing_signature_rejected(self, client):
        body = json_dumps({"action": "opened"})
        resp = client.post("/webhooks/github", content=body, headers={"Content-Type": "application/json"})
        assert resp.status_code == 401

    def test_slack_valid_signature_accepted(self, client):
        import time

        body = json_dumps({"event": {"type": "message", "text": "hello"}})
        ts = str(int(time.time()))
        resp = client.post(
            "/webhooks/slack",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": _sign_slack("slack-secret", ts, body),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_slack_stale_timestamp_rejected(self, client):
        import time

        body = json_dumps({"event": {"type": "app_mention", "text": "@henchmen go"}})
        ts = str(int(time.time()) - 3600)
        resp = client.post(
            "/webhooks/slack",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": _sign_slack("slack-secret", ts, body),
            },
        )
        assert resp.status_code == 401

    def test_slack_bad_signature_rejected(self, client):
        import time

        body = json_dumps({"event": {"type": "app_mention", "text": "@henchmen go"}})
        resp = client.post(
            "/webhooks/slack",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": str(int(time.time())),
                "X-Slack-Signature": "v0=nope",
            },
        )
        assert resp.status_code == 401

    def test_slack_non_utf8_body_is_rejected_not_crashed(self, client):
        """A non-UTF-8 body must fail the HMAC check rather than raise."""
        import time

        body = b"\xff\xfe not json"
        ts = str(int(time.time()))
        resp = client.post(
            "/webhooks/slack",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": _sign_slack("slack-secret", ts, body),
            },
        )
        # Signature is valid, so the failure must come from JSON parsing (400).
        assert resp.status_code == 400

    def test_jira_x_hub_signature_accepted(self, client):
        """Jira Cloud signs with X-Hub-Signature (this was the broken header)."""
        body = json_dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "P-1", "fields": {}}})
        resp = client.post(
            "/webhooks/jira",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature": _sign_sha256("jira-secret", body)},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    def test_jira_atlassian_header_still_accepted(self, client):
        body = json_dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "P-2", "fields": {}}})
        resp = client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Atlassian-Webhook-Signature": _sign_sha256("jira-secret", body),
            },
        )
        assert resp.status_code == 200

    def test_jira_missing_signature_rejected(self, client):
        body = json_dumps({"webhookEvent": "jira:issue_updated", "issue": {"key": "P-3", "fields": {}}})
        resp = client.post("/webhooks/jira", content=body, headers={"Content-Type": "application/json"})
        assert resp.status_code == 401


class TestRequireSigningSecret:
    def test_dev_allows_missing_secret(self, monkeypatch, caplog):
        from henchmen.config.settings import Environment, Settings
        from henchmen.dispatch.server import _require_signing_secret

        monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
        with caplog.at_level("WARNING", logger="henchmen.dispatch.server"):
            _require_signing_secret(Settings(_env_file=None, environment=Environment.DEV), "", integration="slack")
        assert "fail-open is allowed" in caplog.text

    @pytest.mark.parametrize("env_name", ["staging", "prod"])
    def test_staging_and_prod_reject_missing_secret(self, env_name, monkeypatch):
        from fastapi import HTTPException

        from henchmen.config.settings import Environment, Settings
        from henchmen.dispatch.server import _require_signing_secret

        monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
        with pytest.raises(HTTPException) as exc:
            _require_signing_secret(
                Settings(_env_file=None, environment=Environment(env_name), gcp_project_id="p"),
                "",
                integration="github",
            )
        assert exc.value.status_code == 401

    @pytest.mark.parametrize(
        ("env_name", "desktop", "refused"),
        [("dev", False, False), ("dev", True, True), ("staging", False, True), ("prod", True, True)],
    )
    def test_equivalent_to_the_old_environment_or_desktop_rule(self, env_name, desktop, refused, monkeypatch, tmp_path):
        """C2: routing through fail_open_allowed keeps the old "staging/prod or desktop" decision."""
        from fastapi import HTTPException

        from henchmen.config.settings import Environment, Settings
        from henchmen.dispatch.server import _require_signing_secret

        if desktop:
            monkeypatch.setenv("HENCHMEN_DATA_DIR", str(tmp_path))
        else:
            monkeypatch.delenv("HENCHMEN_DATA_DIR", raising=False)
        settings = Settings(_env_file=None, environment=Environment(env_name), gcp_project_id="p")
        if refused:
            with pytest.raises(HTTPException) as exc:
                _require_signing_secret(settings, "", integration="jira")
            assert exc.value.status_code == 401
        else:
            _require_signing_secret(settings, "", integration="jira")
        _require_signing_secret(settings, "configured", integration="jira")  # a configured secret always passes


# ---------------------------------------------------------------------------
# Pub/Sub OIDC verification
# ---------------------------------------------------------------------------


def _pubsub_request(headers=None):
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/pubsub/example",
            "headers": raw,
            "query_string": b"",
            "client": ("10.0.0.1", 1234),
        }
    )


class TestPubsubOidc:
    @pytest.mark.asyncio
    async def test_dev_without_audience_or_token_is_allowed(self, monkeypatch):
        from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc

        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
        monkeypatch.setenv("HENCHMEN_PUBSUB_OIDC_AUDIENCE", "")
        await verify_pubsub_oidc(_pubsub_request(), _mock_settings())

    @pytest.mark.asyncio
    async def test_dev_with_token_but_no_audience_is_rejected(self, monkeypatch):
        from fastapi import HTTPException

        from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc

        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
        monkeypatch.setenv("HENCHMEN_PUBSUB_OIDC_AUDIENCE", "")
        with pytest.raises(HTTPException) as exc:
            await verify_pubsub_oidc(_pubsub_request({"Authorization": "Bearer abc"}), _mock_settings())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_staging_without_audience_is_rejected(self, monkeypatch):
        from fastapi import HTTPException

        from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc

        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "staging")
        monkeypatch.setenv("HENCHMEN_PUBSUB_OIDC_AUDIENCE", "")
        with pytest.raises(HTTPException) as exc:
            await verify_pubsub_oidc(_pubsub_request(), _mock_settings())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_audience_set_but_no_token_is_rejected(self, monkeypatch):
        from fastapi import HTTPException

        from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc

        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
        monkeypatch.setenv("HENCHMEN_PUBSUB_OIDC_AUDIENCE", "https://dispatch.example")
        with pytest.raises(HTTPException) as exc:
            await verify_pubsub_oidc(_pubsub_request(), _mock_settings())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_outside_email_allow_list_is_rejected(self, monkeypatch):
        from fastapi import HTTPException

        from henchmen.dispatch import pubsub_auth

        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "prod")
        monkeypatch.setenv("HENCHMEN_PUBSUB_OIDC_AUDIENCE", "https://dispatch.example")
        monkeypatch.setenv("HENCHMEN_PUBSUB_OIDC_ALLOWED_EMAILS", "pubsub@acme.iam.gserviceaccount.com")

        fake_id_token = MagicMock()
        fake_id_token.verify_oauth2_token.return_value = {"email": "intruder@evil.example"}
        with (
            patch.dict(
                "sys.modules",
                {
                    "google.auth.transport.requests": MagicMock(),
                    "google.oauth2.id_token": fake_id_token,
                },
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await pubsub_auth.verify_pubsub_oidc(_pubsub_request({"Authorization": "Bearer jwt"}), _mock_settings())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_on_allow_list_is_accepted_and_claims_attached(self, monkeypatch):
        from henchmen.dispatch import pubsub_auth

        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "prod")
        monkeypatch.setenv("HENCHMEN_PUBSUB_OIDC_AUDIENCE", "https://dispatch.example")
        monkeypatch.setenv("HENCHMEN_PUBSUB_OIDC_ALLOWED_EMAILS", "pubsub@acme.iam.gserviceaccount.com")

        claims = {"email": "pubsub@acme.iam.gserviceaccount.com", "aud": "https://dispatch.example"}
        fake_id_token = MagicMock()
        fake_id_token.verify_oauth2_token.return_value = claims
        request = _pubsub_request({"Authorization": "Bearer good-jwt"})
        with patch.dict(
            "sys.modules",
            {"google.auth.transport.requests": MagicMock(), "google.oauth2.id_token": fake_id_token},
        ):
            await pubsub_auth.verify_pubsub_oidc(request, _mock_settings())

        assert request.state.pubsub_oidc_claims == claims
        args = fake_id_token.verify_oauth2_token.call_args[0]
        assert args[0] == "good-jwt"
        assert args[2] == "https://dispatch.example"

    @pytest.mark.asyncio
    async def test_token_that_fails_verification_is_rejected(self, monkeypatch):
        from fastapi import HTTPException

        from henchmen.dispatch import pubsub_auth

        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "prod")
        monkeypatch.setenv("HENCHMEN_PUBSUB_OIDC_AUDIENCE", "https://dispatch.example")
        monkeypatch.setenv("HENCHMEN_PUBSUB_OIDC_ALLOWED_EMAILS", "")

        fake_id_token = MagicMock()
        fake_id_token.verify_oauth2_token.side_effect = ValueError("Token has wrong audience")
        with (
            patch.dict(
                "sys.modules",
                {"google.auth.transport.requests": MagicMock(), "google.oauth2.id_token": fake_id_token},
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await pubsub_auth.verify_pubsub_oidc(_pubsub_request({"Authorization": "Bearer bad"}), _mock_settings())
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_non_bearer_authorization_scheme_is_rejected(self, monkeypatch):
        from fastapi import HTTPException

        from henchmen.dispatch.pubsub_auth import verify_pubsub_oidc

        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "prod")
        monkeypatch.setenv("HENCHMEN_PUBSUB_OIDC_AUDIENCE", "https://dispatch.example")
        with pytest.raises(HTTPException) as exc:
            await verify_pubsub_oidc(_pubsub_request({"Authorization": "Basic dXNlcjpwYXNz"}), _mock_settings())
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# /api/v1/tasks request validation
# ---------------------------------------------------------------------------


class TestCreateTaskValidation:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        # Set empty rather than delete: Settings also reads .env.local.
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "")
        yield

    @pytest.fixture
    def client(self):
        from henchmen.dispatch.server import app

        with TestClient(app) as c:
            yield c

    def test_missing_title_returns_422(self, client):
        assert client.post("/api/v1/tasks", json={}).status_code == 422

    def test_invalid_priority_returns_422(self, client):
        resp = client.post("/api/v1/tasks", json={"title": "T", "repo": "a/b", "priority": "urgent"})
        assert resp.status_code == 422

    def test_non_string_title_returns_422(self, client):
        resp = client.post("/api/v1/tasks", json={"title": 12345, "repo": "a/b"})
        assert resp.status_code == 422

    def test_json_string_body_returns_422(self, client):
        resp = client.post("/api/v1/tasks", json="title")
        assert resp.status_code == 422

    def test_missing_repo_without_default_returns_422(self, client):
        resp = client.post("/api/v1/tasks", json={"title": "T"})
        assert resp.status_code == 422

    def test_repo_falls_back_to_default(self, client, monkeypatch):
        from henchmen.config.settings import get_settings

        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "acme/api")
        # The client fixture already warmed the Settings cache with the empty
        # value set by ``_env``; drop it so the route sees the new default.
        get_settings.cache_clear()
        resp = client.post("/api/v1/tasks", json={"title": "T"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "dispatched"


class TestCreateTaskAuth:
    """``POST /api/v1/tasks`` launches paid runs, so it requires a bearer token outside dev."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        import henchmen.dispatch.server as server

        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "acme/api")
        # Set empty rather than delete: Settings also reads .env.local.
        monkeypatch.setenv("HENCHMEN_DISPATCH_API_TOKEN", "")
        monkeypatch.delenv("DISPATCH_API_TOKEN", raising=False)
        monkeypatch.setattr(server, "_open_api_warning_logged", False)
        yield

    @pytest.fixture
    def client(self):
        from henchmen.dispatch.server import app

        with TestClient(app) as c:
            yield c

    @staticmethod
    def _configure(monkeypatch, **env: str) -> None:
        from henchmen.config.settings import get_settings

        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()

    def test_dev_without_token_is_open_and_warns_once(self, client, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="henchmen.dispatch.server"):
            assert client.post("/api/v1/tasks", json={"title": "T"}).status_code == 200
            assert client.post("/api/v1/tasks", json={"title": "T"}).status_code == 200
        warnings = [r for r in caplog.records if "HENCHMEN_DISPATCH_API_TOKEN is empty" in r.getMessage()]
        assert len(warnings) == 1

    @pytest.mark.parametrize("env_name", ["staging", "prod"])
    def test_missing_token_outside_dev_fails_closed(self, client, monkeypatch, env_name):
        self._configure(monkeypatch, HENCHMEN_ENVIRONMENT=env_name)
        resp = client.post("/api/v1/tasks", json={"title": "T"})
        assert resp.status_code == 401

    def test_auth_is_checked_before_body_validation(self, client, monkeypatch):
        """An unauthenticated caller gets 401, not a 422 that describes the request schema."""
        self._configure(monkeypatch, HENCHMEN_DISPATCH_API_TOKEN="s3cret")
        assert client.post("/api/v1/tasks", json={}).status_code == 401

    @pytest.mark.parametrize(
        "header",
        [None, "Bearer wrong", "Basic czNjcmV0", "Bearer ", "s3cret"],
    )
    def test_wrong_or_missing_token_is_rejected(self, client, monkeypatch, header):
        self._configure(monkeypatch, HENCHMEN_ENVIRONMENT="prod", HENCHMEN_DISPATCH_API_TOKEN="s3cret")
        headers = {"Authorization": header} if header is not None else {}
        resp = client.post("/api/v1/tasks", json={"title": "T"}, headers=headers)
        assert resp.status_code == 401
        assert resp.headers["WWW-Authenticate"] == "Bearer"
        assert "s3cret" not in resp.text

    def test_terraform_placeholder_is_not_a_token(self, client, monkeypatch):
        """The seeded Secret Manager placeholder is public; it must not unlock the route."""
        from henchmen.config.settings import SEEDED_SECRET_PLACEHOLDER as _SEEDED_SECRET_PLACEHOLDER

        self._configure(
            monkeypatch, HENCHMEN_ENVIRONMENT="prod", HENCHMEN_DISPATCH_API_TOKEN=_SEEDED_SECRET_PLACEHOLDER
        )
        resp = client.post(
            "/api/v1/tasks",
            json={"title": "T"},
            headers={"Authorization": f"Bearer {_SEEDED_SECRET_PLACEHOLDER}"},
        )
        assert resp.status_code == 401

    def test_placeholder_matches_the_terraform_seed(self):
        from pathlib import Path

        from henchmen.config.settings import SEEDED_SECRET_PLACEHOLDER as _SEEDED_SECRET_PLACEHOLDER

        secrets_tf = Path(__file__).resolve().parents[2] / "terraform" / "modules" / "secrets" / "main.tf"
        assert f'secret_data = "{_SEEDED_SECRET_PLACEHOLDER}"' in secrets_tf.read_text(encoding="utf-8")

    def test_correct_token_is_accepted(self, client, monkeypatch):
        self._configure(monkeypatch, HENCHMEN_ENVIRONMENT="prod", HENCHMEN_DISPATCH_API_TOKEN="s3cret")
        resp = client.post("/api/v1/tasks", json={"title": "T"}, headers={"Authorization": "Bearer s3cret"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "dispatched"

    def test_bare_secret_mount_name_is_honoured(self, client, monkeypatch):
        """Cloud Run mounts the secret as DISPATCH_API_TOKEN."""
        monkeypatch.delenv("HENCHMEN_DISPATCH_API_TOKEN")
        self._configure(monkeypatch, HENCHMEN_ENVIRONMENT="staging", DISPATCH_API_TOKEN="mounted")
        denied = client.post("/api/v1/tasks", json={"title": "T"}, headers={"Authorization": "Bearer other"})
        allowed = client.post("/api/v1/tasks", json={"title": "T"}, headers={"Authorization": "bearer mounted"})
        assert denied.status_code == 401
        assert allowed.status_code == 200


# ---------------------------------------------------------------------------
# Webhook replay protection
# ---------------------------------------------------------------------------


class TestWebhookIdempotency:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
        monkeypatch.setenv("HENCHMEN_GITHUB_DEFAULT_REPO", "acme/api")
        yield

    @pytest.fixture
    def client(self):
        import henchmen.dispatch.server as server

        server._delivery_guard.clear()
        with TestClient(server.app) as c:
            yield c

    def _issue_labeled(self):
        return {
            "action": "labeled",
            "label": {"name": "henchmen"},
            "issue": {
                "number": 5,
                "title": "Bug report",
                "body": "",
                "user": {"login": "alice"},
                "labels": [{"name": "henchmen"}],
                "state": "open",
            },
            "repository": {"full_name": "acme/api", "default_branch": "main"},
        }

    def test_github_redelivery_is_ignored(self, client):
        headers = {"X-GitHub-Delivery": "delivery-1"}
        first = client.post("/webhooks/github", json=self._issue_labeled(), headers=headers)
        second = client.post("/webhooks/github", json=self._issue_labeled(), headers=headers)
        assert first.json()["status"] == "dispatched"
        assert second.json()["status"] == "ignored"
        assert second.json()["reason"] == "duplicate delivery"

    def test_distinct_deliveries_both_dispatch(self, client):
        first = client.post("/webhooks/github", json=self._issue_labeled(), headers={"X-GitHub-Delivery": "delivery-a"})
        second = client.post(
            "/webhooks/github", json=self._issue_labeled(), headers={"X-GitHub-Delivery": "delivery-b"}
        )
        assert first.json()["status"] == "dispatched"
        assert second.json()["status"] == "dispatched"

    def test_slack_retry_header_short_circuits(self, client):
        payload = {
            "event_id": "Ev123",
            "event": {"type": "app_mention", "user": "U1", "channel": "C1", "ts": "1.1", "text": "go"},
        }
        resp = client.post("/webhooks/slack", json=payload, headers={"X-Slack-Retry-Num": "1"})
        assert resp.json() == {"status": "ignored", "reason": "slack retry"}

    def test_slack_same_event_id_is_ignored_second_time(self, client):
        payload = {
            "event_id": "Ev999",
            "event": {"type": "app_mention", "user": "U1", "channel": "C1", "ts": "1.1", "text": "go"},
        }
        first = client.post("/webhooks/slack", json=payload)
        second = client.post("/webhooks/slack", json=payload)
        assert first.json()["status"] == "dispatched"
        assert second.json()["status"] == "ignored"


class TestTTLSet:
    def test_first_add_is_new_and_replay_is_not(self):
        from henchmen.dispatch.idempotency import TTLSet

        guard = TTLSet()
        assert guard.add_if_absent("k") is True
        assert guard.add_if_absent("k") is False

    def test_empty_key_is_never_deduped(self):
        from henchmen.dispatch.idempotency import TTLSet

        guard = TTLSet()
        assert guard.add_if_absent("") is True
        assert guard.add_if_absent("") is True

    def test_entries_expire(self):
        from henchmen.dispatch.idempotency import TTLSet

        guard = TTLSet(ttl_seconds=0.0)
        assert guard.add_if_absent("k") is True
        assert guard.add_if_absent("k") is True

    def test_max_entries_is_enforced(self):
        from henchmen.dispatch.idempotency import TTLSet

        guard = TTLSet(max_entries=5)
        for i in range(50):
            guard.add_if_absent(f"k{i}")
        assert len(guard._seen) <= 5


def test_importing_dispatch_server_installs_secret_redaction():
    """Dispatch logs intake payloads; token-shaped strings must be redacted in this process.

    Runs in a fresh interpreter: another test module importing Mastermind would
    already have installed the factory in this one.
    """
    import os
    import subprocess
    import sys

    code = (
        "import logging, henchmen.dispatch.server\n"
        "from henchmen.utils.redaction import _redacting_factory\n"
        "assert logging.getLogRecordFactory() is _redacting_factory\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path), "HENCHMEN_PROVIDER": "local"}
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Container contract: the dispatch image must serve the FastAPI app
# ---------------------------------------------------------------------------


class TestDispatchContainer:
    def _repo_root(self):
        import pathlib

        return pathlib.Path(__file__).resolve().parents[2]

    def test_entrypoint_execs_uvicorn_with_the_fastapi_app(self):
        entrypoint = (self._repo_root() / "containers" / "dispatch" / "entrypoint.sh").read_text(encoding="utf-8")
        assert "uvicorn henchmen.dispatch.server:app" in entrypoint
        assert "exec uvicorn" in entrypoint
        # The stub health server that only answered GET must be gone.
        assert "BaseHTTPRequestHandler" not in entrypoint

    def test_dockerfile_runs_the_entrypoint(self):
        dockerfile = (self._repo_root() / "containers" / "dispatch" / "Dockerfile").read_text(encoding="utf-8")
        assert 'CMD ["./entrypoint.sh"]' in dockerfile

    def test_requirements_drop_unused_packages(self):
        reqs = (self._repo_root() / "containers" / "dispatch" / "requirements.txt").read_text(encoding="utf-8")
        for unused in ("jira", "google-cloud-secret-manager", "google-cloud-logging"):
            assert unused not in reqs
