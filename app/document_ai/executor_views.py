"""Private HTTP entry points for parser and embedding executor processes."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from document_ai.orchestration import TASK_ENVELOPE_VERSION, WorkKind, derive_job_id
from document_ai.services.executor_jobs import (
    ExecutorJobConflict,
    claim_execution,
    fail_execution,
    finish_execution,
    receipt_payload,
)


_ROLE_KINDS = {
    "parser": {WorkKind.PARSE_DOCUMENT.value},
    "embedding": {
        WorkKind.PLAN_DOCUMENT_EMBEDDING.value,
        WorkKind.EMBED_DOCUMENT_BATCH.value,
        WorkKind.EVALUATE_RETRIEVAL.value,
    },
}
_JOB_SLOT = threading.BoundedSemaphore(1)


def _executor_role() -> str:
    return os.getenv("DOTORI_EXECUTOR_ROLE", "embedding")


def _authorized(request) -> bool:
    token = settings.EXECUTOR_INTERNAL_TOKEN
    return bool(token) and request.headers.get("Authorization") == f"Bearer {token}"


def _error(code: str, message: str, status: int, *, retryable: bool = False):
    response = JsonResponse(
        {"ok": False, "error": {"code": code, "message": message}, "retryable": retryable},
        status=status,
    )
    if code == "EXECUTOR_BUSY":
        response["Retry-After"] = "5"
    return response


def _validate_envelope(payload: Any) -> tuple[dict[str, Any], str] | tuple[None, str]:
    if not isinstance(payload, dict):
        return None, "Expected a JSON object."
    required = ("job_id", "work_kind", "payload")
    if any(not payload.get(field) for field in required):
        return None, "job_id, work_kind, and payload are required."
    if payload.get("envelope_version") != TASK_ENVELOPE_VERSION:
        return None, "Unsupported task envelope version."
    if not isinstance(payload["payload"], dict):
        return None, "payload must be an object."
    if not isinstance(payload["job_id"], str) or len(payload["job_id"]) > 64:
        return None, "job_id must be a string up to 64 characters."
    return payload, ""


def _target_key(work_kind: str, payload: dict[str, Any]) -> str:
    if work_kind in {WorkKind.PARSE_DOCUMENT.value, WorkKind.PLAN_DOCUMENT_EMBEDDING.value}:
        node_id = payload.get("node_id")
        if not isinstance(node_id, int) or node_id < 1:
            raise ValueError("payload.node_id must be a positive integer")
        return f"node:{node_id}"
    if work_kind == WorkKind.EMBED_DOCUMENT_BATCH.value:
        chunk_ids = payload.get("chunk_ids")
        if not isinstance(chunk_ids, list) or not chunk_ids or any(not isinstance(value, int) or value < 1 for value in chunk_ids):
            raise ValueError("payload.chunk_ids must be a non-empty list of positive integers")
        return "chunks:" + ",".join(str(value) for value in sorted(set(chunk_ids)))
    if work_kind == WorkKind.EVALUATE_RETRIEVAL.value:
        run_uid = payload.get("run_uid")
        if not isinstance(run_uid, str) or not run_uid:
            raise ValueError("payload.run_uid must be a non-empty string")
        return f"evaluation:{run_uid}"
    raise ValueError("Unsupported work_kind")


def _execute_parser(envelope: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from document_ai.processing.parsing import ParseExecutionFailed, execute_parse_node, get_parser_for_execution

    node_id = envelope["payload"]["node_id"]
    from document_ai.models import ExecutorJobReceipt
    followups = [{
        "job_id": derive_job_id(envelope["job_id"], WorkKind.PLAN_DOCUMENT_EMBEDDING, str(node_id)),
        "work_kind": WorkKind.PLAN_DOCUMENT_EMBEDDING.value,
        "payload": {"node_id": node_id}, "parent_job_id": envelope["job_id"],
    }]
    def persisted(result):
        from document_ai.services.executor_snapshot import validate_execution
        validate_execution()
        receipt = ExecutorJobReceipt.objects.get(job_id=envelope["job_id"])
        finish_execution(receipt, result=result, followups=followups)
    try:
        result = execute_parse_node(
            node_id,
            parser=get_parser_for_execution(),
            on_persisted=persisted,
            trace_id=envelope.get("trace_id"),
            enqueued_at=envelope.get("enqueued_at"),
            job_id=envelope["job_id"],
            parent_job_id=envelope.get("parent_job_id"),
            work_kind=envelope["work_kind"],
            envelope_version=envelope["envelope_version"],
        )
    except ParseExecutionFailed as exc:
        raise exc.cause
    if result.get("status") != "success":
        return result, []
    child_job_id = derive_job_id(envelope["job_id"], WorkKind.PLAN_DOCUMENT_EMBEDDING, str(node_id))
    return result, [{
        "job_id": child_job_id,
        "work_kind": WorkKind.PLAN_DOCUMENT_EMBEDDING.value,
        "payload": {"node_id": node_id},
        "parent_job_id": envelope["job_id"],
    }]


def _execute_embedding(envelope: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    kind = envelope["work_kind"]
    payload = envelope["payload"]
    if kind == WorkKind.PLAN_DOCUMENT_EMBEDDING.value:
        from document_ai.embedding.executor import enqueue_embedding_tasks_sync

        batches: list[list[int]] = []
        result = enqueue_embedding_tasks_sync(
            payload["node_id"],
            trace_id=envelope.get("trace_id"),
            parent_job_id=envelope["job_id"],
            dispatch_batch=lambda chunk_ids, **_kwargs: batches.append(chunk_ids),
        )
        followups = [{
            "job_id": derive_job_id(envelope["job_id"], WorkKind.EMBED_DOCUMENT_BATCH, ",".join(map(str, batch))),
            "work_kind": WorkKind.EMBED_DOCUMENT_BATCH.value,
            "payload": {"chunk_ids": batch},
            "parent_job_id": envelope["job_id"],
        } for batch in batches]
        return result, followups
    if kind == WorkKind.EMBED_DOCUMENT_BATCH.value:
        from document_ai.embedding.executor import embed_document_chunks_batch_sync

        return embed_document_chunks_batch_sync(
            payload["chunk_ids"],
            trace_id=envelope.get("trace_id"),
            enqueued_at=envelope.get("enqueued_at"),
            job_id=envelope["job_id"],
            parent_job_id=envelope.get("parent_job_id"),
            work_kind=kind,
            envelope_version=envelope["envelope_version"],
        ), []
    if kind == WorkKind.EVALUATE_RETRIEVAL.value:
        from document_ai.search.evaluation import execute_quality_evaluation_run

        execute_quality_evaluation_run(payload["run_uid"])
        from workspaces.models import WorkspaceQualityEvaluationRun
        run = WorkspaceQualityEvaluationRun.objects.get(uid=payload["run_uid"])
        return {"status": "success" if run.status == "succeeded" else "failed",
                "run_uid": payload["run_uid"], "error": run.error_message}, []
    raise ValueError("Unsupported embedding work_kind")


@csrf_exempt
@require_POST
def execute_job(request):
    if not _authorized(request):
        return _error("UNAUTHORIZED", "Executor token is required.", 401)
    if not _JOB_SLOT.acquire(blocking=False):
        return _error("EXECUTOR_BUSY", "Executor job slot occupied.", 503, retryable=True)
    try:
        return _execute_job(request)
    finally:
        from document_ai.services.executor_snapshot import active_execution
        active_execution.set(None)
        _JOB_SLOT.release()


def _execute_job(request):
    if not _authorized(request):
        return _error("UNAUTHORIZED", "Executor token is required.", 401)
    try:
        raw_payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return _error("INVALID_JSON", "Malformed JSON request body.", 400)
    envelope, error = _validate_envelope(raw_payload)
    if envelope is None:
        return _error("INVALID_ENVELOPE", error, 422)
    role = _executor_role()
    if envelope["work_kind"] not in _ROLE_KINDS.get(role, set()):
        return _error("UNSUPPORTED_WORK_KIND", "This executor does not own the requested work.", 422)
    try:
        target_key = _target_key(envelope["work_kind"], envelope["payload"])
        from document_ai.services.executor_snapshot import capture_snapshot
        source, runtime = capture_snapshot(envelope)
        claim = claim_execution(
            job_id=envelope["job_id"],
            work_kind=envelope["work_kind"],
            target_key=target_key,
            payload=envelope["payload"],
            source_snapshot=source,
            runtime_snapshot=runtime,
        )
    except ExecutorJobConflict as exc:
        return _error("JOB_ID_CONFLICT", str(exc), 409)
    except ValueError as exc:
        return _error("INVALID_PAYLOAD", str(exc), 422)

    if not claim.should_execute:
        body = receipt_payload(claim.receipt)
        return JsonResponse({"ok": claim.receipt.status != "failed", **body}, status=202 if claim.receipt.status == "running" else 200)

    try:
        if float(envelope.get("deadline_at", time.time() + 1800)) <= time.time():
            raise ValueError("Executor deadline exceeded")
        from document_ai.services.executor_snapshot import active_execution, validate_execution
        active_execution.set((envelope, source, runtime))
        validate_execution()
        # Planning does no inference. Commit claims and durable child envelopes
        # together, so a crash cannot strand PROCESSING chunks without batches.
        if envelope["work_kind"] == WorkKind.PLAN_DOCUMENT_EMBEDDING.value:
            with transaction.atomic():
                result, followups = _execute_embedding(envelope)
                if result.get("status") == "failed":
                    raise RuntimeError(result.get("error", "Planning failed"))
                receipt = finish_execution(claim.receipt, result=result, followups=followups)
            return JsonResponse({"ok": True, **receipt_payload(receipt)})
        if role == "parser":
            result, followups = _execute_parser(envelope)
        else:
            result, followups = _execute_embedding(envelope)
        validate_execution()
    except Exception as exc:  # executor failures are classified by the transport retry policy
        receipt = fail_execution(claim.receipt, code="EXECUTION_FAILED", message=str(exc), retryable=not isinstance(exc, (ValueError, FileNotFoundError)))
        return JsonResponse({"ok": False, **receipt_payload(receipt)}, status=503)

    if result.get("status") == "failed":
        receipt = fail_execution(
            claim.receipt,
            code="DOMAIN_FAILURE",
            message=str(result.get("error", "executor work failed")),
            retryable=False,
        )
        return JsonResponse({"ok": False, **receipt_payload(receipt)}, status=200)
    receipt = finish_execution(claim.receipt, result=result, followups=followups)
    return JsonResponse({"ok": True, **receipt_payload(receipt)})


@require_GET
def job_status(request, job_id: str):
    if not _authorized(request):
        return _error("UNAUTHORIZED", "Executor token is required.", 401)
    from document_ai.models import ExecutorJobReceipt

    receipt = ExecutorJobReceipt.objects.filter(job_id=job_id).first()
    if receipt is None:
        return _error("JOB_NOT_FOUND", "No executor job receipt was found.", 404)
    return JsonResponse({"ok": True, **receipt_payload(receipt)})


@require_GET
def livez(_request):
    if "DOTORI_EXECUTOR_ROLE" not in os.environ:
        return JsonResponse({"status": "ok"})
    return JsonResponse({"status": "live", "role": _executor_role()})


@require_GET
def readyz(request):
    if _executor_role() == "parser":
        return JsonResponse({"status": "ready", "role": "parser"})
    from document_ai.embedding.internal_views import readyz as embedding_readyz

    return embedding_readyz(request)
