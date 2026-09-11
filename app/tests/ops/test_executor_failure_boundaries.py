import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from celery.exceptions import Retry
from django.test import RequestFactory

from document_ai import executor_views, tasks
from document_ai.orchestration import WorkKind
from document_ai.models import ExecutorJobReceipt
from document_ai.services.executor_jobs import claim_execution, fail_execution


def envelope(job="job"):
    return {"job_id": job, "envelope_version": 1, "work_kind": "parse_document", "payload": {"node_id": 1}}


def test_followup_publish_failure_retries_parent_with_original_deadline():
    deadline = time.time() + 300
    task = SimpleNamespace(request=SimpleNamespace(headers={"executor_deadline": deadline}, retries=0), retry=Mock(side_effect=Retry()))
    with patch("document_ai.services.executor_transport.dispatch_executor_job", return_value={"result": {"status": "success"}}), patch.object(tasks, "_publish_executor_followups", side_effect=RuntimeError("Redis disconnected")):
        with pytest.raises(Retry):
            tasks._dispatch_or_retry(task, WorkKind.PARSE_DOCUMENT, envelope=envelope(), payload={"node_id": 1})
    assert task.retry.call_args.kwargs["headers"]["executor_deadline"] == deadline
    assert tasks._DISPATCH_SLOTS["parse"].acquire(blocking=False)
    tasks._DISPATCH_SLOTS["parse"].release()


def test_parse_slot_busy_does_not_block_embed_dispatch():
    task = SimpleNamespace(request=SimpleNamespace(headers={}, retries=0), retry=Mock(side_effect=Retry()))
    tasks._DISPATCH_SLOTS["parse"].acquire()
    try:
        with patch("document_ai.services.executor_transport.dispatch_executor_job", return_value={"result": {"status": "success"}}) as dispatch:
            assert tasks._dispatch_or_retry(task, WorkKind.PLAN_DOCUMENT_EMBEDDING, envelope=envelope(), payload={"node_id": 1})["status"] == "success"
            dispatch.assert_called_once()
    finally:
        tasks._DISPATCH_SLOTS["parse"].release()


@pytest.mark.django_db
def test_overlapping_batch_claims_do_not_execute_together():
    first = claim_execution(job_id="one", work_kind="embed_document_batch", target_key="chunks:1,2", payload={"chunk_ids": [1, 2]}, source_snapshot={"payload": {"chunk_ids": [1, 2]}})
    second = claim_execution(job_id="two", work_kind="embed_document_batch", target_key="chunks:2,3", payload={"chunk_ids": [2, 3]})
    assert first.should_execute and not second.should_execute


@pytest.mark.django_db
def test_domain_attempt_budget_is_bounded():
    args = dict(job_id="one", work_kind="parse_document", target_key="node:1", payload={"node_id": 1})
    for _ in range(4):
        claim = claim_execution(**args)
        assert claim.should_execute
        fail_execution(claim.receipt, code="TEMP", message="temporary", retryable=True)
    final = claim_execution(**args)
    assert not final.should_execute
    assert final.receipt.error["code"] == "RETRIES_EXHAUSTED"


@pytest.mark.django_db
def test_planning_receipt_failure_rolls_back_domain_write(settings, monkeypatch):
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "embedding")
    body = envelope()
    body["work_kind"] = "plan_document_embedding"
    request = RequestFactory().post("/", json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION="Bearer token")
    def plan(_):
        ExecutorJobReceipt.objects.create(job_id="domain-write", work_kind="test", target_key="test")
        return {"status": "success"}, []
    with patch("document_ai.services.executor_snapshot.capture_snapshot", return_value=({}, {})), patch.object(executor_views, "_execute_embedding", side_effect=plan), patch.object(executor_views, "finish_execution", side_effect=RuntimeError("commit interrupted")):
        response = executor_views.execute_job(request)
    assert response.status_code == 503
    assert not ExecutorJobReceipt.objects.filter(job_id="domain-write").exists()
    assert ExecutorJobReceipt.objects.get(job_id="job").status == "failed"


def test_executor_busy_leaves_status_thread_available(settings):
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    request = RequestFactory().post("/", "{}", content_type="application/json", HTTP_AUTHORIZATION="Bearer token")
    executor_views._JOB_SLOT.acquire()
    try:
        response = executor_views.execute_job(request)
        assert response.status_code == 503
        assert response["Retry-After"] == "5"
    finally:
        executor_views._JOB_SLOT.release()


def test_response_loss_replay_reads_receipt_without_reexecuting():
    from document_ai.services.executor_transport import dispatch_executor_job
    with patch("document_ai.services.executor_transport.read_executor_job", return_value={"status": "succeeded", "result": {"status": "success"}}), patch("document_ai.services.executor_transport.requests.post") as post:
        result = dispatch_executor_job(WorkKind.PARSE_DOCUMENT, envelope={**envelope(), "attempt": 2}, payload={"node_id": 1})
    assert result["status"] == "succeeded"
    post.assert_not_called()


def test_stale_snapshot_prevents_domain_persistence(monkeypatch):
    """An executor must reject work whose document/runtime changed in flight."""
    from document_ai.services import executor_snapshot

    task_envelope = envelope()
    expected_source = {"payload": task_envelope["payload"], "revision": 1}
    expected_runtime = {"fingerprint": "runtime-a"}
    token = executor_snapshot.active_execution.set(
        (task_envelope, expected_source, expected_runtime)
    )
    try:
        monkeypatch.setattr(
            executor_snapshot,
            "capture_snapshot",
            lambda _envelope: ({"payload": task_envelope["payload"], "revision": 2}, expected_runtime),
        )
        with pytest.raises(executor_snapshot.StaleExecution):
            executor_snapshot.validate_execution()
    finally:
        executor_snapshot.active_execution.reset(token)


def test_legacy_message_uses_celery_request_id():
    tasks.parse_document_with_docling.push_request(id="legacy-fixed-id")
    try:
        with patch("document_ai.tasks._embedding_queue_backpressure", return_value=(False, 0, 1)), patch("document_ai.services.executor_transport.dispatch_executor_job", return_value={"result": {"status": "success"}}) as dispatch:
            tasks.parse_document_with_docling(1)
    finally:
        tasks.parse_document_with_docling.pop_request()
    assert dispatch.call_args.kwargs["envelope"]["job_id"] == "legacy-fixed-id"
