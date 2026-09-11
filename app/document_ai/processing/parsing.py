from __future__ import annotations

import logging
from datetime import datetime
from time import perf_counter
from typing import TYPE_CHECKING, Callable

from django.db import transaction
from django.utils import timezone

from config.enums import AIStatus
from config.tracing import get_trace_id, new_trace_id, set_trace_id
from document_ai.db_span import capture_db_spans
from document_ai.models import DocumentChunk, DocumentParseResult
from document_ai.parsers.config import (
    get_chunk_max_tokens,
    get_embedding_backend,
    get_embedding_max_tokens,
    get_embedding_model,
    get_parser_tokenizer_id,
)
from document_ai.parsers.text_utils import normalize_extracted_text, serialize_meta
from document_ai.performance import datetime_delta_ms, elapsed_ms, put_metric
from document_ai.tracing_utils import put_task_identity

if TYPE_CHECKING:
    from document_ai.parsers.schema import ParseResult


logger = logging.getLogger(__name__)


class ParseExecutionFailed(Exception):
    """A persisted parse failure for the transport adapter to retry or return."""

    def __init__(self, *, cause: Exception, result: dict):
        super().__init__(str(cause))
        self.cause = cause
        self.result = result


def get_parser_for_execution():
    """Resolve the configured parser without coupling callers to its registry."""
    from document_ai.parsers.registry import get_document_parser

    return get_document_parser()


def save_parse_result(node, pr: ParseResult) -> DocumentParseResult:
    """Persist a parser result and atomically replace the node's chunks."""
    from document_ai.services.executor_snapshot import validate_execution
    validate_execution()
    metadata = {
        "parser_backend": pr.parser_backend,
        "parser_version": pr.parser_version,
        "tokenizer_name": get_parser_tokenizer_id(),
        "chunk_max_tokens": get_chunk_max_tokens(),
        "embedding_max_tokens": get_embedding_max_tokens(),
        "embedding_backend": get_embedding_backend(),
        "file_ext": pr.file_ext,
    }
    metadata = {key: value for key, value in metadata.items() if value is not None}

    raw_status = (pr.status or "").lower()
    parse_status = AIStatus.FAILED
    if raw_status in {"success", "ok", "done"}:
        parse_status = AIStatus.COMPLETED
    elif raw_status in {"failed", "error"}:
        parse_status = AIStatus.FAILED
    elif pr.chunks and not pr.errors:
        parse_status = AIStatus.COMPLETED

    with transaction.atomic():
        doc_result, _ = DocumentParseResult.objects.update_or_create(
            node=node,
            defaults={
                "parser_name": pr.parser_backend,
                "parser_mode": pr.parser_mode or "",
                "status": parse_status,
                "input_format": pr.input_format or "",
                "input_document_hash": pr.input_document_hash or "",
                "input_page_count": pr.input_page_count,
                "result_page_count": pr.page_count,
                "chunk_count": len(pr.chunks),
                "timings": pr.timings or {},
                "errors": pr.errors or [],
                "parsed_at": timezone.now(),
                "metadata": metadata,
            },
        )

        doc_result.chunks.all().delete()
        chunk_objects = []
        for chunk in pr.chunks:
            raw_meta = chunk.meta or {}
            chunk_objects.append(
                DocumentChunk(
                    parse_result=doc_result,
                    chunk_index=chunk.chunk_index,
                    text=normalize_extracted_text(chunk.serialized_text),
                    token_count=chunk.tokens,
                    section_title=_extract_section_title(raw_meta),
                    page_from=_extract_page(raw_meta, "page_from"),
                    page_to=_extract_page(raw_meta, "page_to"),
                    chunk_meta=serialize_meta(raw_meta) or {},
                )
            )

        DocumentChunk.objects.bulk_create(chunk_objects)
        actual_chunk_count = len(chunk_objects)
        expected_chunk_count = len(pr.chunks)
        if actual_chunk_count != expected_chunk_count:
            raise ValueError(
                f"Chunk count mismatch: expected {expected_chunk_count}, got {actual_chunk_count}"
            )

        logger.info(
            "Chunks saved: node_id=%s, parse_result_id=%s, status=%s, chunks=%s, parser_mode=%s",
            node.id,
            doc_result.id,
            parse_status,
            actual_chunk_count,
            pr.parser_mode or "",
        )
        return doc_result


def _extract_section_title(meta: dict) -> str:
    headings = meta.get("headings", [])
    if headings:
        return " > ".join(headings)
    return ""


