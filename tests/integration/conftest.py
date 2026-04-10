"""Integration test configuration and shared fixtures.

Provides mock GCP services, test workspaces, and assertion helpers
so integration tests can run locally without any real GCP resources.

.. note::
   The integration suite installs in-memory mocks for several
   ``google.cloud.*`` submodules via :func:`unittest.mock.patch` with a
   string target path. That patch form requires the underlying module to be
   importable — if ``google-cloud-pubsub``/``firestore``/``storage`` are not
   installed, the whole suite fails at fixture setup with a cryptic
   ``AttributeError``. To give contributors a clear signal, each required
   SDK is probed via :func:`pytest.importorskip` at module import time. When
   running a minimal ``[dev]`` install without ``[dev-integration]``, the
   suite is cleanly skipped instead of crashing. This is the R1 fix from the
   2026-04-09 expert panel review.
"""

import json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# R1: probe GCP SDK availability at collection time so the integration suite
# is skipped cleanly under a bare [dev] install instead of failing with a
# cryptic AttributeError inside a monkeypatch.setattr call.
pytest.importorskip(
    "google.cloud.pubsub_v1",
    reason="integration suite requires the [dev-integration] extras — run `pip install -e .[dev-integration]`",
)
pytest.importorskip(
    "google.cloud.firestore",
    reason="integration suite requires the [dev-integration] extras — run `pip install -e .[dev-integration]`",
)
pytest.importorskip(
    "google.cloud.storage",
    reason="integration suite requires the [dev-integration] extras — run `pip install -e .[dev-integration]`",
)

