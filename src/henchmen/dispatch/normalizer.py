"""Normalizes task inputs from various sources to HenchmenTask.

Dispatch is intake-only: this module turns a source-specific payload into the
shared :class:`~henchmen.models.task.HenchmenTask` contract and publishes it.
No business logic lives here.
"""

import re
from typing import Any
from uuid import uuid4

from henchmen.config.settings import Settings
from henchmen.models.task import HenchmenTask, TaskContext, TaskPriority, TaskSource
from henchmen.providers.interfaces.message_broker import MessageBroker

# Slack renders a user/bot mention as ``<@U0123ABC>`` (optionally ``<@U0123ABC|name>``).
# The literal string ``<@henchmen>`` is never sent by Slack; it only appears in
# older fixtures and hand-written docs, so it is stripped separately.
_SLACK_MENTION_RE = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")
_PLAIN_MENTION_MARKERS = ("<@henchmen>", "@henchmen")

# Jira Cloud keys custom fields as ``customfield_<numeric id>``; a literal
# ``customfield_repo`` key cannot exist. Accept the plain names an automation
# rule can set instead, and fall back to the configured default repo.
_JIRA_REPO_KEYS = ("repo", "customfield_repo")
_JIRA_BRANCH_KEYS = ("branch", "customfield_branch")


def strip_slack_mentions(text: str) -> str:
    """Remove Slack mention markup (``<@U0123ABC>``) and plain ``@henchmen`` markers."""
    cleaned = _SLACK_MENTION_RE.sub("", text)
    for marker in _PLAIN_MENTION_MARKERS:
        cleaned = cleaned.replace(marker, "")
    return cleaned.strip()


def _resolve_repo(repo: str, settings: Settings | None) -> str:
    """Return *repo* or, when empty, the configured default target repo."""
    if repo:
        return repo
    return settings.github_default_repo if settings is not None else ""


def _first_str(source: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Return the first non-empty string value among *keys* in *source*."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class TaskNormalizer:
    """Normalizes task inputs from various sources to HenchmenTask."""

    def from_cli(self, data: dict[str, Any], settings: Settings | None = None) -> HenchmenTask:
        """Normalize CLI REST API request."""
        return HenchmenTask(
            source=TaskSource.CLI,
            source_id=data.get("id") or str(uuid4()),
            title=data["title"],
            description=data.get("description", ""),
            context=TaskContext(
                repo=_resolve_repo(data.get("repo", "") or "", settings),
                branch=data.get("branch"),
            ),
            priority=TaskPriority(data.get("priority", "normal")),
            created_by=data.get("created_by", "cli"),
        )

    def from_slack(self, event: dict[str, Any], settings: Settings | None = None) -> HenchmenTask:
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
        # The Socket Mode bot pre-fetches ``conversations.replies`` and puts the
        # raw message texts on the inner event as ``thread_messages``.
        for fetched in slack_event.get("thread_messages", []) or []:
            if isinstance(fetched, str) and fetched and fetched not in thread_messages:
                thread_messages.append(fetched)

        # Build a human-readable source_id: channel/thread_ts
        source_id = f"{channel}/{thread_ts}" if channel and thread_ts else str(uuid4())

        title = strip_slack_mentions(text)
        if not title:
            title = "Slack task"

        return HenchmenTask(
            source=TaskSource.SLACK,
            source_id=source_id,
            title=title[:200],
            description=text,
            context=TaskContext(
                repo=_resolve_repo(event.get("repo", "") or "", settings),
                branch=event.get("branch"),
                thread_messages=thread_messages,
            ),
            priority=TaskPriority.NORMAL,
            created_by=user,
        )

    def from_github(self, payload: dict[str, Any], settings: Settings | None = None) -> HenchmenTask:
        """Normalize GitHub webhook (issue/PR, labels, diff)."""
        action = payload.get("action", "")
        repo = payload.get("repository", {}).get("full_name", "")
        comment = payload.get("comment") or {}

        # ``issue_comment`` on a pull request: the payload carries ``issue``
        # (with an ``issue.pull_request`` marker) and ``comment``, but no
        # top-level ``pull_request`` object.
        if "issue" in payload and "pull_request" not in payload and comment:
            issue = payload["issue"]
            number = issue.get("number", "")
            is_pr = bool(issue.get("pull_request"))
            source_id = f"{'pr' if is_pr else 'issue'}-{number or uuid4()}"
            title = issue.get("title", "GitHub comment task")
            description = comment.get("body", "") or ""
            created_by = (comment.get("user") or issue.get("user") or {}).get("login", "github")
            branch = payload.get("repository", {}).get("default_branch", "main")
            labels = [lbl.get("name") for lbl in issue.get("labels", [])]
            context = TaskContext(
                repo=repo,
                branch=branch,
                issue_fields={
                    "number": str(number),
                    "labels": ",".join(labels),
                    "state": issue.get("state", ""),
                    "action": action,
                    "is_pull_request": str(is_pr).lower(),
                },
            )
        # Handle issue labeled 'henchmen'
        elif "issue" in payload and "pull_request" not in payload:
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

        if not context.repo:
            context = context.model_copy(update={"repo": _resolve_repo("", settings)})

        return HenchmenTask(
            source=TaskSource.GITHUB,
            source_id=source_id,
            title=title[:200],
            description=description,
            context=context,
            priority=TaskPriority.NORMAL,
            created_by=created_by,
        )

    def from_jira(self, payload: dict[str, Any], settings: Settings | None = None) -> HenchmenTask:
        """Normalize Jira webhook (issue fields, transitions)."""
        issue = payload.get("issue", {})
        fields = issue.get("fields", {})
        issue_key = issue.get("key", str(uuid4()))
        transition = payload.get("transition", {})

        title = fields.get("summary") or f"Jira issue {issue_key}"
        description = fields.get("description") or ""
        created_by = (fields.get("assignee") or fields.get("reporter") or {}).get("emailAddress", "jira")
        repo = _first_str(fields, _JIRA_REPO_KEYS) or _first_str(payload, _JIRA_REPO_KEYS)
        branch = _first_str(fields, _JIRA_BRANCH_KEYS) or _first_str(payload, _JIRA_BRANCH_KEYS) or None

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
            context=TaskContext(
                repo=_resolve_repo(repo, settings),
                branch=branch,
                issue_fields=issue_fields,
            ),
            priority=priority,
            created_by=created_by,
        )

    async def publish_task(
        self,
        task: HenchmenTask,
        settings: Settings,
        broker: MessageBroker | None = None,
        dedup_key: str | None = None,
    ) -> str:
        """Publish normalized task to Pub/Sub task-intake topic. Returns message ID.

        When *dedup_key* is supplied (a stable id derived from the source
        delivery, e.g. ``github:<X-GitHub-Delivery>``), it is attached as a
        message attribute so Mastermind's application-level dedup rejects
        replays even though each redelivery produces a fresh task id.
        """
        if broker is None:
            from henchmen.providers.registry import ProviderRegistry

            broker = ProviderRegistry(settings).get_message_broker()
        data = task.model_dump_json().encode("utf-8")
        attributes: dict[str, str] = {"task_id": task.id}
        if dedup_key:
            attributes["dedup_key"] = dedup_key
        return await broker.publish(settings.pubsub_topic_task_intake, data, **attributes)
