"""Unit tests for Dispatch: TaskNormalizer, handlers, and server routes."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.models.task import HenchmenTask, TaskPriority, TaskSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_settings():
    s = MagicMock()
    s.gcp_project_id = "test-project"
    s.pubsub_topic_task_intake = "henchmen-task-intake"
    return s


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
                {"title": "Do something", "repo": "acme/api"},
                normalizer,
                settings,
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
            result = await handle_cli_request({"title": "T"}, normalizer, settings)

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
            result = await handle_slack_event(payload, normalizer, settings)

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

        result = await handle_slack_event(payload, normalizer, settings)
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
            result = await handle_slack_event(payload, normalizer, settings)

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
            result = await handle_github_webhook(payload, normalizer, settings)

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
            "comment": {"body": "@henchmen fix this", "user": {"login": "reviewer"}},
            "repository": {"full_name": "acme/api"},
        }

        with patch.object(normalizer, "publish_task", new=AsyncMock(return_value="msg-gh-2")):
            result = await handle_github_webhook(payload, normalizer, settings)

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

        result = await handle_github_webhook(payload, normalizer, settings)
        assert result["status"] == "ignored"


# ---------------------------------------------------------------------------
# Dispatch server route registration
# ---------------------------------------------------------------------------


class TestDispatchServerRoutes:
    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
        monkeypatch.setenv("HENCHMEN_PROVIDER", "local")
        from henchmen.config.settings import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

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
        assert response.status_code in (200, 400, 422, 500)

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

    def test_pubsub_task_planned_route_registered(self, client):
        import base64
        import json

        data = base64.b64encode(json.dumps({"task_id": "t1"}).encode()).decode()
        response = client.post(
            "/pubsub/task-planned",
            json={"message": {"data": data}},
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# CLI embed command
# ---------------------------------------------------------------------------


class TestCliEmbedCommand:
    @pytest.mark.asyncio
    async def test_embed_full_mode(self):
        from henchmen.dispatch.handlers.cli import handle_embed_command

        with patch("henchmen.dispatch.handlers.cli.run_embedding_pipeline", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {"status": "completed", "chunks_upserted": 100}
            result = await handle_embed_command(
                repo="acme-org/sample-repo",
                full=True,
                pinecone_api_key="test-key",
                settings=MagicMock(),
            )

        mock_run.assert_awaited_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["repo"] == "acme-org/sample-repo"
        assert call_kwargs["mode"] == "full"
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_embed_incremental_mode(self):
        from henchmen.dispatch.handlers.cli import handle_embed_command

        with patch("henchmen.dispatch.handlers.cli.run_embedding_pipeline", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {"status": "completed", "chunks_upserted": 5}
            await handle_embed_command(
                repo="acme-org/sample-repo",
                full=False,
                pinecone_api_key="test-key",
                settings=MagicMock(),
            )

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["mode"] == "incremental"


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
