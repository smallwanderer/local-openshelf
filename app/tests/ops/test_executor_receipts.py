import pytest

from document_ai.services.executor_jobs import (
    ExecutorJobConflict,
    claim_execution,
    finish_execution,
)


pytestmark = pytest.mark.django_db


def test_receipt_reuses_terminal_result_for_same_job():
    claim = claim_execution(
        job_id="job-1",
        work_kind="parse_document",
        target_key="node:1",
        payload={"node_id": 1},
    )
    assert claim.should_execute is True
    finish_execution(claim.receipt, result={"status": "success", "node_id": 1})

    replay = claim_execution(
        job_id="job-1",
        work_kind="parse_document",
        target_key="node:1",
        payload={"node_id": 1},
    )
    assert replay.should_execute is False
    assert replay.receipt.result == {"status": "success", "node_id": 1}


def test_receipt_rejects_job_id_reuse_with_different_payload():
    claim_execution(
        job_id="job-1",
        work_kind="parse_document",
        target_key="node:1",
        payload={"node_id": 1},
    )

    with pytest.raises(ExecutorJobConflict):
        claim_execution(
            job_id="job-1",
            work_kind="parse_document",
            target_key="node:2",
            payload={"node_id": 2},
        )


def test_receipt_prevents_parallel_jobs_for_same_target():
    first = claim_execution(
        job_id="job-1",
        work_kind="parse_document",
        target_key="node:1",
        payload={"node_id": 1},
    )
    second = claim_execution(
        job_id="job-2",
        work_kind="parse_document",
        target_key="node:1",
        payload={"node_id": 1},
    )

    assert first.should_execute is True
    assert second.should_execute is False
    assert second.receipt.job_id == "job-1"
