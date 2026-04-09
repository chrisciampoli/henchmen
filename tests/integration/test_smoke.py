"""Smoke tests verifying that all integration test fixtures are functional."""

import json
from pathlib import Path

import pytest

from tests.integration.conftest import (
    IntegrationAssertions,
    MockFirestoreClient,
    MockPubSubPublisher,
    MockStorageClient,
)
from tests.integration.helpers import (
    create_mock_ci_result,
    create_mock_operative_report,
    wait_for_condition,
)

# ---------------------------------------------------------------------------
# MockPubSubPublisher
# ---------------------------------------------------------------------------


class TestMockPubSubPublisher:
    def test_topic_path(self, mock_pubsub: MockPubSubPublisher):
        path = mock_pubsub.topic_path("my-project", "my-topic")
        assert path == "projects/my-project/topics/my-topic"

    def test_publish_captures_message(self, mock_pubsub: MockPubSubPublisher):
        topic = mock_pubsub.topic_path("proj", "henchmen-task-intake")
        payload = {"id": "abc", "title": "Fix bug"}
        future = mock_pubsub.publish(topic, json.dumps(payload).encode())

        assert future.result() == "mock-message-id"
        assert len(mock_pubsub.published_messages) == 1
        msg = mock_pubsub.published_messages[0]
        assert msg["topic"] == topic
        assert msg["data"] == payload

    def test_publish_with_attributes(self, mock_pubsub: MockPubSubPublisher):
        topic = mock_pubsub.topic_path("proj", "henchmen-task-intake")
        mock_pubsub.publish(topic, b"{}", source="cli", version="1")
        assert mock_pubsub.published_messages[0]["attributes"] == {"source": "cli", "version": "1"}

    def test_get_messages_for_topic(self, mock_pubsub: MockPubSubPublisher):
        topic_a = mock_pubsub.topic_path("proj", "henchmen-task-intake")
        topic_b = mock_pubsub.topic_path("proj", "henchmen-task-planned")
        mock_pubsub.publish(topic_a, b"{}")
        mock_pubsub.publish(topic_b, b"{}")
        mock_pubsub.publish(topic_a, b"{}")

        assert len(mock_pubsub.get_messages_for_topic("task-intake")) == 2
        assert len(mock_pubsub.get_messages_for_topic("task-planned")) == 1

    def test_assert_published_to_passes(self, mock_pubsub: MockPubSubPublisher):
        topic = mock_pubsub.topic_path("proj", "henchmen-task-intake")
        mock_pubsub.publish(topic, b"{}")
        mock_pubsub.assert_published_to("task-intake", count=1)  # should not raise

    def test_assert_published_to_fails(self, mock_pubsub: MockPubSubPublisher):
        with pytest.raises(AssertionError):
            mock_pubsub.assert_published_to("task-intake", count=1)


# ---------------------------------------------------------------------------
# MockFirestoreClient
# ---------------------------------------------------------------------------


class TestMockFirestoreClient:
    def test_set_and_get_document(self, mock_firestore: MockFirestoreClient):
        doc_ref = mock_firestore.collection("tasks").document("task-1")
        doc_ref.set({"status": "pending", "title": "Fix bug"})

        snapshot = doc_ref.get()
        assert snapshot.exists
        assert snapshot.id == "task-1"
        assert snapshot.to_dict() == {"status": "pending", "title": "Fix bug"}

    def test_get_missing_document(self, mock_firestore: MockFirestoreClient):
        snapshot = mock_firestore.collection("tasks").document("nonexistent").get()
        assert not snapshot.exists
        assert snapshot.to_dict() is None

    def test_update_document(self, mock_firestore: MockFirestoreClient):
        col = mock_firestore.collection("tasks")
        col.document("task-1").set({"status": "pending"})
        col.document("task-1").update({"status": "completed"})

        data = col.document("task-1").get().to_dict()
        assert data["status"] == "completed"

    def test_delete_document(self, mock_firestore: MockFirestoreClient):
        col = mock_firestore.collection("tasks")
        col.document("task-1").set({"status": "pending"})
        col.document("task-1").delete()

        assert not col.document("task-1").get().exists

    def test_add_generates_id(self, mock_firestore: MockFirestoreClient):
        col = mock_firestore.collection("tasks")
        _, doc_ref = col.add({"status": "pending"})
        assert doc_ref.id  # non-empty auto-generated ID
        assert doc_ref.get().exists

    def test_where_filter(self, mock_firestore: MockFirestoreClient):
        col = mock_firestore.collection("tasks")
        col.document("a").set({"status": "pending", "priority": "high"})
        col.document("b").set({"status": "completed", "priority": "low"})
        col.document("c").set({"status": "pending", "priority": "normal"})

        results = col.where("status", "==", "pending").get()
        assert len(results) == 2
        ids = {s.id for s in results}
        assert ids == {"a", "c"}

    def test_limit(self, mock_firestore: MockFirestoreClient):
        col = mock_firestore.collection("tasks")
        for i in range(5):
            col.document(f"doc-{i}").set({"n": i})

        results = col.limit(3).get()
        assert len(results) == 3

    def test_stream_returns_all(self, mock_firestore: MockFirestoreClient):
        col = mock_firestore.collection("tasks")
        col.document("x").set({"v": 1})
        col.document("y").set({"v": 2})

        streamed = col.stream()
        assert len(streamed) == 2

    def test_collections_are_isolated(self, mock_firestore: MockFirestoreClient):
        mock_firestore.collection("tasks").document("t1").set({"x": 1})
        mock_firestore.collection("merge_queue").document("t1").set({"y": 2})

        task_doc = mock_firestore.collection("tasks").document("t1").get().to_dict()
        queue_doc = mock_firestore.collection("merge_queue").document("t1").get().to_dict()
        assert task_doc == {"x": 1}
        assert queue_doc == {"y": 2}