def _extract_page(meta: dict, key: str) -> int | None:
    # `key` remains part of the compatibility signature used by the old helper.
    _ = key
    doc_items = meta.get("doc_items", [])
    if doc_items:
        prov = doc_items[0].get("prov", [])
        if prov:
            page = prov[0].get("page_no")
            if page is not None:
                return page
    return None


def execute_parse_node(
    node_id: int,
    *,
    parser,
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
    save_result=save_parse_result,
    on_success: Callable[[], None] | None = None,
    on_persisted: Callable[[dict], None] | None = None,
) -> dict:
    """Execute and persist one parse, independent of its queue transport."""
    from files.models import Node

    set_trace_id(trace_id or new_trace_id())
    task_start = timezone.now()
    worker_started = perf_counter()
    enqueued_dt = datetime.fromisoformat(enqueued_at) if enqueued_at else None

    try:
        with capture_db_spans():
            try:
                node = Node.objects.select_related("blob").get(pk=node_id)
                if node.node_type != "file":
                    raise ValueError(f"Node {node_id} is not a file")
                if not node.ai_processing_enabled:
                    logger.info("Parse skipped: node_id=%s, reason=ai_processing_disabled", node_id)
                    return {
                        "status": "skipped",
                        "node_id": node_id,
                        "message": "AI processing is disabled",
                    }
                if not hasattr(node, "blob") or not node.blob.file:
                    raise ValueError(f"Node {node_id} has no attached file blob")

                logger.info("Parse started: node_id=%s", node_id)
                parse_result = parser.parse(node.blob.file.path)

                node.refresh_from_db(fields=["ai_processing_enabled"])
                if not node.ai_processing_enabled:
                    logger.info(
                        "Parse result discarded: node_id=%s, reason=ai_processing_disabled_after_parse",
                        node_id,
                    )
                    return {
                        "status": "skipped",
                        "node_id": node_id,
                        "message": "AI processing was disabled during parsing",
                    }

                with transaction.atomic():
                    doc_result = save_result(node, parse_result)
                    metrics = dict(doc_result.performance_metrics or {})
                    put_metric(metrics, "trace_id", get_trace_id())
                    put_task_identity(
                        metrics, job_id=job_id, parent_job_id=parent_job_id,
                        work_kind=work_kind, envelope_version=envelope_version,
                    )
                    put_metric(metrics, "queue_wait_ms", datetime_delta_ms(enqueued_dt, task_start))
                    put_metric(metrics, "parse_processing_ms", elapsed_ms(worker_started))
                    doc_result.performance_metrics = metrics
                    doc_result.save(update_fields=["performance_metrics"])
                    if on_persisted is not None:
                        on_persisted({"status": "success", "node_id": node_id, "chunk_count": doc_result.chunk_count})
            except Node.DoesNotExist:
                logger.error("Node %s not found", node_id)
                return {"status": "failed", "error": f"Node {node_id} not found"}

        if on_success is not None:
            on_success()

        logger.info(
            "Parse completed: node_id=%s, status=%s, chunks=%s, parser_mode=%s",
            node_id,
            doc_result.status,
            doc_result.chunk_count,
            parse_result.parser_mode or "",
        )
        return {
            "status": "success",
            "node_id": node_id,
            "chunk_count": doc_result.chunk_count,
        }
    except Exception as exc:
        from document_ai.services.executor_snapshot import StaleExecution
        if isinstance(exc, StaleExecution):
            raise
        logger.exception("파싱 실패: node_id=%s", node_id)
        failure_metrics = {}
        put_metric(failure_metrics, "trace_id", get_trace_id())
        put_task_identity(
            failure_metrics,
            job_id=job_id,
            parent_job_id=parent_job_id,
            work_kind=work_kind,
            envelope_version=envelope_version,
        )
        put_metric(failure_metrics, "queue_wait_ms", datetime_delta_ms(enqueued_dt, task_start))
        put_metric(failure_metrics, "parse_processing_ms", elapsed_ms(worker_started))
        failure_metrics["failed"] = True

        DocumentParseResult.objects.update_or_create(
            node_id=node_id,
            defaults={
                "parser_name": parser.spec.backend,
                "status": AIStatus.FAILED,
                "errors": [{"message": str(exc)}],
                "performance_metrics": failure_metrics,
                "metadata": {
                    "tokenizer_name": get_embedding_model(),
                    "chunk_max_tokens": get_chunk_max_tokens(),
                    "embedding_max_tokens": get_embedding_max_tokens(),
                    "embedding_backend": get_embedding_backend(),
                },
                "parsed_at": timezone.now(),
            },
        )
        raise ParseExecutionFailed(
            cause=exc,
            result={"status": "failed", "node_id": node_id, "error": str(exc)},
        ) from exc
