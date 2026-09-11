from __future__ import annotations

import logging
import random
import time
import threading

from celery import shared_task

from document_ai.orchestration import (
    WorkKind,
    embedding_queue_backpressure as _embedding_queue_backpressure,
    enqueue_embedding_batch,
    enqueue_embedding,
    enqueue_parse,
    enqueue_retrieval_evaluation,
)
from document_ai.services.environment import get_positive_int_env as _get_positive_int_env

from config.tracing import get_trace_id, new_trace_id, set_trace_id

logger = logging.getLogger(__name__)
_DISPATCH_SLOTS = {"parse": threading.BoundedSemaphore(1), "embed": threading.BoundedSemaphore(1)}


# Compatibility names for extensions/tests that historically imported these
# helpers from tasks.py. Their real ownership remains document_ai.rag and they
# are lazy so a queue-only orchestrator does not import the RAG stack at boot.
def _build_rag_context(*args, **kwargs):
    from document_ai.rag.generation import _build_rag_context as implementation

    return implementation(*args, **kwargs)


def _extract_llm_final_content(*args, **kwargs):
    from document_ai.rag.generation import _extract_llm_final_content as implementation

    return implementation(*args, **kwargs)


def _executor_envelope(
    *,
    job_id: str | None,
    parent_job_id: str | None,
    trace_id: str | None,
    enqueued_at: str | None,
    work_kind: str | None,
    envelope_version: int | None,
) -> dict:
    from document_ai.orchestration import TASK_ENVELOPE_VERSION
    from config.tracing import get_trace_id, new_trace_id

    if not job_id:
        from celery import current_task
        from uuid import uuid4

        # Messages published before envelope v1 carry Celery's immutable task
        # id. Direct compatibility calls have no request object, so they get a
        # one-off identity only for that local invocation.
        job_id = getattr(getattr(current_task, "request", None), "id", None) or str(uuid4())
    return {
        "job_id": job_id,
        "parent_job_id": parent_job_id,
        "trace_id": trace_id or get_trace_id() or new_trace_id(),
        "enqueued_at": enqueued_at,
        "work_kind": work_kind,
        "envelope_version": envelope_version or TASK_ENVELOPE_VERSION,
    }


def _publish_executor_followups(response: dict) -> None:
    for followup in response.get("followups") or []:
        kind = WorkKind(followup["work_kind"])
        payload = followup["payload"]
        kwargs = {
            "job_id": followup["job_id"],
            "parent_job_id": followup.get("parent_job_id"),
        }
        if kind is WorkKind.PARSE_DOCUMENT:
            enqueue_parse(payload["node_id"], **kwargs)
        elif kind is WorkKind.PLAN_DOCUMENT_EMBEDDING:
            enqueue_embedding(payload["node_id"], **kwargs)
        elif kind is WorkKind.EMBED_DOCUMENT_BATCH:
            enqueue_embedding_batch(payload["chunk_ids"], **kwargs)
        elif kind is WorkKind.EVALUATE_RETRIEVAL:
            enqueue_retrieval_evaluation(payload["run_uid"], **kwargs)


def _dispatch_or_retry(task, kind: WorkKind, *, envelope: dict, payload: dict) -> dict:
    from document_ai.services.executor_transport import ExecutorDispatchError, dispatch_executor_job

    headers = dict(getattr(task.request, "headers", None) or {})
    deadline = headers.setdefault("executor_deadline", time.time() + 1800)
    envelope = {**envelope, "deadline_at": deadline, "attempt": (getattr(task.request, "retries", 0) or 0) + 1}
    if time.time() >= deadline:
        return {"status": "failed", "error": "Executor deadline exceeded; inspect receipt before recovery"}

    def retry(exc, delay=5):
        # Admission, response loss and publication retries are bounded by the
        # original deadline, not the domain execution failure budget.
        raise task.retry(exc=exc, countdown=min(delay + random.uniform(0, 1), max(0, deadline - time.time())),
                         headers=headers, max_retries=100000)

    slot = _DISPATCH_SLOTS["parse" if kind == WorkKind.PARSE_DOCUMENT else "embed"]
    if not slot.acquire(blocking=False):
        retry(ExecutorDispatchError("Transport slot busy"))
    try:
        try:
            response = dispatch_executor_job(kind, envelope=envelope, payload=payload)
        except ExecutorDispatchError as exc:
            if exc.retryable:
                retry(exc, exc.retry_after_seconds or 5)
            return {"status": "failed", "error": str(exc)}
        try:
            _publish_executor_followups(response)
        except Exception as exc:
            # Replay reads the durable parent and republishes identical child IDs.
            retry(exc)
    finally:
        slot.release()
    return response.get("result") or {"status": "success"}


@shared_task(queue="parse")
def recover_document_pipeline_backlog() -> dict:
    from document_ai.processing.recovery import recover_document_pipeline_backlog_sync

    return recover_document_pipeline_backlog_sync()