# ---------------------------------------------------------------------------
# MockStorageClient
# ---------------------------------------------------------------------------


class TestMockStorageClient:
    def test_blob_does_not_exist_initially(self, mock_gcs: MockStorageClient):
        blob = mock_gcs.bucket("dossier-bucket").blob("task-1/context.json")
        assert not blob.exists()

    def test_upload_and_download_string(self, mock_gcs: MockStorageClient):
        blob = mock_gcs.bucket("dossier-bucket").blob("task-1/context.json")
        blob.upload_from_string('{"key": "value"}')
        assert blob.exists()
        assert blob.download_as_text() == '{"key": "value"}'

    def test_upload_bytes(self, mock_gcs: MockStorageClient):
        blob = mock_gcs.bucket("snapshots-bucket").blob("snap.tar.gz")
        blob.upload_from_string(b"\x00\x01\x02")
        assert blob.exists()

    def test_upload_and_download_file(self, mock_gcs: MockStorageClient, tmp_path: Path):
        src = tmp_path / "upload.txt"
        src.write_text("hello from file")

        blob = mock_gcs.bucket("test-bucket").blob("hello.txt")
        blob.upload_from_filename(str(src))
        assert blob.exists()

        dst = tmp_path / "download.txt"
        blob.download_to_filename(str(dst))
        assert dst.read_text() == "hello from file"

    def test_missing_blob_download_raises(self, mock_gcs: MockStorageClient):
        blob = mock_gcs.bucket("test-bucket").blob("missing.txt")
        with pytest.raises(FileNotFoundError):
            blob.download_as_text()

    def test_buckets_are_isolated(self, mock_gcs: MockStorageClient):
        mock_gcs.bucket("bucket-a").blob("file.txt").upload_from_string("aaa")
        assert not mock_gcs.bucket("bucket-b").blob("file.txt").exists()


# ---------------------------------------------------------------------------
# test_workspace fixture
# ---------------------------------------------------------------------------


class TestWorkspaceFixture:
    def test_workspace_is_git_repo(self, test_workspace: Path):
        assert (test_workspace / ".git").exists()

    def test_workspace_has_expected_files(self, test_workspace: Path):
        assert (test_workspace / "src" / "auth.py").exists()
        assert (test_workspace / "tests" / "test_auth.py").exists()
        assert (test_workspace / "CLAUDE.md").exists()
        assert (test_workspace / "pyproject.toml").exists()

    def test_workspace_has_initial_commit(self, test_workspace: Path):
        import subprocess

        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=test_workspace,
            capture_output=True,
            text=True,
        )
        assert "Initial commit" in result.stdout


# ---------------------------------------------------------------------------
# integration_settings fixture
# ---------------------------------------------------------------------------


class TestIntegrationSettings:
    def test_settings_use_test_project(self, integration_settings):
        assert integration_settings.gcp_project_id == "test-project"
        assert integration_settings.environment.value == "dev"
        assert integration_settings.gcp_region == "us-central1"


# ---------------------------------------------------------------------------
# Sample task / event data fixtures
# ---------------------------------------------------------------------------


