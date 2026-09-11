"""Transport facade and routing contract for asynchronous AI work.

Domain callers use these entry points instead of importing Celery tasks.  The
route table is deliberately small and transport-neutral at the call site; the
current adapter remains Celery so task names, acknowledgements and retries stay
compatible with existing workers.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from django.utils import timezone

from config.tracing import get_trace_id, new_trace_id
from document_ai.services.environment import get_positive_int_env


TASK_ENVELOPE_VERSION = 1
logger = logging.getLogger(__name__)


class WorkKind(StrEnum):
    PARSE_DOCUMENT = "parse_document"
    PLAN_DOCUMENT_EMBEDDING = "plan_document_embedding"
    EMBED_DOCUMENT_BATCH = "embed_document_batch"
    EVALUATE_RETRIEVAL = "evaluate_retrieval"


@dataclass(frozen=True)
class TaskRoute:
    task_name: str
    queue: str


@dataclass(frozen=True)
class TaskEnvelope:
    """Versioned identity carried by every task published by this facade."""

    version: int
    job_id: str
    parent_job_id: str | None
    trace_id: str
    work_kind: str
    enqueued_at: str

    @classmethod
    def create(
        cls,
        kind: WorkKind,
        *,
        job_id: str | None = None,
        parent_job_id: str | None = None,
        trace_id: str | None = None,
        enqueued_at: str | None = None,
    ) -> TaskEnvelope:
        return cls(
            version=TASK_ENVELOPE_VERSION,
            job_id=job_id or str(uuid4()),
            parent_job_id=parent_job_id,
            trace_id=trace_id or get_trace_id() or new_trace_id(),
            work_kind=kind.value,
            enqueued_at=enqueued_at or timezone.now().isoformat(),
        )

    def as_task_kwargs(self) -> dict[str, Any]:
        return {
            "envelope_version": self.version,
            "job_id": self.job_id,
            "parent_job_id": self.parent_job_id,
            "trace_id": self.trace_id,
            "work_kind": self.work_kind,
            "enqueued_at": self.enqueued_at,
        }


TASK_ROUTES = MappingProxyType(
    {
        WorkKind.PARSE_DOCUMENT: TaskRoute(
            task_name="document_ai.tasks.parse_document_with_docling",
            queue="parse",
        ),
        WorkKind.PLAN_DOCUMENT_EMBEDDING: TaskRoute(
            task_name="document_ai.tasks.enqueue_embedding_tasks",
            queue="embed",
        ),
        WorkKind.EMBED_DOCUMENT_BATCH: TaskRoute(
            task_name="document_ai.tasks.embedding_document_batch_with_bge",
            queue="embed",
        ),
        WorkKind.EVALUATE_RETRIEVAL: TaskRoute(
            task_name="document_ai.tasks.run_quality_evaluation_task",
            queue="embed",
        ),
    }
)


def get_task_route(kind: WorkKind) -> TaskRoute:
    return TASK_ROUTES[kind]


def derive_job_id(parent_job_id: str, kind: WorkKind, target: str) -> str:
    """Return a stable child identity across parent-message redelivery."""

    return str(uuid5(NAMESPACE_URL, f"dotori:{parent_job_id}:{kind.value}:{target}"))


def _dispatch(task, kind: WorkKind, *, args: list[Any], kwargs: dict[str, Any]):
    route = get_task_route(kind)
    envelope_keys = {
        "job_id",
        "parent_job_id",
        "trace_id",
        "enqueued_at",
        "work_kind",
        "envelope_version",
    }
    envelope_values = {
        key: kwargs.pop(key)
        for key in tuple(kwargs)
        if key in envelope_keys
    }
    supplied_kind = envelope_values.get("work_kind")
    if supplied_kind is not None and supplied_kind != kind.value:
        raise ValueError(
            f"work_kind {supplied_kind!r} does not match dispatch kind {kind.value!r}"
        )
    supplied_version = envelope_values.get("envelope_version")
    if supplied_version is not None and supplied_version != TASK_ENVELOPE_VERSION:
        raise ValueError(
            f"Unsupported task envelope version: {supplied_version!r}"
        )

    envelope = TaskEnvelope.create(
        kind,
        job_id=envelope_values.get("job_id"),
        parent_job_id=envelope_values.get("parent_job_id"),
        trace_id=envelope_values.get("trace_id"),
        enqueued_at=envelope_values.get("enqueued_at"),
    )
    return task.apply_async(
        args=args,
        kwargs={**kwargs, **envelope.as_task_kwargs()},
        queue=route.queue,
        task_id=envelope.job_id,
    )


def enqueue_parse(node_id: int, **kwargs: Any):
    from document_ai.tasks import parse_document_with_docling

    return _dispatch(
        parse_document_with_docling,
        WorkKind.PARSE_DOCUMENT,
        args=[node_id],
        kwargs=kwargs,
    )


def enqueue_embedding(node_id: int, **kwargs: Any):
    from document_ai.tasks import enqueue_embedding_tasks

    return _dispatch(
        enqueue_embedding_tasks,
        WorkKind.PLAN_DOCUMENT_EMBEDDING,
        args=[node_id],
        kwargs=kwargs,
    )


def enqueue_embedding_batch(chunk_ids: list[int], **kwargs: Any):
    from document_ai.tasks import embedding_document_batch_with_bge

    return _dispatch(
        embedding_document_batch_with_bge,
        WorkKind.EMBED_DOCUMENT_BATCH,
        args=[chunk_ids],
        kwargs=kwargs,
    )


def enqueue_retrieval_evaluation(run_uid: str, **kwargs: Any):
    from document_ai.tasks import run_quality_evaluation_task

    return _dispatch(
        run_quality_evaluation_task,
        WorkKind.EVALUATE_RETRIEVAL,
        args=[run_uid],
        kwargs=kwargs,
    )


def broker_redis_client():
    from redis import Redis

    redis_url = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    return Redis.from_url(
        redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def embedding_queue_backpressure() -> tuple[bool, int | None, int]:
    """Return whether parsing should pause behind the durable embed backlog.

    This is a cross-queue orchestration policy rather than embedding business
    logic. Broker inspection deliberately fails open: Celery delivery remains
    available even when the optional backlog observation is unavailable.
    """

    limit = get_positive_int_env(
        "DOCUMENT_AI_EMBED_QUEUE_BACKPRESSURE_LIMIT",
        32,
    )
    try:
        client = broker_redis_client()
        depth = int(client.llen(get_task_route(WorkKind.PLAN_DOCUMENT_EMBEDDING).queue))
    except Exception as exc:
        logger.warning(
            "Unable to inspect embed queue depth; parse backpressure is temporarily disabled: %s",
            exc,
        )
        return False, None, limit
    return depth >= limit, depth, limit
