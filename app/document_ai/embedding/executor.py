"""Document embedding job execution and persistence."""

from __future__ import annotations

import logging
from datetime import datetime
from time import perf_counter

from django.db import transaction
from django.utils import timezone

from config.enums import AIStatus
from config.tracing import get_trace_id, new_trace_id, set_trace_id
from document_ai.db_span import capture_db_spans
from document_ai.models import DocumentChunk, DocumentParseResult
from document_ai.parsers.config import (
    get_embedding_backend,
    get_embedding_document_batch_max_chunks,
    get_embedding_document_batch_max_tokens,
    get_embedding_max_tokens,
    get_embedding_model,
    get_hf_tokenizer,
    get_raw_tokenizer,
)
from document_ai.parsers.text_utils import normalize_extracted_text
from document_ai.performance import datetime_delta_ms, elapsed_ms, put_metric
from document_ai.tracing_utils import enqueue_kwargs, put_task_identity

logger = logging.getLogger(__name__)


class RetryableEmbeddingError(Exception):
    pass


class EmbeddingDispatchError(Exception):
    """Raised when one or more claimed chunks could not be published."""

    def __init__(self, message: str, *, unpublished_chunk_ids: list[int]):
        super().__init__(message)
        self.unpublished_chunk_ids = unpublished_chunk_ids