class TestSampleEventFixtures:
    def test_cli_task_data_has_required_fields(self, cli_task_data: dict):
        for field in ("title", "description", "repo", "branch", "priority", "created_by"):
            assert field in cli_task_data

    def test_slack_event_data_shape(self, slack_event_data: dict):
        assert slack_event_data["type"] == "app_mention"
        assert "user" in slack_event_data

    def test_github_issue_event_shape(self, github_issue_event: dict):
        assert github_issue_event["action"] == "labeled"
        assert github_issue_event["label"]["name"] == "henchmen"

    def test_github_pr_comment_event_shape(self, github_pr_comment_event: dict):
        assert github_pr_comment_event["action"] == "created"
        assert "pull_request" in github_pr_comment_event

    def test_jira_webhook_data_shape(self, jira_webhook_data: dict):
        assert jira_webhook_data["webhookEvent"] == "jira:issue_updated"
        assert "issue" in jira_webhook_data


# ---------------------------------------------------------------------------
# IntegrationAssertions fixture
# ---------------------------------------------------------------------------


class TestIntegrationAssertions:
    def test_assert_valid_henchmen_task_passes(self, assertions: IntegrationAssertions):
        task = {
            "id": "abc",
            "source": "slack",
            "source_id": "C01/ts",
            "title": "Fix bug",
            "description": "desc",
            "context": {},
            "priority": "high",
            "status": "pending",
        }
        assertions.assert_valid_henchmen_task(task)  # should not raise

    def test_assert_valid_henchmen_task_fails_missing_field(self, assertions: IntegrationAssertions):
        task = {"id": "abc", "source": "slack"}
        with pytest.raises(AssertionError, match="Missing required field"):
            assertions.assert_valid_henchmen_task(task)

    def test_assert_pubsub_message_contains_passes(
        self, mock_pubsub: MockPubSubPublisher, assertions: IntegrationAssertions
    ):
        topic = mock_pubsub.topic_path("proj", "henchmen-task-intake")
        mock_pubsub.publish(topic, json.dumps({"source": "cli", "title": "Fix bug"}).encode())
        assertions.assert_pubsub_message_contains(mock_pubsub, "task-intake", source="cli")

    def test_assert_pubsub_message_contains_fails_no_messages(
        self, mock_pubsub: MockPubSubPublisher, assertions: IntegrationAssertions
    ):
        with pytest.raises(AssertionError, match="No messages published"):
            assertions.assert_pubsub_message_contains(mock_pubsub, "task-intake", source="cli")

    def test_assert_state_machine_reached(self, assertions: IntegrationAssertions):
        assertions.assert_state_machine_reached({"current_state": "planning"}, "planning")

    def test_assert_state_machine_reached_fails(self, assertions: IntegrationAssertions):
        with pytest.raises(AssertionError):
            assertions.assert_state_machine_reached({"current_state": "pending"}, "planning")


# ---------------------------------------------------------------------------
# helpers.py
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_create_mock_operative_report_completed(self):
        report = create_mock_operative_report("task-1", "investigate", "bug-fix-v1")
        assert report["task_id"] == "task-1"
        assert report["node_id"] == "investigate"
        assert report["scheme_id"] == "bug-fix-v1"
        assert report["status"] == "completed"
        assert 0.0 <= report["confidence_score"] <= 1.0
        assert report["error"] is None

    def test_create_mock_operative_report_failed(self):
        report = create_mock_operative_report("task-1", "investigate", "bug-fix-v1", status="failed")
        assert report["status"] == "failed"
        assert report["error"] is not None
        assert report["git_diff"] is None

    def test_create_mock_ci_result_passed(self):
        result = create_mock_ci_result("https://github.com/acme/repo/pull/1")
        assert result["status"] == "passed"
        assert result["pr_url"] == "https://github.com/acme/repo/pull/1"
        assert len(result["checks"]) == 2

    def test_create_mock_ci_result_failed(self):
        result = create_mock_ci_result("https://github.com/acme/repo/pull/2", status="failed")
        assert result["status"] == "failed"

    def test_wait_for_condition_true_immediately(self):
        assert wait_for_condition(lambda: True, timeout=1.0) is True

    def test_wait_for_condition_becomes_true(self):
        state = {"ready": False}

        import threading

        def flip():
            import time

            time.sleep(0.15)
            state["ready"] = True

        threading.Thread(target=flip, daemon=True).start()
        result = wait_for_condition(lambda: state["ready"], timeout=2.0, interval=0.05)
        assert result is True

    def test_wait_for_condition_timeout(self):
        result = wait_for_condition(lambda: False, timeout=0.2, interval=0.05)
        assert result is False
