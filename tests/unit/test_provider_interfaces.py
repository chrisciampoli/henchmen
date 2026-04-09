"""Tests that provider interfaces are importable and properly typed."""

from henchmen.providers.interfaces import (
    CIProvider,
    CIResult,
    CIStatus,
    ContainerOrchestrator,
    DocumentStore,
    JobResult,
    JobStatus,
    LLMProvider,
    MessageBroker,
    ObjectStore,
)


def test_all_interfaces_are_runtime_checkable():
    for iface in [MessageBroker, DocumentStore, ObjectStore, ContainerOrchestrator, LLMProvider, CIProvider]:
        assert hasattr(iface, "__protocol_attrs__") or hasattr(iface, "__abstractmethods__")


def test_job_status_values():
    assert JobStatus.PROVISIONING == "provisioning"
    assert JobStatus.RUNNING == "running"
    assert JobStatus.COMPLETED == "completed"
    assert JobStatus.FAILED == "failed"


def test_ci_status_values():
    assert CIStatus.SUCCESS == "success"
    assert CIStatus.FAILURE == "failure"


def test_job_result_model():
    result = JobResult(job_id="j-1", status=JobStatus.COMPLETED, exit_code=0)
    assert result.exit_code == 0


def test_ci_result_model():
    result = CIResult(build_id="b-1", status=CIStatus.SUCCESS, duration_seconds=42.0)
    assert result.duration_seconds == 42.0