from henchmen.config.settings import Settings, get_settings  # noqa: E402 — importorskip must run first


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-tag every test in this directory with the `integration` marker.

    This lets contributors run only the fast unit loop via
    ``pytest -m 'not integration'`` and keeps new integration files automatically
    covered without requiring every author to remember to add the marker.
    """
    integration_root = Path(__file__).parent.resolve()
    marker = pytest.mark.integration
    for item in items:
        try:
            item_path = Path(str(item.fspath)).resolve()
        except Exception:
            continue
        try:
            item_path.relative_to(integration_root)
        except ValueError:
            continue
        item.add_marker(marker)


# ---------------------------------------------------------------------------
# MockPubSub
# ---------------------------------------------------------------------------


class MockFuture:
    def result(self, timeout=None):
        return "mock-message-id"


class MockPubSubPublisher:
    """Captures Pub/Sub publish calls for assertion."""

    def __init__(self):
        self.published_messages: list[dict] = []  # {topic, data, attributes}

    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic: str, data: bytes, **attrs) -> MockFuture:
        self.published_messages.append(
            {
                "topic": topic,
                "data": json.loads(data.decode("utf-8")) if data else None,
                "attributes": attrs,
            }
        )
        return MockFuture()

    def get_messages_for_topic(self, topic_name: str) -> list[dict]:
        """Get all messages published to a topic (matches partial topic name)."""
        return [m for m in self.published_messages if topic_name in m["topic"]]

    def assert_published_to(self, topic_name: str, count: int = 1):
        """Assert that exactly `count` messages were published to the given topic."""
        msgs = self.get_messages_for_topic(topic_name)
        assert len(msgs) == count, f"Expected {count} messages on {topic_name}, got {len(msgs)}"


# ---------------------------------------------------------------------------
# MockFirestore
# ---------------------------------------------------------------------------


class MockDocSnapshot:
    def __init__(self, data: dict | None, doc_id: str, exists: bool = True):
        self._data = data
        self._doc_id = doc_id
        self._exists = exists

    @property
    def id(self) -> str:
        return self._doc_id

    @property
    def exists(self) -> bool:
        return self._exists

    def to_dict(self) -> dict | None:
        return self._data


class MockDocument:
    def __init__(self, collection_data: dict, doc_id: str):
        self._data = collection_data
        self._doc_id = doc_id

    @property
    def id(self) -> str:
        return self._doc_id

    def set(self, data: dict) -> None:
        self._data[self._doc_id] = dict(data)

    def get(self) -> MockDocSnapshot:
        if self._doc_id in self._data:
            return MockDocSnapshot(dict(self._data[self._doc_id]), self._doc_id, exists=True)
        return MockDocSnapshot(None, self._doc_id, exists=False)

    def update(self, data: dict) -> None:
        if self._doc_id not in self._data:
            self._data[self._doc_id] = {}
        self._data[self._doc_id].update(data)

    def delete(self) -> None:
        self._data.pop(self._doc_id, None)


def _apply_filter(doc_data: dict, field: str, op: str, value) -> bool:
    """Evaluate a single Firestore where-clause against a document dict."""
    actual = doc_data.get(field)
    if op in ("==", "="):
        return actual == value
    if op == "!=":
        return actual != value
    if op == "<":
        return actual is not None and actual < value
    if op == "<=":
        return actual is not None and actual <= value
    if op == ">":
        return actual is not None and actual > value
    if op == ">=":
        return actual is not None and actual >= value
    if op == "in":
        return actual in value
    if op == "not-in":
        return actual not in value
    if op == "array-contains":
        return isinstance(actual, list) and value in actual
    return False


class MockCollection:
    def __init__(self, data: dict, name: str):
        self._data = data
        self._name = name
        self._filters: list[tuple] = []
        self._order_by_field: str | None = None
        self._order_direction: str | None = None
        self._limit_val: int | None = None

    def _clone(self) -> "MockCollection":
        clone = MockCollection(self._data, self._name)
        clone._filters = list(self._filters)
        clone._order_by_field = self._order_by_field
        clone._order_direction = self._order_direction
        clone._limit_val = self._limit_val
        return clone

    def document(self, doc_id: str | None = None) -> MockDocument:
        if doc_id is None:
            doc_id = str(uuid.uuid4())
        return MockDocument(self._data, doc_id)

    def add(self, data: dict) -> tuple:
        doc_id = str(uuid.uuid4())
        self._data[doc_id] = dict(data)
        return (None, MockDocument(self._data, doc_id))

    def where(self, field: str, op: str, value) -> "MockCollection":
        clone = self._clone()
        clone._filters.append((field, op, value))
        return clone

    def order_by(self, field: str, direction=None) -> "MockCollection":
        clone = self._clone()
        clone._order_by_field = field
        clone._order_direction = direction
        return clone

    def limit(self, count: int) -> "MockCollection":
        clone = self._clone()
        clone._limit_val = count
        return clone

    def _filtered_docs(self) -> list[MockDocSnapshot]:
        results: list[MockDocSnapshot] = []
        for doc_id, doc_data in self._data.items():
            if all(_apply_filter(doc_data, f, op, v) for f, op, v in self._filters):
                results.append(MockDocSnapshot(dict(doc_data), doc_id, exists=True))
        if self._order_by_field:
            reverse = self._order_direction == "DESCENDING"
            results.sort(
                key=lambda s: (s.to_dict() or {}).get(self._order_by_field, ""),
                reverse=reverse,
            )
        if self._limit_val is not None:
            results = results[: self._limit_val]
        return results

    def get(self) -> list[MockDocSnapshot]:
        return self._filtered_docs()

    def stream(self) -> list[MockDocSnapshot]:
        return self._filtered_docs()


class MockFirestoreClient:
    """In-memory Firestore fake for integration tests."""

    def __init__(self):
        self._collections: dict[str, dict[str, dict]] = {}

    def collection(self, name: str) -> MockCollection:
        if name not in self._collections:
            self._collections[name] = {}
        return MockCollection(self._collections[name], name)


# ---------------------------------------------------------------------------
# MockGCS
# ---------------------------------------------------------------------------


class MockBlob:
    def __init__(self, bucket_data: dict[str, bytes], name: str):
        self._data = bucket_data
        self._name = name

    def exists(self) -> bool:
        return self._name in self._data

    def upload_from_string(self, data: str | bytes) -> None:
        if isinstance(data, str):
            self._data[self._name] = data.encode("utf-8")
        else:
            self._data[self._name] = data

    def download_as_text(self) -> str:
        if self._name not in self._data:
            raise FileNotFoundError(f"Blob {self._name!r} does not exist")
        raw = self._data[self._name]
        return raw.decode("utf-8") if isinstance(raw, bytes) else raw

    def upload_from_filename(self, filename: str) -> None:
        with open(filename, "rb") as fh:
            self._data[self._name] = fh.read()

    def download_to_filename(self, filename: str) -> None:
        if self._name not in self._data:
            raise FileNotFoundError(f"Blob {self._name!r} does not exist")
        with open(filename, "wb") as fh:
            raw = self._data[self._name]
            fh.write(raw if isinstance(raw, bytes) else raw.encode("utf-8"))


class MockBucket:
    def __init__(self, bucket_data: dict[str, bytes]):
        self._data = bucket_data

    def blob(self, name: str) -> MockBlob:
        return MockBlob(self._data, name)


class MockStorageClient:
    """In-memory GCS fake."""

    def __init__(self):
        self._buckets: dict[str, dict[str, bytes]] = {}

    def bucket(self, name: str) -> MockBucket:
        if name not in self._buckets:
            self._buckets[name] = {}
        return MockBucket(self._buckets[name])


# ---------------------------------------------------------------------------
# Fixtures: GCP client patches
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pubsub(monkeypatch) -> MockPubSubPublisher:
    """Patch google.cloud.pubsub_v1.PublisherClient with mock."""
    mock = MockPubSubPublisher()
    monkeypatch.setattr("google.cloud.pubsub_v1.PublisherClient", lambda: mock)
    return mock


@pytest.fixture
def mock_firestore(monkeypatch) -> MockFirestoreClient:
    """Patch google.cloud.firestore.Client with mock."""
    mock = MockFirestoreClient()
    monkeypatch.setattr("google.cloud.firestore.Client", lambda: mock)
    return mock


@pytest.fixture
def mock_gcs(monkeypatch) -> MockStorageClient:
    """Patch google.cloud.storage.Client with mock."""
    mock = MockStorageClient()
    monkeypatch.setattr("google.cloud.storage.Client", lambda: mock)
    return mock


# ---------------------------------------------------------------------------
# Fixtures: test workspace
# ---------------------------------------------------------------------------


@pytest.fixture
def test_workspace(tmp_path):
    """Create a temporary git workspace with a sample Python project.

    Also points the Arsenal workspace boundary at this directory so
    file_write / file_edit / git_commit accept paths inside it. The cache
    is cleared on teardown so subsequent tests start with the default.
    """
    from henchmen.arsenal import _workspace as _arsenal_workspace

    workspace = tmp_path / "test-repo"
    workspace.mkdir()

    subprocess.run(["git", "init"], cwd=workspace, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=workspace, capture_output=True)
    # Set per-workspace user.name/user.email so subsequent git commands made
    # by Arsenal tools (which inherit only os.environ) can commit even when
    # the host runner has no global git config (e.g. fresh GitHub Actions VM).
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=workspace,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=workspace,
        capture_output=True,
    )
    # Suppress GPG signing requests on hosts that have commit.gpgsign=true.
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=workspace,
        capture_output=True,
    )

    (workspace / "src").mkdir()
    (workspace / "src" / "auth.py").write_text(
        '''
def login(username: str, password: str) -> dict:
    """Authenticate user."""
    if not username or not password:
        raise ValueError("Missing credentials")
    return {"token": "abc123", "user": username}
'''
    )

    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_auth.py").write_text(
        """
