"""Durable idempotency receipts for executor HTTP work."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError, transaction, connection
from django.utils import timezone

from document_ai.models import ExecutorJobReceipt


class ExecutorJobConflict(ValueError):
    pass


@dataclass(frozen=True)
class ExecutionClaim:
    receipt: ExecutorJobReceipt
    should_execute: bool


def canonical_fingerprint(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def claim_execution(
    *,
    job_id: str,
    work_kind: str,
    target_key: str,
    payload: dict[str, Any],
    source_snapshot: dict[str, Any] | None = None,
    runtime_snapshot: dict[str, Any] | None = None,
) -> ExecutionClaim:
    """Claim a job without holding a transaction through model execution."""

    fingerprint = canonical_fingerprint(payload)
    with transaction.atomic():
        # Serialize only the short receipt claim, never inference. This also
        # closes the absent-row race when two requests reuse a job ID.
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(72401931)")
        receipt = (
            ExecutorJobReceipt.objects.select_for_update().filter(job_id=job_id).first()
        )
        if receipt is not None:
            if receipt.work_kind != work_kind or receipt.payload_fingerprint != fingerprint:
                raise ExecutorJobConflict("job_id was reused with different work or payload")
            if receipt.status == ExecutorJobReceipt.STATUS_RUNNING:
                return ExecutionClaim(receipt=receipt, should_execute=False)
            if receipt.status == ExecutorJobReceipt.STATUS_SUCCEEDED:
                return ExecutionClaim(receipt=receipt, should_execute=False)
            if not receipt.retryable:
                return ExecutionClaim(receipt=receipt, should_execute=False)
            if source_snapshot is not None and receipt.source_snapshot != source_snapshot:
                raise ExecutorJobConflict("Source changed since the original attempt")
            if runtime_snapshot is not None and receipt.runtime_snapshot != runtime_snapshot:
                raise ExecutorJobConflict("Embedding runtime changed since the original attempt")
            if receipt.attempt >= 4:
                fail_execution(receipt, code="RETRIES_EXHAUSTED", message="Execution attempts exhausted", retryable=False)
                receipt.refresh_from_db()
                return ExecutionClaim(receipt=receipt, should_execute=False)
        for active in ExecutorJobReceipt.objects.filter(status="running").exclude(job_id=job_id):
            same_target = active.work_kind == work_kind and active.target_key == target_key
            overlap = set((source_snapshot or {}).get("node_ids", [])) & set(active.source_snapshot.get("node_ids", []))
            chunks_overlap = set(payload.get("chunk_ids", [])) & set(active.source_snapshot.get("payload", {}).get("chunk_ids", []))
            if same_target or overlap or chunks_overlap:
                return ExecutionClaim(receipt=active, should_execute=False)
        if receipt is not None:
            receipt.status = ExecutorJobReceipt.STATUS_RUNNING
            receipt.attempt += 1
            receipt.error = {}
            receipt.retryable = False
            receipt.finished_at = None
            receipt.save(update_fields=["status", "attempt", "error", "retryable", "finished_at", "updated_at"])
            return ExecutionClaim(receipt=receipt, should_execute=True)

        try:
            # Keep the unique-constraint failure inside a savepoint so the
            # outer transaction can read the winning active receipt.
            with transaction.atomic():
                receipt = ExecutorJobReceipt.objects.create(
                    job_id=job_id,
                    work_kind=work_kind,
                    target_key=target_key,
                    payload_fingerprint=fingerprint,
                    source_snapshot=source_snapshot or {},
                    runtime_snapshot=runtime_snapshot or {},
                )
        except IntegrityError:
            # A different envelope is already executing the same durable target.
            active = ExecutorJobReceipt.objects.select_for_update().get(
                work_kind=work_kind,
                target_key=target_key,
                status=ExecutorJobReceipt.STATUS_RUNNING,
            )
            return ExecutionClaim(receipt=active, should_execute=False)
    return ExecutionClaim(receipt=receipt, should_execute=True)


def finish_execution(
    receipt: ExecutorJobReceipt,
    *,
    result: dict[str, Any],
    followups: list[dict[str, Any]] | None = None,
) -> ExecutorJobReceipt:
    with transaction.atomic():
        current = ExecutorJobReceipt.objects.select_for_update().get(job_id=receipt.job_id)
        current.status = ExecutorJobReceipt.STATUS_SUCCEEDED
        current.result = result
        current.followups = followups or []
        current.error = {}
        current.retryable = False
        current.finished_at = timezone.now()
        current.save(update_fields=["status", "result", "followups", "error", "retryable", "finished_at", "updated_at"])
    return current


def fail_execution(
    receipt: ExecutorJobReceipt,
    *,
    code: str,
    message: str,
    retryable: bool,
) -> ExecutorJobReceipt:
    with transaction.atomic():
        current = ExecutorJobReceipt.objects.select_for_update().get(job_id=receipt.job_id)
        current.status = ExecutorJobReceipt.STATUS_FAILED
        current.error = {"code": code, "message": message[:1000]}
        current.retryable = retryable
        current.finished_at = timezone.now()
        current.save(update_fields=["status", "error", "retryable", "finished_at", "updated_at"])
    return current


def receipt_payload(receipt: ExecutorJobReceipt) -> dict[str, Any]:
    return {
        "job_id": receipt.job_id,
        "work_kind": receipt.work_kind,
        "status": receipt.status,
        "attempt": receipt.attempt,
        "result": receipt.result,
        "error": receipt.error,
        "retryable": receipt.retryable,
        "followups": receipt.followups,
    }
