"""Integration tests for the Dispatch pipeline.

Verifies each dispatch source flows end-to-end:
  raw input → normalizer → Pub/Sub publish

Uses mock GCP infrastructure from tests/integration/conftest.py.
All tests target the repo ``acme-org/sample-repo``.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from henchmen.config.settings import get_settings
from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.dispatch.server import app
from tests.integration.conftest import IntegrationAssertions, MockPubSubPublisher

TASK_INTAKE_TOPIC = "task-intake"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _post(client: AsyncClient, path: str, payload: dict) -> dict:
    """POST JSON and return parsed response body."""
    response = await client.post(path, json=payload)
    return response.json()


def _get_first_message(mock_pubsub: MockPubSubPublisher) -> dict:
    """Return the data dict from the first published message on the task-intake topic."""
    msgs = mock_pubsub.get_messages_for_topic(TASK_INTAKE_TOPIC)
    assert msgs, "No messages published to task-intake topic"
    return msgs[0]["data"]


# ---------------------------------------------------------------------------
# TestCLIDispatchPipeline
# ---------------------------------------------------------------------------


class TestCLIDispatchPipeline:
    """Tests the CLI → Dispatch → Pub/Sub flow via FastAPI routes."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_pubsub):
        """Ensure settings and mock Pub/Sub are active for every test."""
        # integration_settings monkeypatches env vars and clears the cache;
        # mock_pubsub patches google.cloud.pubsub_v1.PublisherClient.
        self.mock_pubsub = mock_pubsub

    @pytest.mark.asyncio
    async def test_cli_request_produces_valid_task_on_pubsub(
        self,
        cli_task_data: dict,
        assertions: IntegrationAssertions,
    ):
        """POST /api/v1/tasks → a message with correct HenchmenTask shape on task-intake."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await _post(client, "/api/v1/tasks", cli_task_data)

        assert resp["status"] == "dispatched"
        self.mock_pubsub.assert_published_to(TASK_INTAKE_TOPIC, count=1)
        task_data = _get_first_message(self.mock_pubsub)
        assertions.assert_valid_henchmen_task(task_data)

    @pytest.mark.asyncio
    async def test_cli_task_has_correct_source(self, cli_task_data: dict):
        """Source field must be 'cli'."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _post(client, "/api/v1/tasks", cli_task_data)

        task_data = _get_first_message(self.mock_pubsub)
        assert task_data["source"] == "cli"

    @pytest.mark.asyncio
    async def test_cli_task_preserves_title_and_description(self, cli_task_data: dict):
        """Title and description from the request must be preserved in the published task."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _post(client, "/api/v1/tasks", cli_task_data)

        task_data = _get_first_message(self.mock_pubsub)
        assert task_data["title"] == cli_task_data["title"]
        assert task_data["description"] == cli_task_data["description"]

    @pytest.mark.asyncio
    async def test_cli_task_preserves_repo_and_branch(self, cli_task_data: dict):
        """context.repo must equal 'acme-org/sample-repo'."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _post(client, "/api/v1/tasks", cli_task_data)

        task_data = _get_first_message(self.mock_pubsub)
        assert task_data["context"]["repo"] == "acme-org/sample-repo"
        assert task_data["context"]["branch"] == cli_task_data["branch"]

    @pytest.mark.asyncio
    async def test_cli_task_priority_mapping(self):
        """Each CLI priority string must survive the round-trip correctly."""
        priority_map = {
            "critical": "critical",
            "high": "high",
            "normal": "normal",
            "low": "low",
        }
        for input_priority, expected_priority in priority_map.items():
            self.mock_pubsub.published_messages.clear()
            payload = {
                "title": f"Task with {input_priority} priority",
                "description": "Test priority mapping",
                "repo": "acme-org/sample-repo",
                "priority": input_priority,
                "created_by": "tester",
            }
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await _post(client, "/api/v1/tasks", payload)

            task_data = _get_first_message(self.mock_pubsub)
            assert task_data["priority"] == expected_priority, (
                f"Priority {input_priority!r} should map to {expected_priority!r}, got {task_data['priority']!r}"
            )


# ---------------------------------------------------------------------------
# TestSlackDispatchPipeline
# ---------------------------------------------------------------------------


