from __future__ import annotations

import json
import logging
from time import perf_counter

from django.utils import timezone

from config.enums import AIStatus
from config.tracing import get_trace_id
from document_ai.db_span import capture_db_spans
from document_ai.embedding.providers.base import EmbeddingBusyError
from document_ai.performance import datetime_delta_ms, elapsed_ms, put_metric

logger = logging.getLogger(__name__)


def _get_search_job_orm_constraints(search_job) -> dict:
    query_log = getattr(search_job, "query_log", None)
    if not query_log:
        return {}
    orm = query_log.orm or {}
    return {
        "filter_kwargs": orm.get("filter_kwargs") or {},
        "exclude_kwargs": orm.get("exclude_kwargs") or {},
        "order_by": orm.get("order_by") or [],
    }


def search_documents_sync(
    *,
    owner,
    workspace=None,
    query: str,
    top_k: int = 5,
    threshold: float | None = None,
    node_ids: list[str] | None = None,
    tuning_params: dict | None = None,
    orm_constraints: dict | None = None,
) -> tuple[list[dict], dict]:
    """Run retrieval in the request process without a Celery job boundary."""
    from document_ai.search.retriever import VectorRetriever

    started = perf_counter()
    metrics: dict = {}
    results = VectorRetriever().retrieve(
        query=query,
        top_k=top_k,
        threshold=threshold,
        node_ids=node_ids or None,
        user=owner,
        workspace=workspace,
        tuning_params=tuning_params or {},
        performance_metrics=metrics,
        **(orm_constraints or {}),
    )
    normalized_results = json.loads(json.dumps(results, default=str))
    put_metric(metrics, "request_search_ms", elapsed_ms(started))
    put_metric(metrics, "result_count", len(normalized_results))
    return normalized_results, metrics


def perform_vector_search_sync(job_id: int, *, retries: int = 0, max_retries: int = 1) -> dict:
    from document_ai.models import SearchJob

    try:
        job = SearchJob.objects.select_related("owner", "workspace", "query_log").get(pk=job_id)
    except SearchJob.DoesNotExist:
        logger.error("Search job %s not found", job_id)
        return {
            "status": "failed",
            "job_id": job_id,
            "error": f"Search job {job_id} not found",
        }

    worker_started = perf_counter()
    job.status = AIStatus.PROCESSING
    job.started_at = timezone.now()
    job.error_message = ""
    metrics = dict(job.performance_metrics or {})
    put_metric(metrics, "trace_id", get_trace_id())
    if "queue_wait_ms" not in metrics:
        put_metric(metrics, "queue_wait_ms", datetime_delta_ms(job.created_at, job.started_at))
    metrics["attempt"] = retries + 1
    job.performance_metrics = metrics
    job.save(update_fields=["status", "started_at", "error_message", "performance_metrics"])

    try:
        with capture_db_spans():
            logger.info(
                "Vector search started: job_id=%s, owner_id=%s, top_k=%s, tuning_params=%s",
                job.id,
                job.owner_id,
                job.top_k,
                job.tuning_params,
            )
            normalized_results, retrieval_metrics = search_documents_sync(
                owner=job.owner,
                workspace=job.workspace,
                query=job.query,
                top_k=job.top_k,
                threshold=job.threshold,
                node_ids=job.node_ids,
                tuning_params=job.tuning_params,
                orm_constraints=_get_search_job_orm_constraints(job),
            )

            job.results = normalized_results
            job.status = AIStatus.COMPLETED
            job.completed_at = timezone.now()
            job.error_message = ""
            metrics.update(retrieval_metrics)
            put_metric(metrics, "worker_total_ms", elapsed_ms(worker_started))
            put_metric(metrics, "end_to_end_ms", datetime_delta_ms(job.created_at, job.completed_at))
            put_metric(metrics, "result_count", len(normalized_results))
            job.performance_metrics = metrics
            job.save(update_fields=["results", "status", "completed_at", "error_message", "performance_metrics"])

            logger.info(
                "Vector search completed: job_id=%s, result_count=%s, performance=%s",
                job.id,
                len(normalized_results),
                metrics,
            )
            return {
                "status": "success",
                "job_id": job_id,
                "result_count": len(normalized_results),
            }

    except EmbeddingBusyError as exc:
        # embedding-executor is demonstrably busy with real traffic, not broken --
        # callers (e.g. RAG's _create_rag_jobs_sync) surface this as a
        # retryable 503 instead of a generic search-failed error.
        if retries < max_retries:
            raise

        job.status = AIStatus.FAILED
        job.completed_at = timezone.now()
        job.error_message = str(exc)
        put_metric(metrics, "worker_total_ms", elapsed_ms(worker_started))
        put_metric(metrics, "end_to_end_ms", datetime_delta_ms(job.created_at, job.completed_at))
        metrics["failed"] = True
        job.performance_metrics = metrics
        job.save(update_fields=["status", "completed_at", "error_message", "performance_metrics"])
        logger.warning("Vector search failed (embedding busy): job_id=%s", job_id)
        return {
            "status": "failed",
            "job_id": job_id,
            "error": str(exc),
            "error_code": "EMBEDDING_BUSY",
            "retry_after_seconds": exc.retry_after_seconds,
        }

    except Exception as exc:
        if retries < max_retries:
            raise

        job.status = AIStatus.FAILED
        job.completed_at = timezone.now()
        job.error_message = str(exc)
        put_metric(metrics, "worker_total_ms", elapsed_ms(worker_started))
        put_metric(metrics, "end_to_end_ms", datetime_delta_ms(job.created_at, job.completed_at))
        metrics["failed"] = True
        job.performance_metrics = metrics
        job.save(update_fields=["status", "completed_at", "error_message", "performance_metrics"])
        logger.exception("Vector search failed: job_id=%s", job_id)
        return {
            "status": "failed",
            "job_id": job_id,
            "error": str(exc),
        }