def truncate_embedding_input_text(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        raise ValueError("Embedding max_tokens must be greater than zero.")

    raw_tokenizer = get_raw_tokenizer()
    token_ids = raw_tokenizer.encode(text, add_special_tokens=False)
    if len(token_ids) <= max_tokens:
        return text

    truncated_ids = token_ids[:max_tokens]
    truncated_text = raw_tokenizer.decode(
        truncated_ids,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=True,
    ).strip()
    return truncated_text or text


def prepare_embedding_input_text(text: str, *, chunk=None) -> tuple[str, int, bool]:
    max_tokens = get_embedding_max_tokens()
    tokenizer = get_hf_tokenizer()
    token_count = tokenizer.count_tokens(text)
    if token_count > max_tokens:
        chunk_label = f"chunk_id={chunk.id}, " if chunk is not None else ""
        truncated_text = truncate_embedding_input_text(text, max_tokens)
        truncated_token_count = tokenizer.count_tokens(truncated_text)
        logger.warning(
            "Embedding input truncated: %stokens=%s, truncated_tokens=%s, max_tokens=%s",
            chunk_label,
            token_count,
            truncated_token_count,
            max_tokens,
        )
        return truncated_text, truncated_token_count, True
    return text, token_count, False


def _group_chunks_into_batches(chunk_rows: list[tuple[int, int | None]]) -> list[list[int]]:
    """Greedily group a document's chunk ids into micro-batches, bounded by
    both count and total token budget. Chunks arrive pre-ordered by
    chunk_index; a chunk with no token_count only counts against the count
    cap. One chunk that alone exceeds the token budget still gets its own
    (size-1) batch rather than being rejected.
    """
    max_chunks = get_embedding_document_batch_max_chunks()
    max_tokens = get_embedding_document_batch_max_tokens()

    batches: list[list[int]] = []
    current_batch: list[int] = []
    current_tokens = 0

    for chunk_id, token_count in chunk_rows:
        estimated_tokens = token_count or 0
        would_exceed_tokens = current_batch and (current_tokens + estimated_tokens) > max_tokens
        would_exceed_count = len(current_batch) >= max_chunks
        if current_batch and (would_exceed_tokens or would_exceed_count):
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0

        current_batch.append(chunk_id)
        current_tokens += estimated_tokens

    if current_batch:
        batches.append(current_batch)

    return batches


def enqueue_embedding_tasks_sync(
    node_id: int,
    *,
    trace_id: str | None = None,
    parent_job_id: str | None = None,
    dispatch_batch=None,
) -> dict:
    """Claim pending chunks and publish bounded embedding batches.

    Database selection and state transitions remain in the embedding domain.
    Queue transport is injected by the Celery adapter; the lazy default keeps
    direct callers compatible without importing Celery tasks from this module.
    """
    try:
        with transaction.atomic():
            from files.models import Node

            if not Node.objects.filter(pk=node_id, ai_processing_enabled=True).exists():
                logger.info("Embedding queue skipped: node_id=%s, reason=ai_processing_disabled_or_missing", node_id)
                return {
                    "status": "skipped",
                    "node_id": node_id,
                    "chunk_count": 0,
                    "message": "AI processing is disabled or node is missing",
                }

            chunk_rows = list(
                DocumentChunk.objects
                .select_for_update(skip_locked=True)
                .filter(
                    parse_result__node_id=node_id,
                    parse_result__node__ai_processing_enabled=True,
                    status=AIStatus.PENDING,
                )
                .order_by("chunk_index")
                .values_list("id", "token_count")
            )

            if not chunk_rows:
                logger.info("Embedding queue skipped: node_id=%s, reason=no_pending_chunks", node_id)
                return {
                    "status": "success",
                    "node_id": node_id,
                    "chunk_count": 0,
                    "message": "No pending chunks found",
                }

            chunk_ids = [chunk_id for chunk_id, _ in chunk_rows]
            DocumentChunk.objects.filter(id__in=chunk_ids).update(
                status=AIStatus.PROCESSING
            )

        if dispatch_batch is None:
            from document_ai.orchestration import enqueue_embedding_batch

            dispatch_batch = enqueue_embedding_batch

        batches = _group_chunks_into_batches(chunk_rows)
        for batch_index, batch in enumerate(batches):
            try:
                dispatch_batch(
                    batch,
                    parent_job_id=parent_job_id,
                    **enqueue_kwargs(trace_id=trace_id),
                )
            except Exception as exc:
                unpublished_chunk_ids = [
                    chunk_id
                    for unpublished_batch in batches[batch_index:]
                    for chunk_id in unpublished_batch
                ]
                DocumentChunk.objects.filter(
                    id__in=unpublished_chunk_ids,
                    status=AIStatus.PROCESSING,
                ).update(status=AIStatus.PENDING)
                raise EmbeddingDispatchError(
                    f"Embedding batch publish failed for node {node_id}: {exc}",
                    unpublished_chunk_ids=unpublished_chunk_ids,
                ) from exc

        logger.info(
            "Embedding queued: node_id=%s, chunks=%s",
            node_id,
            len(chunk_ids),
        )

        return {
            "status": "success",
            "node_id": node_id,
            "chunk_count": len(chunk_ids),
        }

    except EmbeddingDispatchError:
        logger.exception("bge-embedding batch publish failed: node_id=%s", node_id)
        raise
    except Exception as exc:
        logger.exception("bge-embedding queueing failed: node_id=%s", node_id)
        return {
            "status": "failed",
            "node_id": node_id,
            "error": str(exc),
        }


def _mark_embedding_failed(
    chunk, *, embedding_model: str, embedding_backend: str, error_message: str, performance_metrics: dict
) -> None:
    from document_ai.embedding.store_registry import get_embedding_store_instance

    store = get_embedding_store_instance(
        model_name=embedding_model,
        backend=embedding_backend,
    )
    store.mark_chunk_embedding_failed(chunk=chunk, error_message=error_message)

    chunk.status = AIStatus.FAILED
    chunk.error_message = error_message
    chunk.performance_metrics = performance_metrics
    chunk.save(update_fields=["status", "error_message", "performance_metrics"])


def _log_if_document_ready(node_id: int) -> None:
    """Emit one "document ready" event the moment a node's last chunk leaves
    PENDING/PROCESSING/FAILED -- there is otherwise no log line marking when a
    file became fully searchable. Concurrent chunk completions on the same
    node can each observe zero remaining and both log; harmless for a log
    line, not worth a lock."""
    chunks = DocumentChunk.objects.filter(parse_result__node_id=node_id)
    total = chunks.count()
    if total == 0:
        return
    remaining = chunks.exclude(status=AIStatus.COMPLETED).exists()
    if remaining:
        return
    from document_ai.services.embedding_runtime_config import (
        get_active_embedding_runtime,
    )

    runtime = get_active_embedding_runtime()
    DocumentParseResult.objects.filter(node_id=node_id).update(
        embedding_generation_id=runtime.generation_id,
        embedding_runtime_fingerprint=runtime.runtime_fingerprint,
        updated_at=timezone.now(),
    )
    logger.info("Document ready: node_id=%s, chunks=%s", node_id, total)


def embed_document_chunk_sync(
    chunk_id: int,
    *,
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    retries: int = 0,
    max_retries: int = 3,
) -> dict:
    set_trace_id(trace_id or new_trace_id())
    task_start = timezone.now()
    worker_started = perf_counter()
    enqueued_dt = datetime.fromisoformat(enqueued_at) if enqueued_at else None

    def build_metrics(chunk) -> dict:
        metrics = dict(chunk.performance_metrics or {})
        put_metric(metrics, "trace_id", get_trace_id())
        put_metric(metrics, "queue_wait_ms", datetime_delta_ms(enqueued_dt, task_start))
        put_metric(metrics, "embedding_processing_ms", elapsed_ms(worker_started))
        return metrics

    embedding_model = get_embedding_model()
    embedding_backend = get_embedding_backend()

    try:
        chunk = DocumentChunk.objects.select_related("parse_result__node").get(pk=chunk_id)
    except DocumentChunk.DoesNotExist:
        logger.error("Chunk %s not found", chunk_id)
        return {
            "status": "failed",
            "chunk_id": chunk_id,
            "error": f"Chunk {chunk_id} not found",
        }

    if not chunk.parse_result.node.ai_processing_enabled:
        logger.info(
            "Embedding skipped: chunk_id=%s, node_id=%s, reason=ai_processing_disabled",
            chunk_id,
            chunk.parse_result.node_id,
        )
        if chunk.status == AIStatus.PROCESSING:
            chunk.status = AIStatus.PENDING
            chunk.error_message = {}
            chunk.save(update_fields=["status", "error_message"])
        return {
            "status": "skipped",
            "chunk_id": chunk_id,
            "node_id": chunk.parse_result.node_id,
            "message": "AI processing is disabled",
        }

    if chunk.status != AIStatus.PROCESSING:
        logger.warning("Chunk %s skipped: %s", chunk_id, chunk.status)
        return {
            "status": "skipped",
            "chunk_id": chunk_id,
            "message": f"Chunk {chunk_id} is not valid state",
        }

    try:
        with capture_db_spans():
            text = normalize_extracted_text(chunk.text or "")
            if not text:
                raise ValueError("Chunk text is empty")
            text, input_token_count, input_truncated = prepare_embedding_input_text(text, chunk=chunk)

            logger.info(
                "Embedding started: chunk_id=%s, node_id=%s, chunk_index=%s, stored_tokens=%s, input_tokens=%s, max_tokens=%s, truncated=%s, model=%s, backend=%s",
                chunk_id,
                chunk.parse_result.node_id,
                chunk.chunk_index,
                chunk.token_count,
                input_token_count,
                get_embedding_max_tokens(),
                input_truncated,
                embedding_model,
                embedding_backend,
            )

            from document_ai.embedding.internal_views import run_embedding_with_admission

            embedding = run_embedding_with_admission(
                "document", text=text
            )[0]

            from document_ai.embedding.store_registry import get_embedding_store_instance

            store = get_embedding_store_instance(
                model_name=embedding_model,
                backend=embedding_backend,
            )
            store.save_chunk_embedding(chunk=chunk, embedding=embedding, status=AIStatus.COMPLETED)

            chunk.status = AIStatus.COMPLETED
            chunk.error_message = ""
            chunk.performance_metrics = build_metrics(chunk)
            chunk.save(update_fields=["status", "error_message", "performance_metrics"])

            _log_if_document_ready(chunk.parse_result.node_id)

            logger.info(
                "Embedding completed: chunk_id=%s, node_id=%s, dense_dim=%s, sparse_terms=%s, model=%s, backend=%s",
                chunk_id,
                chunk.parse_result.node_id,
                len(embedding.dense_vector),
                len(embedding.sparse_vector),
                embedding_model,
                embedding_backend,
            )

            return {
                "status": "success",
                "chunk_id": chunk_id,
            }

    except ValueError as exc:
        logger.warning("Embedding validation failed: chunk_id=%s, error=%s", chunk_id, exc)
        _mark_embedding_failed(
            chunk,
            embedding_model=embedding_model,
            embedding_backend=embedding_backend,
            error_message=str(exc),
            performance_metrics=build_metrics(chunk),
        )
        return {
            "status": "failed",
            "chunk_id": chunk_id,
            "error": str(exc),
        }

    except RuntimeError as exc:
        error_message = str(exc)
        if "GPU OOM" in error_message:
            logger.warning(
                "Embedding GPU OOM: chunk_id=%s, retries=%s/%s, error=%s",
                chunk_id,
                retries,
                max_retries,
                error_message,
            )
        else:
            logger.warning(
                "Embedding runtime error: chunk_id=%s, retries=%s/%s, error=%s",
                chunk_id,
                retries,
                max_retries,
                error_message,
            )

        if retries < max_retries:
            raise RetryableEmbeddingError(error_message) from exc

        logger.exception("Embedding permanently failed: chunk_id=%s", chunk_id)
        _mark_embedding_failed(
            chunk,
            embedding_model=embedding_model,
            embedding_backend=embedding_backend,
            error_message=error_message,
            performance_metrics=build_metrics(chunk),
        )
        return {
            "status": "failed",
            "chunk_id": chunk_id,
            "error": error_message,
        }

    except Exception as exc:
        logger.warning(
            "Embedding failed (retrying): chunk_id=%s, retries=%s/%s, error=%s",
            chunk_id,
            retries,
            max_retries,
            exc,
        )

        if retries < max_retries:
            raise RetryableEmbeddingError(str(exc)) from exc

        logger.exception("Embedding permanently failed: chunk_id=%s", chunk_id)
        _mark_embedding_failed(
            chunk,
            embedding_model=embedding_model,
            embedding_backend=embedding_backend,
            error_message=str(exc),
            performance_metrics=build_metrics(chunk),
        )
        return {
            "status": "failed",
            "chunk_id": chunk_id,
            "error": str(exc),
        }


def embed_document_chunks_batch_sync(
    chunk_ids: list[int],
    *,
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
    retries: int = 0,
    max_retries: int = 3,
) -> dict:
    """Embed a same-document micro-batch of chunks with one model.encode()
    call. chunk_ids is pre-grouped by _group_chunks_into_batches (count +
    token budget bounded). A single chunk with empty text is filtered out and
    marked failed immediately rather than blocking the rest of the batch; any
    other failure (OOM, remote busy, etc.) retries the whole batch as one unit
    via RetryableEmbeddingError, same as the single-chunk path.
    """
    set_trace_id(trace_id or new_trace_id())
    task_start = timezone.now()
    worker_started = perf_counter()
    enqueued_dt = datetime.fromisoformat(enqueued_at) if enqueued_at else None

    def build_metrics(chunk) -> dict:
        metrics = dict(chunk.performance_metrics or {})
        put_metric(metrics, "trace_id", get_trace_id())
        put_task_identity(
            metrics,
            job_id=job_id,
            parent_job_id=parent_job_id,
            work_kind=work_kind,
            envelope_version=envelope_version,
        )
        put_metric(metrics, "queue_wait_ms", datetime_delta_ms(enqueued_dt, task_start))
        put_metric(metrics, "embedding_processing_ms", elapsed_ms(worker_started))
        return metrics

    embedding_model = get_embedding_model()
    embedding_backend = get_embedding_backend()

    chunks_by_id = {
        chunk.id: chunk
        for chunk in DocumentChunk.objects.select_related("parse_result__node").filter(pk__in=chunk_ids)
    }

    skipped: list[dict] = []
    failed: list[dict] = []
    valid_chunks = []
    texts: list[str] = []

    for chunk_id in chunk_ids:
        chunk = chunks_by_id.get(chunk_id)
        if chunk is None:
            logger.error("Chunk %s not found", chunk_id)
            failed.append({"chunk_id": chunk_id, "error": f"Chunk {chunk_id} not found"})
            continue

        if not chunk.parse_result.node.ai_processing_enabled:
            logger.info(
                "Embedding skipped: chunk_id=%s, node_id=%s, reason=ai_processing_disabled",
                chunk_id,
                chunk.parse_result.node_id,
            )
            if chunk.status == AIStatus.PROCESSING:
                chunk.status = AIStatus.PENDING
                chunk.error_message = {}
                chunk.save(update_fields=["status", "error_message"])
            skipped.append({"chunk_id": chunk_id, "message": "AI processing is disabled"})
            continue

        if chunk.status != AIStatus.PROCESSING:
            logger.warning("Chunk %s skipped: %s", chunk_id, chunk.status)
            skipped.append({"chunk_id": chunk_id, "message": f"Chunk {chunk_id} is not valid state"})
            continue

        text = normalize_extracted_text(chunk.text or "")
        if not text:
            # One empty chunk must not sink the rest of the batch's model
            # call -- fail it immediately and exclude it, same ValueError
            # semantics as the single-chunk path's permanent failure.
            logger.warning("Embedding validation failed: chunk_id=%s, error=Chunk text is empty", chunk_id)
            _mark_embedding_failed(
                chunk,
                embedding_model=embedding_model,
                embedding_backend=embedding_backend,
                error_message="Chunk text is empty",
                performance_metrics=build_metrics(chunk),
            )
            failed.append({"chunk_id": chunk_id, "error": "Chunk text is empty"})
            continue

        text, _input_token_count, _input_truncated = prepare_embedding_input_text(text, chunk=chunk)
        valid_chunks.append(chunk)
        texts.append(text)

    if not valid_chunks:
        return {
            "status": "skipped" if skipped and not failed else ("success" if not failed else "partial"),
            "chunk_ids": chunk_ids,
            "count": 0,
            "skipped": skipped,
            "failed": failed,
        }

    logger.info(
        "Batch embedding started: chunk_ids=%s, count=%s, model=%s, backend=%s",
        [chunk.id for chunk in valid_chunks],
        len(valid_chunks),
        embedding_model,
        embedding_backend,
    )

    try:
        with capture_db_spans():
            from document_ai.embedding.internal_views import run_embedding_with_admission

            embeddings = run_embedding_with_admission("document", texts=texts)
            if len(embeddings) != len(valid_chunks):
                raise RuntimeError("Embedding result count does not match input count")

            from document_ai.embedding.store_registry import get_embedding_store_instance

            store = get_embedding_store_instance(
                model_name=embedding_model,
                backend=embedding_backend,
            )

            from document_ai.services.executor_snapshot import validate_execution
            with transaction.atomic():
                succeeded: list[int] = []
                for chunk, embedding in zip(valid_chunks, embeddings):
                    store.save_chunk_embedding(chunk=chunk, embedding=embedding, status=AIStatus.COMPLETED)
                    chunk.status = AIStatus.COMPLETED
                    chunk.error_message = ""
                    chunk.performance_metrics = build_metrics(chunk)
                    chunk.save(update_fields=["status", "error_message", "performance_metrics"])
                    succeeded.append(chunk.id)

                for node_id in {chunk.parse_result.node_id for chunk in valid_chunks}:
                    _log_if_document_ready(node_id)
                validate_execution()

        logger.info(
            "Batch embedding completed: chunk_ids=%s, model=%s, backend=%s",
            succeeded,
            embedding_model,
            embedding_backend,
        )

        return {
            "status": "success" if not (skipped or failed) else "partial",
            "chunk_ids": chunk_ids,
            "count": len(succeeded),
            "skipped": skipped,
            "failed": failed,
        }

    except RuntimeError as exc:
        error_message = str(exc)
        valid_ids = [chunk.id for chunk in valid_chunks]
        if "GPU OOM" in error_message:
            logger.warning(
                "Batch embedding GPU OOM: chunk_ids=%s, retries=%s/%s, error=%s",
                valid_ids, retries, max_retries, error_message,
            )
        else:
            logger.warning(
                "Batch embedding runtime error: chunk_ids=%s, retries=%s/%s, error=%s",
                valid_ids, retries, max_retries, error_message,
            )

        if retries < max_retries:
            raise RetryableEmbeddingError(error_message) from exc

        logger.exception("Batch embedding permanently failed: chunk_ids=%s", valid_ids)
        for chunk in valid_chunks:
            _mark_embedding_failed(
                chunk,
                embedding_model=embedding_model,
                embedding_backend=embedding_backend,
                error_message=error_message,
                performance_metrics=build_metrics(chunk),
            )
            failed.append({"chunk_id": chunk.id, "error": error_message})

        return {
            "status": "failed",
            "chunk_ids": chunk_ids,
            "count": 0,
            "skipped": skipped,
            "failed": failed,
        }

    except Exception as exc:
        valid_ids = [chunk.id for chunk in valid_chunks]
        from document_ai.services.executor_snapshot import StaleExecution
        if isinstance(exc, StaleExecution):
            raise
        logger.warning(
            "Batch embedding failed (retrying): chunk_ids=%s, retries=%s/%s, error=%s",
            valid_ids, retries, max_retries, exc,
        )

        if retries < max_retries:
            raise RetryableEmbeddingError(str(exc)) from exc

        logger.exception("Batch embedding permanently failed: chunk_ids=%s", valid_ids)
        for chunk in valid_chunks:
            _mark_embedding_failed(
                chunk,
                embedding_model=embedding_model,
                embedding_backend=embedding_backend,
                error_message=str(exc),
                performance_metrics=build_metrics(chunk),
            )
            failed.append({"chunk_id": chunk.id, "error": str(exc)})

        return {
            "status": "failed",
            "chunk_ids": chunk_ids,
            "count": 0,
            "skipped": skipped,
            "failed": failed,
        }