from src.auth import login


def test_login_success():
    result = login("user", "pass")
    assert result["token"] == "abc123"


def test_login_missing_credentials():
    import pytest
    with pytest.raises(ValueError):
        login("", "")
"""
    )

    (workspace / "CLAUDE.md").write_text("# Rules\nFollow PEP 8.\nWrite tests for all changes.")
    (workspace / "pyproject.toml").write_text('[project]\nname = "test-repo"\n')

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
    }
    subprocess.run(["git", "add", "."], cwd=workspace, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=workspace,
        capture_output=True,
        env=git_env,
    )

    # Point the Arsenal workspace boundary at this temp dir so file-write
    # tools accept paths inside it. The fixture is function-scoped, so the
    # finalizer below resets the cache for the next test.
    _arsenal_workspace.set_workspace_root(workspace)
    yield workspace
    _arsenal_workspace.set_workspace_root(None)


# ---------------------------------------------------------------------------
# Fixtures: settings
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def dispatch_client(mock_pubsub) -> AsyncIterator[AsyncClient]:
    """Yield an ``httpx.AsyncClient`` wired to the dispatch FastAPI app.

    Replaces the hand-rolled ``async with AsyncClient(...)`` blocks that
    previously lived inside ``test_dispatch_pipeline.py``. Tests consume
    this fixture by adding ``dispatch_client`` to their signature and
    using it in place of the old ``client`` local variable.

    ``httpx.AsyncClient`` with ``ASGITransport`` does not trigger FastAPI
    lifespan hooks, so handlers that read ``app.state.message_broker``
    would otherwise fail with ``AttributeError``. We inject a minimal
    broker shim backed by the ``mock_pubsub`` publisher so tests get
    exactly the same observable behaviour (``published_messages``) as
    before without bringing up the real provider registry.
    """
    from henchmen.dispatch.server import app as dispatch_app

    class _MockBroker:
        """Minimal MessageBroker that delegates to mock_pubsub."""

        def __init__(self, publisher):
            self._publisher = publisher

        async def publish(self, topic: str, data: bytes, ordering_key=None, **attributes) -> str:
            # Route through the mock_pubsub publisher so assertions work.
            project = os.environ.get("HENCHMEN_GCP_PROJECT_ID", "test-project")
            topic_path = f"projects/{project}/topics/{topic}"
            future = self._publisher.publish(topic_path, data, **attributes)
            return str(future.result(timeout=10))

    dispatch_app.state.message_broker = _MockBroker(mock_pubsub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=dispatch_app),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        # Clear the state so later tests that DO exercise the full lifespan
        # (or that want a fresh broker) are not polluted.
        if hasattr(dispatch_app.state, "message_broker"):
            del dispatch_app.state.message_broker


@pytest.fixture
def integration_settings(monkeypatch) -> Settings:
    """Settings with test-appropriate defaults.

    The root ``_isolate_settings`` autouse fixture clears the
    ``get_settings`` ``lru_cache`` on both sides of every test, so this
    fixture only has to set env vars and return a freshly-constructed
    instance.
    """
    monkeypatch.setenv("HENCHMEN_GCP_PROJECT_ID", "test-project")
    monkeypatch.setenv("HENCHMEN_ENVIRONMENT", "dev")
    monkeypatch.setenv("HENCHMEN_GCP_REGION", "us-central1")
    # Clear once more so the setenv calls above are reflected in the returned
    # instance (autouse fixture already ran before monkeypatch was applied).
    get_settings.cache_clear()
    return get_settings()


# ---------------------------------------------------------------------------
# Fixtures: sample event / task data
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_task_data() -> dict:
    """Raw CLI request data."""
    return {
        "title": "Fix login bug",
        "description": "Login endpoint returns 500 for special chars in passwords",
        "repo": "acme-org/sample-repo",
        "branch": "main",
        "priority": "high",
        "created_by": "developer@acme.com",
    }


@pytest.fixture
def slack_event_data() -> dict:
    """Raw Slack app_mention event."""
    return {
        "type": "app_mention",
        "user": "U0123456",
        "text": "<@B0123456> fix the login bug in auth.py",
        "channel": "C0123456",
        "ts": "1700000000.000001",
        "thread_ts": "1700000000.000001",
        "thread_messages": ["User reported: login fails with special chars"],
    }


@pytest.fixture
def github_issue_event() -> dict:
    """Raw GitHub issue labeled event."""
    return {
        "action": "labeled",
        "label": {"name": "henchmen"},
        "issue": {
            "number": 42,
            "title": "Login fails with special characters",
            "body": "When a user has special characters in their password, login returns 500.",
            "labels": [{"name": "bug"}, {"name": "henchmen"}],
            "user": {"login": "dev-user"},
        },
        "repository": {
            "full_name": "acme-org/sample-repo",
            "default_branch": "main",
        },
    }


@pytest.fixture
def github_pr_comment_event() -> dict:
    """Raw GitHub PR review comment event."""
    return {
        "action": "created",
        "comment": {
            "body": "@henchmen fix this authentication issue",
            "user": {"login": "reviewer"},
        },
        "pull_request": {
            "number": 99,
            "title": "Update auth module",
            "head": {"ref": "feature/auth-update"},
            "base": {"ref": "main"},
            "diff_url": "https://github.com/acme-org/sample-repo/pull/99.diff",
        },
        "repository": {
            "full_name": "acme-org/sample-repo",
            "default_branch": "main",
        },
    }


@pytest.fixture
def jira_webhook_data() -> dict:
    """Raw Jira webhook payload."""
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "PROJ-123",
            "fields": {
                "summary": "Fix login endpoint 500 errors",
                "description": "Users with special chars in passwords get 500 errors",
                "priority": {"name": "Major"},
                "assignee": {"emailAddress": "dev@acme.com"},
                "project": {"key": "PROJ"},
            },
        },
        "changelog": {"items": [{"field": "status", "toString": "Ready for Henchmen"}]},
        "transition": {"transitionName": "Ready for Henchmen"},
    }


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


class IntegrationAssertions:
    """Helper methods for common integration test assertions."""

    @staticmethod
    def assert_valid_henchmen_task(task_data: dict) -> None:
        """Assert a dict represents a valid HenchmenTask."""
        required = [
            "id",
            "source",
            "source_id",
            "title",
            "description",
            "context",
            "priority",
            "status",
        ]
        for field in required:
            assert field in task_data, f"Missing required field: {field}"
        valid_statuses = {"pending", "dispatched", "in_progress", "completed", "failed", "escalated"}
        assert task_data["status"] in valid_statuses, f"Invalid status: {task_data['status']!r}"

    @staticmethod
    def assert_pubsub_message_contains(mock_pubsub: MockPubSubPublisher, topic: str, **expected_fields) -> None:
        """Assert a message was published to topic with expected fields in the data."""
        msgs = mock_pubsub.get_messages_for_topic(topic)
        assert len(msgs) > 0, f"No messages published to {topic}"
        for msg in msgs:
            data = msg["data"] or {}
            if all(data.get(k) == v for k, v in expected_fields.items()):
                return  # Found a match
        assert False, f"No message on {topic} matched {expected_fields}"  # noqa: B011

    @staticmethod
    def assert_execution_state_reached(exec_state: dict, expected_state: str) -> None:
        """Assert a task execution document reached the expected state.

        Replaces the former ``assert_state_machine_reached`` helper. After the
        2026-04-09 expert panel remediation (finding E1) the in-memory
        ``TaskStateMachine`` was deleted; lifecycle state now lives in
        Firestore ``task_executions/{task_id}`` documents managed by
        ``SchemeExecutor``.
        """
        assert exec_state.get("execution_state") == expected_state, (
            f"Expected execution_state {expected_state!r}, got {exec_state.get('execution_state')!r}"
        )


@pytest.fixture
def assertions() -> IntegrationAssertions:
    """Provide IntegrationAssertions helper as a fixture."""
    return IntegrationAssertions()