class TestSlackDispatchPipeline:
    """Tests the Slack webhook → Dispatch → Pub/Sub flow."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_pubsub):
        self.mock_pubsub = mock_pubsub

    @pytest.mark.asyncio
    async def test_slack_mention_produces_task_on_pubsub(
        self,
        slack_event_data: dict,
        assertions: IntegrationAssertions,
    ):
        """POST /webhooks/slack with app_mention → message on task-intake."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await _post(client, "/webhooks/slack", slack_event_data)

        assert resp["status"] == "dispatched"
        self.mock_pubsub.assert_published_to(TASK_INTAKE_TOPIC, count=1)
        task_data = _get_first_message(self.mock_pubsub)
        assertions.assert_valid_henchmen_task(task_data)

    @pytest.mark.asyncio
    async def test_slack_task_has_correct_source(self, slack_event_data: dict):
        """Source must be 'slack'."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _post(client, "/webhooks/slack", slack_event_data)

        task_data = _get_first_message(self.mock_pubsub)
        assert task_data["source"] == "slack"

    @pytest.mark.asyncio
    async def test_slack_task_captures_thread_context(self, slack_event_data: dict):
        """context.thread_messages must be populated from the event text."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _post(client, "/webhooks/slack", slack_event_data)

        task_data = _get_first_message(self.mock_pubsub)
        thread_messages = task_data["context"].get("thread_messages") or []
        assert len(thread_messages) > 0, "Expected thread_messages to be populated"
        # The event text should appear in thread_messages
        assert any("fix the login bug" in msg for msg in thread_messages), (
            f"Event text not found in thread_messages: {thread_messages}"
        )

    @pytest.mark.asyncio
    async def test_slack_task_strips_bot_mention_from_title(self):
        """The @henchmen mention must be stripped from the task title."""
        # Use an event where the mention is the literal "@henchmen" string that the
        # normalizer is designed to strip (not an opaque Slack user-ID like <@B0123456>).
        payload = {
            "type": "app_mention",
            "user": "U0123456",
            "text": "@henchmen fix the login bug in auth.py",
            "channel": "C0123456",
            "ts": "1700000000.000001",
            "thread_ts": "1700000000.000001",
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _post(client, "/webhooks/slack", payload)

        task_data = _get_first_message(self.mock_pubsub)
        # "@henchmen" should be stripped; the remaining task description should be present
        assert "@henchmen" not in task_data["title"], (
            f"@henchmen mention not stripped from title: {task_data['title']!r}"
        )
        assert "fix the login bug" in task_data["title"], (
            f"Task content missing from title after stripping: {task_data['title']!r}"
        )

    @pytest.mark.asyncio
    async def test_slack_non_mention_event_ignored(self):
        """A non-app_mention event with no @henchmen text returns 200 but publishes nothing."""
        payload = {
            "type": "message",
            "user": "U9999999",
            "text": "Just chatting, nothing to see here",
            "channel": "C0123456",
            "ts": "1700000001.000001",
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await _post(client, "/webhooks/slack", payload)

        assert resp["status"] == "ignored"
        assert len(self.mock_pubsub.get_messages_for_topic(TASK_INTAKE_TOPIC)) == 0


# ---------------------------------------------------------------------------
# TestGitHubDispatchPipeline
# ---------------------------------------------------------------------------


class TestGitHubDispatchPipeline:
    """Tests the GitHub webhook → Dispatch → Pub/Sub flow."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_pubsub):
        self.mock_pubsub = mock_pubsub

    @pytest.mark.asyncio
    async def test_github_issue_labeled_produces_task(
        self,
        github_issue_event: dict,
        assertions: IntegrationAssertions,
    ):
        """Issue labeled 'henchmen' → task on task-intake topic."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await _post(client, "/webhooks/github", github_issue_event)

        assert resp["status"] == "dispatched"
        self.mock_pubsub.assert_published_to(TASK_INTAKE_TOPIC, count=1)
        task_data = _get_first_message(self.mock_pubsub)
        assertions.assert_valid_henchmen_task(task_data)

    @pytest.mark.asyncio
    async def test_github_issue_task_has_correct_source(self, github_issue_event: dict):
        """Source must be 'github'."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _post(client, "/webhooks/github", github_issue_event)

        task_data = _get_first_message(self.mock_pubsub)
        assert task_data["source"] == "github"

    @pytest.mark.asyncio
    async def test_github_issue_task_captures_repo(self, github_issue_event: dict):
        """context.repo must be 'acme-org/sample-repo'."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _post(client, "/webhooks/github", github_issue_event)

        task_data = _get_first_message(self.mock_pubsub)
        assert task_data["context"]["repo"] == "acme-org/sample-repo"

    @pytest.mark.asyncio
    async def test_github_pr_comment_produces_task(
        self,
        github_pr_comment_event: dict,
        assertions: IntegrationAssertions,
    ):
        """PR comment containing '@henchmen' → task on task-intake topic."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await _post(client, "/webhooks/github", github_pr_comment_event)

        assert resp["status"] == "dispatched"
        self.mock_pubsub.assert_published_to(TASK_INTAKE_TOPIC, count=1)
        task_data = _get_first_message(self.mock_pubsub)
        assertions.assert_valid_henchmen_task(task_data)

    @pytest.mark.asyncio
    async def test_github_pr_comment_captures_branch(self, github_pr_comment_event: dict):
        """context.branch must equal the PR head branch 'feature/auth-update'."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _post(client, "/webhooks/github", github_pr_comment_event)

        task_data = _get_first_message(self.mock_pubsub)
        assert task_data["context"]["branch"] == "feature/auth-update"

    @pytest.mark.asyncio
    async def test_github_unrelated_event_ignored(self):
        """An event without the henchmen label or @henchmen mention publishes nothing."""
        payload = {
            "action": "opened",
            "issue": {
                "number": 1,
                "title": "Regular issue",
                "body": "Nothing special here",
                "user": {"login": "someone"},
                "labels": [{"name": "bug"}],
                "state": "open",
            },
            "repository": {
                "full_name": "acme-org/sample-repo",
                "default_branch": "main",
            },
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await _post(client, "/webhooks/github", payload)

        assert resp["status"] == "ignored"
        assert len(self.mock_pubsub.get_messages_for_topic(TASK_INTAKE_TOPIC)) == 0


# ---------------------------------------------------------------------------
# TestJiraDispatchPipeline
# ---------------------------------------------------------------------------


class TestJiraDispatchPipeline:
    """Tests the Jira webhook → Dispatch → Pub/Sub flow."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_pubsub):
        self.mock_pubsub = mock_pubsub

    @pytest.mark.asyncio
    async def test_jira_transition_produces_task(
        self,
        jira_webhook_data: dict,
        assertions: IntegrationAssertions,
    ):
        """Issue transitioned to 'Ready for Henchmen' → task on task-intake topic."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await _post(client, "/webhooks/jira", jira_webhook_data)

        assert resp["status"] == "dispatched"
        self.mock_pubsub.assert_published_to(TASK_INTAKE_TOPIC, count=1)
        task_data = _get_first_message(self.mock_pubsub)
        assertions.assert_valid_henchmen_task(task_data)

    @pytest.mark.asyncio
    async def test_jira_task_has_correct_source(self, jira_webhook_data: dict):
        """Source must be 'jira'."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await _post(client, "/webhooks/jira", jira_webhook_data)

        task_data = _get_first_message(self.mock_pubsub)
        assert task_data["source"] == "jira"

    @pytest.mark.asyncio
    async def test_jira_task_priority_mapping(self):
        """Jira 'Major' → 'high', 'Blocker' → 'critical'."""
        priority_cases = [
            ("Major", "high"),
            ("Blocker", "critical"),
        ]
        for jira_priority, expected in priority_cases:
            self.mock_pubsub.published_messages.clear()
            payload = {
                "webhookEvent": "jira:issue_updated",
                "issue": {
                    "key": "PROJ-456",
                    "fields": {
                        "summary": f"Issue with {jira_priority} priority",
                        "description": "Priority test",
                        "priority": {"name": jira_priority},
                        "assignee": {"emailAddress": "dev@acme.com"},
                        "project": {"key": "PROJ"},
                    },
                },
                "changelog": {"items": [{"field": "status", "toString": "Ready for Henchmen"}]},
                "transition": {"transitionName": "Ready for Henchmen"},
            }
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                await _post(client, "/webhooks/jira", payload)

            task_data = _get_first_message(self.mock_pubsub)
            assert task_data["priority"] == expected, (
                f"Jira priority {jira_priority!r} should map to {expected!r}, got {task_data['priority']!r}"
            )

    @pytest.mark.asyncio
    async def test_jira_wrong_transition_ignored(self):
        """A transition to a status other than 'Ready for Henchmen' publishes nothing."""
        payload = {
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": "PROJ-789",
                "fields": {
                    "summary": "Some issue",
                    "description": "Just moved to In Progress",
                    "priority": {"name": "Normal"},
                    "assignee": {"emailAddress": "dev@acme.com"},
                    "project": {"key": "PROJ"},
                },
            },
            "changelog": {"items": [{"field": "status", "toString": "In Progress"}]},
            "transition": {"transitionName": "In Progress"},
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await _post(client, "/webhooks/jira", payload)

        assert resp["status"] == "ignored"
        assert len(self.mock_pubsub.get_messages_for_topic(TASK_INTAKE_TOPIC)) == 0


# ---------------------------------------------------------------------------
# TestDispatchNormalizerIntegration
# ---------------------------------------------------------------------------


class TestDispatchNormalizerIntegration:
    """Tests the normalizer directly: from_<source>() + publish_task()."""

    @pytest.fixture(autouse=True)
    def _setup(self, integration_settings, mock_pubsub):
        self.mock_pubsub = mock_pubsub
        self.normalizer = TaskNormalizer()
        self.settings = get_settings()

    @pytest.mark.asyncio
    async def test_normalize_and_publish_cli(
        self,
        cli_task_data: dict,
        assertions: IntegrationAssertions,
    ):
        """from_cli + publish_task writes a valid HenchmenTask to Pub/Sub."""
        task = self.normalizer.from_cli(cli_task_data)
        await self.normalizer.publish_task(task, self.settings)

        self.mock_pubsub.assert_published_to(TASK_INTAKE_TOPIC, count=1)
        task_data = _get_first_message(self.mock_pubsub)
        assertions.assert_valid_henchmen_task(task_data)
        assert task_data["source"] == "cli"

    @pytest.mark.asyncio
    async def test_normalize_and_publish_slack(
        self,
        slack_event_data: dict,
        assertions: IntegrationAssertions,
    ):
        """from_slack + publish_task writes a valid HenchmenTask to Pub/Sub."""
        task = self.normalizer.from_slack(slack_event_data)
        await self.normalizer.publish_task(task, self.settings)

        self.mock_pubsub.assert_published_to(TASK_INTAKE_TOPIC, count=1)
        task_data = _get_first_message(self.mock_pubsub)
        assertions.assert_valid_henchmen_task(task_data)
        assert task_data["source"] == "slack"

    @pytest.mark.asyncio
    async def test_normalize_and_publish_github_issue(
        self,
        github_issue_event: dict,
        assertions: IntegrationAssertions,
    ):
        """from_github (issue) + publish_task writes a valid HenchmenTask to Pub/Sub."""
        task = self.normalizer.from_github(github_issue_event)
        await self.normalizer.publish_task(task, self.settings)

        self.mock_pubsub.assert_published_to(TASK_INTAKE_TOPIC, count=1)
        task_data = _get_first_message(self.mock_pubsub)
        assertions.assert_valid_henchmen_task(task_data)
        assert task_data["source"] == "github"
        assert task_data["context"]["repo"] == "acme-org/sample-repo"

    @pytest.mark.asyncio
    async def test_normalize_and_publish_github_pr_comment(
        self,
        github_pr_comment_event: dict,
        assertions: IntegrationAssertions,
    ):
        """from_github (PR comment) + publish_task writes a valid HenchmenTask to Pub/Sub."""
        task = self.normalizer.from_github(github_pr_comment_event)
        await self.normalizer.publish_task(task, self.settings)

        self.mock_pubsub.assert_published_to(TASK_INTAKE_TOPIC, count=1)
        task_data = _get_first_message(self.mock_pubsub)
        assertions.assert_valid_henchmen_task(task_data)
        assert task_data["source"] == "github"
        assert task_data["context"]["branch"] == "feature/auth-update"

    @pytest.mark.asyncio
    async def test_normalize_and_publish_jira(
        self,
        jira_webhook_data: dict,
        assertions: IntegrationAssertions,
    ):
        """from_jira + publish_task writes a valid HenchmenTask to Pub/Sub."""
        task = self.normalizer.from_jira(jira_webhook_data)
        await self.normalizer.publish_task(task, self.settings)

        self.mock_pubsub.assert_published_to(TASK_INTAKE_TOPIC, count=1)
        task_data = _get_first_message(self.mock_pubsub)
        assertions.assert_valid_henchmen_task(task_data)
        assert task_data["source"] == "jira"

    @pytest.mark.asyncio
    async def test_all_sources_produce_unique_task_ids(
        self,
        cli_task_data: dict,
        slack_event_data: dict,
        github_issue_event: dict,
        jira_webhook_data: dict,
    ):
        """All four sources produce tasks with different unique IDs."""
        cli_task = self.normalizer.from_cli(cli_task_data)
        slack_task = self.normalizer.from_slack(slack_event_data)
        github_task = self.normalizer.from_github(github_issue_event)
        jira_task = self.normalizer.from_jira(jira_webhook_data)

        ids = {cli_task.id, slack_task.id, github_task.id, jira_task.id}
        assert len(ids) == 4, f"Expected 4 unique task IDs, got {len(ids)}: {ids}"