@shared_task(
    queue="parse",
    bind=True,
    max_retries=2,
    retry_backoff=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def parse_document_with_docling(
    self,
    node_id: int,
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
) -> dict:
    """
    Celery 태스크: 파싱 → DB 저장 오케스트레이션
    node_id를 받아 파일 경로를 조회하고, 파싱 후 결과를 DB에 저장
    """
    should_pause, embed_queue_depth, backpressure_limit = _embedding_queue_backpressure()
    if should_pause:
        retry_seconds = _get_positive_int_env(
            "DOCUMENT_AI_PARSE_BACKPRESSURE_RETRY_SECONDS", 5
        )
        logger.warning(
            "Parse deferred by embed queue backpressure: node_id=%s, embed_queue_depth=%s, limit=%s, retry_seconds=%s",
            node_id,
            embed_queue_depth,
            backpressure_limit,
            retry_seconds,
        )
        raise self.retry(
            exc=RuntimeError(
                f"embed queue depth {embed_queue_depth} reached limit {backpressure_limit}"
            ),
            countdown=retry_seconds,
            max_retries=None,
        )

    return _dispatch_or_retry(
        self,
        WorkKind.PARSE_DOCUMENT,
        envelope=_executor_envelope(
            job_id=job_id,
            parent_job_id=parent_job_id,
            trace_id=trace_id,
            enqueued_at=enqueued_at,
            work_kind=work_kind,
            envelope_version=envelope_version,
        ),
        payload={"node_id": node_id},
    )


@shared_task(queue="embed", bind=True, acks_late=True, reject_on_worker_lost=True)
def run_quality_evaluation_task(
    self,
    run_uid: str,
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
) -> dict:
    """Runs a workspace quality-profile evaluation (WorkspaceQualityEvaluationRun)
    and persists its metrics. Defined here rather than in document_ai.search.evaluation
    because Celery's autodiscover_tasks() only imports each app's tasks.py module."""
    return _dispatch_or_retry(
        self,
        WorkKind.EVALUATE_RETRIEVAL,
        envelope=_executor_envelope(
            job_id=job_id,
            parent_job_id=parent_job_id,
            trace_id=trace_id,
            enqueued_at=enqueued_at,
            work_kind=work_kind,
            envelope_version=envelope_version,
        ),
        payload={"run_uid": run_uid},
    )


@shared_task(
    queue="embed",
    bind=True,
    max_retries=3,
    retry_backoff=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def enqueue_embedding_tasks(
    self,
    node_id: int,
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
) -> dict:
    """
    Celery 태스크: 임베딩 → DB 저장 오케스트레이션
    node_id를 받아 파일 경로를 조회하고, 임베딩 후 결과를 DB에 저장
    """
    return _dispatch_or_retry(
        self,
        WorkKind.PLAN_DOCUMENT_EMBEDDING,
        envelope=_executor_envelope(
            job_id=job_id,
            parent_job_id=parent_job_id,
            trace_id=trace_id,
            enqueued_at=enqueued_at,
            work_kind=work_kind,
            envelope_version=envelope_version,
        ),
        payload={"node_id": node_id},
    )


@shared_task(
    queue="embed",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def embedding_document_with_bge(
    self, chunk_id: int, trace_id: str | None = None, enqueued_at: str | None = None
) -> dict:
    """
    Celery 태스크: 임베딩 → DB 저장 오케스트레이션
    node_id를 받아 파일 경로를 조회하고, 임베딩 후 결과를 DB에 저장
    """
    from document_ai.orchestration import TaskEnvelope

    envelope = TaskEnvelope.create(WorkKind.EMBED_DOCUMENT_BATCH, job_id=self.request.id, trace_id=trace_id, enqueued_at=enqueued_at)
    return _dispatch_or_retry(
        self,
        WorkKind.EMBED_DOCUMENT_BATCH,
        envelope=envelope.as_task_kwargs(),
        payload={"chunk_ids": [chunk_id]},
    )


@shared_task(
    queue="embed",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def embedding_document_batch_with_bge(
    self,
    chunk_ids: list[int],
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
) -> dict:
    """
    Celery 태스크: 같은 문서의 청크 묶음을 model.encode() 한 번으로 임베딩.
    enqueue_embedding_tasks_sync가 청크 수/토큰 예산으로 미리 묶어 넘긴 chunk_ids를
    받아 처리한다. 실패 시 배치 전체를 하나의 단위로 재시도한다.
    """
    return _dispatch_or_retry(
        self,
        WorkKind.EMBED_DOCUMENT_BATCH,
        envelope=_executor_envelope(
            job_id=job_id,
            parent_job_id=parent_job_id,
            trace_id=trace_id,
            enqueued_at=enqueued_at,
            work_kind=work_kind,
            envelope_version=envelope_version,
        ),
        payload={"chunk_ids": chunk_ids},
    )


@shared_task(queue="query")
def parse_user_query(
    query: str,
    mode: str = "search",
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
) -> dict:
    """
    사용자 질의를 LLM 기반 QueryDSL 후보로 파싱하고 query_engine에서 검증/ORM 컴파일합니다.
    규칙 기반 QueryAnalyzer는 사용하지 않으며, 실패 시 원 질의를 semantic query로 보존합니다.
    """
    from document_ai.query_understanding.parser_service import parse_user_query_sync

    _ = (enqueued_at, job_id, parent_job_id, work_kind, envelope_version)
    set_trace_id(trace_id or new_trace_id())
    return parse_user_query_sync(query, mode)
