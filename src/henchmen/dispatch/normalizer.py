"""Normalizes task inputs from various sources to HenchmenTask."""

from typing import Any
from uuid import uuid4

from henchmen.config.settings import Settings
from henchmen.models.task import HenchmenTask, TaskContext, TaskPriority, TaskSource
from henchmen.providers.interfaces.message_broker import MessageBroker


class TaskNormalizer:
    """Normalizes task inputs from various sources to HenchmenTask."""

    def from_cli(self, data: dict[str, Any]) -> HenchmenTask:
        """Normalize CLI REST API request."""
        return HenchmenTask(
            source=TaskSource.CLI,
            source_id=data.get("id", str(uuid4())),
            title=data["title"],
            description=data.get("description", ""),
            context=TaskContext(repo=data.get("repo", ""), branch=data.get("branch")),
            priority=TaskPriority(data.get("priority", "normal")),
            created_by=data.get("created_by", "cli"),
        )

    def from_slack(self, event: dict[str, Any]) -> HenchmenTask:
        """Normalize Slack event (thread messages, user, channel)."""
        # Extract relevant fields from Slack event payload
        slack_event = event.get("event", event)
        user = slack_event.get("user", "unknown")
        channel = slack_event.get("channel", "")
        text = slack_event.get("text", "")
        thread_ts = slack_event.get("thread_ts", slack_event.get("ts", ""))

        # Collect thread messages if present
        thread_messages: list[str] = []
        if text:
            thread_messages.append(text)
        for msg in event.get("messages", []):
            msg_text = msg.get("text", "")
            if msg_text and msg_text not in thread_messages:
                thread_messages.append(msg_text)

        # Build a human-readable source_id: channel/thread_ts
        source_id = f"{channel}/{thread_ts}" if channel and thread_ts else str(uuid4())

        # Strip @henchmen mention from title
        title = text.replace("<@henchmen>", "").replace("@henchmen", "").strip()
        if not title:
            title = "Slack task"

        return HenchmenTask(
            source=TaskSource.SLACK,
            source_id=source_id,
            title=title[:200],
            description=text,
            context=TaskContext(
                repo=event.get("repo", ""),
                branch=event.get("branch"),
                thread_messages=thread_messages,
            ),
            priority=TaskPriority.NORMAL,
            created_by=user,
        )

    def from_github(self, payload: dict[str, Any]) -> HenchmenTask:
        """Normalize GitHub webhook (issue/PR, labels, diff)."""
        action = payload.get("action", "")
        repo = payload.get("repository", {}).get("full_name", "")

        # Handle issue labeled 'henchmen'
        if "issue" in payload and "pull_request" not in payload:
            issue = payload["issue"]
            source_id = f"issue-{issue.get('number', str(uuid4()))}"
            title = issue.get("title", "GitHub issue task")
            description = issue.get("body", "") or ""
            created_by = (issue.get("user") or {}).get("login", "github")
            branch = payload.get("repository", {}).get("default_branch", "main")
            labels = [lbl.get("name") for lbl in issue.get("labels", [])]
            issue_fields = {
                "number": str(issue.get("number", "")),
                "labels": ",".join(labels),
                "state": issue.get("state", ""),
                "action": action,
            }
            context = TaskContext(
                repo=repo,
                branch=branch,
                issue_fields=issue_fields,
            )
        # Handle PR review comment mentioning @henchmen, or PR event
        elif "pull_request" in payload:
            pr = payload["pull_request"]
            comment = payload.get("comment", {})
            source_id = f"pr-{pr.get('number', str(uuid4()))}"
            title = pr.get("title", "GitHub PR task")
            description = comment.get("body", pr.get("body", "") or "")
            created_by = (comment.get("user") or pr.get("user") or {}).get("login", "github")
            branch = pr.get("head", {}).get("ref", "")
            pr_diff = payload.get("diff", "")
            pr_labels = [lbl.get("name") for lbl in pr.get("labels", [])]
            issue_fields = {
                "number": str(pr.get("number", "")),
                "labels": ",".join(pr_labels),
                "state": pr.get("state", ""),
                "action": action,
            }
            context = TaskContext(
                repo=repo,
                branch=branch,
                pr_diff=pr_diff,
                issue_fields=issue_fields,
            )
        else:
            # Generic fallback
            source_id = str(uuid4())
            title = f"GitHub event: {action}"
            description = ""
            created_by = "github"
            context = TaskContext(repo=repo, branch="main")

        return HenchmenTask(
            source=TaskSource.GITHUB,
            source_id=source_id,
            title=title[:200],
            description=description,
            context=context,
            priority=TaskPriority.NORMAL,
            created_by=created_by,
        )

    def from_jira(self, payload: dict[str, Any]) -> HenchmenTask:
        """Normalize Jira webhook (issue fields, transitions)."""
        issue = payload.get("issue", {})
        fields = issue.get("fields", {})
        issue_key = issue.get("key", str(uuid4()))
        transition = payload.get("transition", {})

        title = fields.get("summary") or f"Jira issue {issue_key}"
        description = fields.get("description") or ""
        created_by = (fields.get("assignee") or fields.get("reporter") or {}).get("emailAddress", "jira")
        repo = fields.get("customfield_repo", "")
        branch = fields.get("customfield_branch")

        issue_fields = {
            "key": issue_key,
            "status": (fields.get("status") or {}).get("name", ""),
            "transition": transition.get("transitionName", ""),
            "priority": (fields.get("priority") or {}).get("name", "normal"),
            "labels": ",".join(fields.get("labels", [])),
        }

        # Map Jira priority to HenchmenTask priority
        jira_priority = (fields.get("priority") or {}).get("name", "normal").lower()
        priority_map = {
            "blocker": TaskPriority.CRITICAL,
            "critical": TaskPriority.CRITICAL,
            "major": TaskPriority.HIGH,
            "high": TaskPriority.HIGH,
            "normal": TaskPriority.NORMAL,
            "medium": TaskPriority.NORMAL,
            "minor": TaskPriority.LOW,
            "low": TaskPriority.LOW,
            "trivial": TaskPriority.LOW,
        }
        priority = priority_map.get(jira_priority, TaskPriority.NORMAL)

        return HenchmenTask(
            source=TaskSource.JIRA,
            source_id=issue_key,
            title=title[:200],
            description=description,
            context=TaskContext(repo=repo, branch=branch, issue_fields=issue_fields),
            priority=priority,
            created_by=created_by,
        )

    async def publish_task(self, task: HenchmenTask, settings: Settings, broker: MessageBroker | None = None) -> str:
        """Publish normalized task to Pub/Sub task-intake topic. Returns message ID."""
        if broker is None:
            from henchmen.providers.registry import ProviderRegistry

            broker = ProviderRegistry(settings).get_message_broker()
        data = task.model_dump_json().encode("utf-8")
        return await broker.publish(settings.pubsub_topic_task_intake, data, task_id=task.id)
