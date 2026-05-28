from __future__ import annotations

import logging
import json
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q

from document_ai.models import ChunkEmbedding, DocumentParseResult, DocumentChunk
from document_ai.parsers.config import (
    get_chunk_max_tokens,
    get_embedding_backend,
    get_embedding_max_tokens,
    get_embedding_model,
)
from document_ai.parsers.text_utils import normalize_extracted_text, serialize_meta

from config.enums import AIStatus

if TYPE_CHECKING:
    from document_ai.parsers.docling_parser import ParseResult

logger = logging.getLogger(__name__)


def _json_safe(value):
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _get_llm_message_content(payload: dict) -> str:
    choice = (payload.get("choices") or [{}])[0]
    message_content = choice.get("message", {}).get("content", "")
    if isinstance(message_content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in message_content
        ).strip()
    return str(message_content or choice.get("text", "") or "").strip()


def _strip_llm_control_tokens(text: str) -> str:
    cleaned = re.sub(r"<\|[^>]+?\|>", "", text or "").strip()
    cleaned = re.sub(r"^```(?:[a-zA-Z]+)?\s*|\s*```$", "", cleaned).strip()
    return cleaned


def _extract_llm_final_content(text: str, *, fallback_to_raw: bool = True) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""

    final_markers = [
        "<|channel>final",
        "<channel|>",
        "<|final|>",
        "Final Output:",
        "Final Output",
        "최종 답변:",
        "답변:",
    ]
    for marker in final_markers:
        if marker in raw:
            return _strip_llm_control_tokens(raw.split(marker, 1)[1])

    if "<|channel>thought" in raw or "<|think" in raw or "<think>" in raw:
        return ""

    if not fallback_to_raw:
        return ""
    raw = re.sub(r"<\|channel\>thought[\s\S]*?(?=<\|channel\>final|$)", "", raw).strip()
    raw = re.sub(r"<think>[\s\S]*?(?=</think>|$)", "", raw).strip()
    return _strip_llm_control_tokens(raw)


def _get_positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default

    if value < 1:
        logger.warning("%s must be >= 1, falling back to %s", name, default)
        return default

    return value


def _get_recovery_stale_minutes() -> int:
    return _get_positive_int_env("DOCUMENT_AI_RECOVERY_STALE_MINUTES", 30)


def _get_recovery_parse_batch_size() -> int:
    return _get_positive_int_env("DOCUMENT_AI_RECOVERY_PARSE_BATCH_SIZE", 50)


def _get_recovery_embedding_batch_size() -> int:
    return _get_positive_int_env("DOCUMENT_AI_RECOVERY_EMBED_BATCH_SIZE", 200)


def _get_max_recovery_attempts() -> int:
    return _get_positive_int_env("DOCUMENT_AI_MAX_RECOVERY_ATTEMPTS", 5)


def _recovery_cutoff():
    return timezone.now() - timedelta(minutes=_get_recovery_stale_minutes())


def _get_parse_recovery_node_ids(limit: int) -> list[int]:
    from files.models import Node

    cutoff = _recovery_cutoff()
    max_attempts = _get_max_recovery_attempts()

    # parse_result 가 없는 신규 파일은 attempt 제한 없이 항상 포함
    # parse_result 가 있는 파일은 staleness + attempt 한계 조건 모두 적용
    node_ids = set(
        Node.objects.select_related("blob", "parse_result")
        .filter(
            node_type="file",
            trashed=False,
            blob__isnull=False,
        )
        .filter(
            Q(parse_result__isnull=True)
            | Q(
                parse_result__status=AIStatus.FAILED,
                parse_result__updated_at__lte=cutoff,
                parse_result__recovery_attempts__lt=max_attempts,
            )
            | Q(
                parse_result__status=AIStatus.PENDING,
                parse_result__updated_at__lte=cutoff,
                parse_result__recovery_attempts__lt=max_attempts,
            )
            | Q(
                parse_result__status=AIStatus.PROCESSING,
                parse_result__updated_at__lte=cutoff,
                parse_result__recovery_attempts__lt=max_attempts,
            )
        )
        .order_by("id")
        .values_list("id", flat=True)
    )
    chunk_gap_ids = DocumentParseResult.objects.filter(
        node__trashed=False,
        node__blob__isnull=False,
        status=AIStatus.COMPLETED,
        updated_at__lte=cutoff,
        recovery_attempts__lt=max_attempts,
    ).annotate(
        actual_chunk_rows=Count("chunks", distinct=True),
    ).filter(
        actual_chunk_rows__lt=F("chunk_count")
    ).values_list("node_id", flat=True)
    node_ids.update(chunk_gap_ids)
    return sorted(node_ids)[:limit]


def _get_embedding_recovery_chunk_ids(limit: int) -> list[int]:
    cutoff = _recovery_cutoff()
    max_attempts = _get_max_recovery_attempts()
    backend = get_embedding_backend()
    model_name = get_embedding_model()

    existing_embedding_qs = ChunkEmbedding.objects.filter(
        chunk_id=OuterRef("pk"),
        model_name=model_name,
        model_version=backend,
        status=AIStatus.COMPLETED,
    )
    chunk_qs = (
        DocumentChunk.objects.select_related("parse_result", "parse_result__node")
        .annotate(
            has_completed_embedding=Exists(existing_embedding_qs),
        )
        .filter(
            parse_result__node__trashed=False,
            parse_result__status=AIStatus.COMPLETED,
            recovery_attempts__lt=max_attempts,
        )
        .filter(
            Q(status=AIStatus.PENDING, updated_at__lte=cutoff)
            | Q(status=AIStatus.FAILED, updated_at__lte=cutoff)
            | Q(status=AIStatus.PROCESSING, updated_at__lte=cutoff)
        )
        .filter(has_completed_embedding=False)
        .order_by("id")
    )
    return list(chunk_qs.values_list("id", flat=True)[:limit])


def _reset_chunks_to_pending(chunk_ids: list[int]) -> int:
    if not chunk_ids:
        return 0
    return DocumentChunk.objects.filter(id__in=chunk_ids).exclude(status=AIStatus.PENDING).update(
        status=AIStatus.PENDING,
        error_message={},
    )


def _get_node_ids_for_chunks(chunk_ids: list[int]) -> list[int]:
    """chunk_ids에 해당하는 고유 node_id 목록을 반환합니다."""
    if not chunk_ids:
        return []
    return list(
        DocumentChunk.objects
        .filter(id__in=chunk_ids)
        .values_list("parse_result__node_id", flat=True)
        .distinct()
    )


def _redis_client():
    """복구 idempotency 체크용 Redis 클라이언트를 반환합니다."""
    from redis import Redis
    redis_url = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    return Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)


def _try_acquire_recovery_lock(redis_client, key: str, ttl_seconds: int) -> bool:
    """Redis SET NX 로 복구 락을 획득합니다.

    락 획득 성공(중복 아님) → True
    이미 존재(중복 큐잉 방지 대상) → False
    """
    return bool(redis_client.set(key, "1", nx=True, ex=ttl_seconds))


@shared_task(queue="parse")
def recover_document_pipeline_backlog() -> dict:
    parse_limit = _get_recovery_parse_batch_size()
    embed_limit = _get_recovery_embedding_batch_size()
    stale_minutes = _get_recovery_stale_minutes()
    lock_ttl = stale_minutes * 60

    # Redis 연결 실패 시 dedup 없이 정상 진행 (graceful fallback)
    try:
        redis = _redis_client()
    except Exception as e:
        logger.warning("Redis unavailable for recovery dedup, proceeding without dedup: %s", e)
        redis = None

    # --- 파싱 복구 ---
    parse_node_ids = _get_parse_recovery_node_ids(parse_limit)
    recovered_parse_count = 0
    skipped_parse_count = 0
    queued_parse_node_ids = []
    for node_id in parse_node_ids:
        if redis is not None:
            lock_key = f"recovery:parse:{node_id}"
            if not _try_acquire_recovery_lock(redis, lock_key, lock_ttl):
                skipped_parse_count += 1
                logger.debug("Parse recovery dedup skip: node_id=%s", node_id)
                continue
        parse_document_with_docling.delay(node_id)
        queued_parse_node_ids.append(node_id)
        recovered_parse_count += 1

    # 파싱 복구 메타데이터 업데이트 (재큐잉된 parse result 만)
    if queued_parse_node_ids:
        now = timezone.now()
        DocumentParseResult.objects.filter(node_id__in=queued_parse_node_ids).update(
            recovery_attempts=F("recovery_attempts") + 1,
            last_recovered_at=now,
        )

    # --- 임베딩 복구 ---
    # 복구된 청크를 node 단위로 묶어 enqueue_embedding_tasks 를 경유합니다.
    # enqueue_embedding_tasks 는 PENDING 청크를 PROCESSING 으로 전환한 뒤
    # embedding_document_with_bge 를 큐에 넣으므로, 상태 검사를 올바르게 통과합니다.
    # (기존: embedding_document_with_bge 를 직접 호출 → PROCESSING 상태가 아니어서 skip 됨)
    chunk_ids = _get_embedding_recovery_chunk_ids(embed_limit)
    reset_count = _reset_chunks_to_pending(chunk_ids)

    # chunk_id → node_id 매핑을 미리 구성해 dedup skip 된 노드의 청크는 제외
    chunk_to_node = dict(
        DocumentChunk.objects
        .filter(id__in=chunk_ids)
        .values_list("id", "parse_result__node_id")
    ) if chunk_ids else {}
    node_to_chunks: dict[int, list[int]] = {}
    for cid, nid in chunk_to_node.items():
        node_to_chunks.setdefault(nid, []).append(cid)

    embed_node_ids = list(node_to_chunks.keys())
    recovered_embed_count = 0
    skipped_embed_count = 0
    queued_embed_chunk_ids: list[int] = []
    for node_id in embed_node_ids:
        if redis is not None:
            lock_key = f"recovery:embed:{node_id}"
            if not _try_acquire_recovery_lock(redis, lock_key, lock_ttl):
                skipped_embed_count += 1
                logger.debug("Embed recovery dedup skip: node_id=%s", node_id)
                continue
        enqueue_embedding_tasks.delay(node_id)
        queued_embed_chunk_ids.extend(node_to_chunks.get(node_id, []))
        recovered_embed_count += 1

    # 임베딩 복구 메타데이터 업데이트 (실제 재큐잉된 청크만)
    if queued_embed_chunk_ids:
        now = timezone.now()
        DocumentChunk.objects.filter(id__in=queued_embed_chunk_ids).update(
            recovery_attempts=F("recovery_attempts") + 1,
            last_recovered_at=now,
        )

    summary = {
        "status": "success",
        "parse_requeued": recovered_parse_count,
        "parse_skipped_dedup": skipped_parse_count,
        "embedding_nodes_requeued": recovered_embed_count,
        "embedding_nodes_skipped_dedup": skipped_embed_count,
        "chunks_reset_to_pending": reset_count,
        "stale_minutes": stale_minutes,
        "max_recovery_attempts": _get_max_recovery_attempts(),
    }
    logger.info("Recovered document pipeline backlog: %s", summary)
    return summary



def save_parse_result(node, pr: ParseResult) -> DocumentParseResult:
    """
    Pydantic ParseResult → Django ORM 매핑
    DocumentParseResult + DocumentChunk 를 DB에 저장
    """
    metadata = {
        "parser_version": pr.parser_version,
        "tokenizer_name": get_embedding_model(),
        "chunk_max_tokens": get_chunk_max_tokens(),
        "embedding_max_tokens": get_embedding_max_tokens(),
        "embedding_backend": get_embedding_backend(),
        "file_ext": pr.file_ext,
    }

    metadata = {k:v for k, v in metadata.items() if v is not None}

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
            defaults = {
                "parser_name": "docling",
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
            }
        )

        # 기존 청크 삭제 후 재생성 (재파싱 대응)
        doc_result.chunks.all().delete()

        chunk_objects = []
        for chunk in pr.chunks:
            # meta에서 검색 보조 메타데이터 추출
            raw_meta = chunk.meta or {}

            serialized_meta = serialize_meta(raw_meta) or {}

            normalized_text = normalize_extracted_text(chunk.serialized_text)

            chunk_objects.append(
                DocumentChunk(
                    parse_result=doc_result,
                    chunk_index=chunk.chunk_index,
                    text=normalized_text,
                    token_count=chunk.tokens,
                    section_title=_extract_section_title(raw_meta),
                    page_from=_extract_page(raw_meta, "page_from"),
                    page_to=_extract_page(raw_meta, "page_to"),
                    chunk_meta=serialized_meta,
                )
            )

        DocumentChunk.objects.bulk_create(chunk_objects)

        # Chunk 무결성 검증
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
    """meta에서 섹션 제목을 추출하는 헬퍼"""
    # docling meta 구조에 따라 headings 또는 doc_items에서 추출
    headings = meta.get("headings", [])
    if headings:
        return " > ".join(headings)
    return ""


def _extract_page(meta: dict, key: str) -> int | None:
    """meta에서 페이지 번호를 추출하는 헬퍼"""
    # docling meta 구조에 따라 page 정보 추출
    doc_items = meta.get("doc_items", [])
    if doc_items:
        prov = doc_items[0].get("prov", [])
        if prov:
            page = prov[0].get("page_no")
            if page is not None:
                return page
    return None


@shared_task(queue="parse")
def parse_document_with_docling(node_id: int) -> dict:
    """
    Celery 태스크: 파싱 → DB 저장 오케스트레이션
    node_id를 받아 파일 경로를 조회하고, 파싱 후 결과를 DB에 저장
    """
    from files.models import Node
    from document_ai.parsers.docling_parser import parse_document_entry

    try:
        node = Node.objects.select_related("blob").get(pk=node_id)
        if node.node_type != "file":
            raise ValueError(f"Node {node_id} is not a file")
        if not hasattr(node, "blob") or not node.blob.file:
            raise ValueError(f"Node {node_id} has no attached file blob")

        file_path = node.blob.file.path

        logger.info("Parse started: node_id=%s", node_id)

        # 1. 파서 호출 (순수 Pydantic 결과)
        parse_result = parse_document_entry(file_path)

        # 2. DB 저장 (Pydantic → ORM 매핑)
        doc_result = save_parse_result(node, parse_result)

        enqueue_embedding_tasks.delay(node_id)

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

    except Node.DoesNotExist:
        logger.error(f"Node {node_id} not found")
        return {"status": "failed", "error": f"Node {node_id} not found"}

    except Exception as e:
        logger.exception(f"파싱 실패: node_id={node_id}")

        # 실패 상태 기록
        DocumentParseResult.objects.update_or_create(
            node_id=node_id,
            defaults={
                "parser_name": "docling",
                "status": AIStatus.FAILED,
                "errors": [{"message": str(e)}],
                "metadata": {
                    "tokenizer_name": get_embedding_model(),
                    "chunk_max_tokens": get_chunk_max_tokens(),
                    "embedding_max_tokens": get_embedding_max_tokens(),
                    "embedding_backend": get_embedding_backend(),
                },
                "parsed_at": timezone.now(),
            },
        )

        return {
            "status": "failed",
            "node_id": node_id,
            "error": str(e),
        }

@shared_task(queue="embed")
def enqueue_embedding_tasks(node_id: int) -> dict:
    """
    Celery 태스크: 임베딩 → DB 저장 오케스트레이션
    node_id를 받아 파일 경로를 조회하고, 임베딩 후 결과를 DB에 저장
    """
    from document_ai.models import DocumentChunk
    from config.enums import AIStatus

    try:
        with transaction.atomic():
            chunk_ids = list(
                DocumentChunk.objects
                .select_for_update(skip_locked=True)
                .filter(
                    parse_result__node_id=node_id,
                    status=AIStatus.PENDING,
                )
                .values_list("id", flat=True)
            )

            if not chunk_ids:
                logger.info("Embedding queue skipped: node_id=%s, reason=no_pending_chunks", node_id)
                return {
                    "status": "success",
                    "node_id": node_id,
                    "chunk_count": 0,
                    "message": "No pending chunks found",
                }
            
            DocumentChunk.objects.filter(id__in=chunk_ids).update(
                status=AIStatus.PROCESSING
            )

        for chunk_id in chunk_ids:
            embedding_document_with_bge.apply_async(args=[chunk_id], queue="embed")

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

    except Exception as e:
        logger.exception(f"bge-embedding queueing failed: node_id={node_id}")
        return {
            "status": "failed",
            "node_id": node_id,
            "error": str(e),
        }


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def embedding_document_with_bge(self, chunk_id: int) -> dict:
    """
    Celery 태스크: 임베딩 → DB 저장 오케스트레이션
    node_id를 받아 파일 경로를 조회하고, 임베딩 후 결과를 DB에 저장
    """
    from django.utils import timezone
    from document_ai.models import DocumentChunk, ChunkEmbedding
    from config.enums import AIStatus
    from document_ai.parsers.config import get_embedding_backend, get_embedding_model

    embedding_model = get_embedding_model()
    embedding_backend = get_embedding_backend()

    try:
        chunk = DocumentChunk.objects.get(pk=chunk_id)
    except DocumentChunk.DoesNotExist:
        logger.error("Chunk %s not found", chunk_id)
        return {
            "status": "failed",
            "chunk_id": chunk_id,
            "error": f"Chunk {chunk_id} not found",
        }

    if chunk.status != AIStatus.PROCESSING:
        logger.warning("Chunk %s skipped: %s", chunk_id, chunk.status)
        return {
            "status": "skipped",
            "chunk_id": chunk_id,
            "message": f"Chunk {chunk_id} is not valid state",
        }

    try:
        text = normalize_extracted_text(chunk.text or "")
        if not text:
            raise ValueError("Chunk text is empty")

        logger.info(
            "Embedding started: chunk_id=%s, node_id=%s, chunk_index=%s, tokens=%s, model=%s, backend=%s",
            chunk_id,
            chunk.parse_result.node_id,
            chunk.chunk_index,
            chunk.token_count,
            embedding_model,
            embedding_backend,
        )

        from document_ai.embedding.embeding_models import embed_document

        embedding = embed_document(
            text=text,
            model_name=embedding_model,
            backend=embedding_backend,
        )

        ChunkEmbedding.objects.update_or_create(
            chunk=chunk,
            model_name=embedding_model,
            model_version=embedding_backend,
            defaults={
                "vector": embedding.dense_vector,
                "sparse_vector": embedding.sparse_vector,
                "embedded_at": timezone.now(),
                "status": AIStatus.COMPLETED,
                "error_message": "",
            },
        )

        chunk.status = AIStatus.COMPLETED
        chunk.error_message = ""
        chunk.save(update_fields=["status", "error_message"])

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

    except ValueError as e:
        logger.warning("Embedding validation failed: chunk_id=%s, error=%s", chunk_id, e)

        ChunkEmbedding.objects.update_or_create(
            chunk=chunk,
            model_name=embedding_model,
            model_version=embedding_backend,
            defaults={
                "vector": None,
                "sparse_vector": {},
                "embedded_at": None,
                "status": AIStatus.FAILED,
                "error_message": str(e),
            },
        )

        chunk.status = AIStatus.FAILED
        chunk.error_message = str(e)
        chunk.save(update_fields=["status", "error_message"])

        return {
            "status": "failed",
            "chunk_id": chunk_id,
            "error": str(e),
        }

    except RuntimeError as e:
        error_message = str(e)

        # embedding.py에서 GPU OOM을 RuntimeError로 래핑해서 올린 경우
        if "GPU OOM" in error_message:
            logger.warning(
                "Embedding GPU OOM: chunk_id=%s, retries=%s/%s, error=%s",
                chunk_id,
                self.request.retries,
                self.max_retries,
                error_message,
            )

        else:
            logger.warning(
                "Embedding runtime error: chunk_id=%s, retries=%s/%s, error=%s",
                chunk_id,
                self.request.retries,
                self.max_retries,
                error_message,
            )

        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)

        logger.exception("Embedding permanently failed: chunk_id=%s", chunk_id)

        ChunkEmbedding.objects.update_or_create(
            chunk=chunk,
            model_name=embedding_model,
            model_version=embedding_backend,
            defaults={
                "vector": None,
                "sparse_vector": {},
                "embedded_at": None,
                "status": AIStatus.FAILED,
                "error_message": error_message,
            },
        )

        chunk.status = AIStatus.FAILED
        chunk.error_message = error_message
        chunk.save(update_fields=["status", "error_message"])

        return {
            "status": "failed",
            "chunk_id": chunk_id,
            "error": error_message,
        }

    except Exception as e:
        logger.warning(
            "Embedding failed (retrying): chunk_id=%s, retries=%s/%s, error=%s",
            chunk_id,
            self.request.retries,
            self.max_retries,
            e,
        )

        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)

        logger.exception("Embedding permanently failed: chunk_id=%s", chunk_id)

        ChunkEmbedding.objects.update_or_create(
            chunk=chunk,
            model_name=embedding_model,
            model_version=embedding_backend,
            defaults={
                "vector": None,
                "sparse_vector": {},
                "embedded_at": None,
                "status": AIStatus.FAILED,
                "error_message": str(e),
            },
        )

        chunk.status = AIStatus.FAILED
        chunk.error_message = str(e)
        chunk.save(update_fields=["status", "error_message"])

        return {
            "status": "failed",
            "chunk_id": chunk_id,
            "error": str(e),
        }


@shared_task(queue="search", bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=1)
def perform_vector_search(self, job_id: int) -> dict:
    from document_ai.models import SearchJob
    from document_ai.search.retriever import VectorRetriever

    try:
        job = SearchJob.objects.select_related("owner").get(pk=job_id)
    except SearchJob.DoesNotExist:
        logger.error("Search job %s not found", job_id)
        return {
            "status": "failed",
            "job_id": job_id,
            "error": f"Search job {job_id} not found",
        }

    job.status = AIStatus.PROCESSING
    job.started_at = timezone.now()
    job.error_message = ""
    job.save(update_fields=["status", "started_at", "error_message"])

    try:
        retriever = VectorRetriever()
        logger.info(
            "Vector search started: job_id=%s, owner_id=%s, top_k=%s, tuning_params=%s",
            job.id,
            job.owner_id,
            job.top_k,
            job.tuning_params,
        )
        results = retriever.retrieve(
            query=job.query,
            top_k=job.top_k,
            threshold=job.threshold,
            node_ids=job.node_ids or None,
            user=job.owner,
            tuning_params=job.tuning_params,
        )

        # UUID 등 JSONField가 직접 저장하지 못하는 값을 문자열로 정규화합니다.
        normalized_results = json.loads(json.dumps(results, default=str))

        job.results = normalized_results
        job.status = AIStatus.COMPLETED
        job.completed_at = timezone.now()
        job.error_message = ""
        job.save(update_fields=["results", "status", "completed_at", "error_message"])

        logger.info(
            "Vector search completed: job_id=%s, result_count=%s",
            job.id,
            len(normalized_results),
        )

        return {
            "status": "success",
            "job_id": job_id,
            "result_count": len(normalized_results),
        }

    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)

        job.status = AIStatus.FAILED
        job.completed_at = timezone.now()
        job.error_message = str(exc)
        job.save(update_fields=["status", "completed_at", "error_message"])

        logger.exception("Vector search failed: job_id=%s", job_id)
        return {
            "status": "failed",
            "job_id": job_id,
            "error": str(exc),
        }


@shared_task(queue="text2sql", bind=True, soft_time_limit=330, time_limit=360)
def generate_text2sql_response(self, prompt: str) -> dict:
    """
    Celery 태스크: Text2SQL 요청을 전용 큐에서 순차적으로 받아
    Redis Semaphore 범위 안에서만 LLM 서버에 전달합니다.
    """
    import requests
    from redis import Redis
    from redis_semaphore import NotAvailable, Semaphore

    llm_base_url = os.getenv("TEXT2SQL_LLM_URL", "http://llm-parser:8080").rstrip("/")
    redis_url = os.getenv("TEXT2SQL_REDIS_URL", os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"))
    semaphore_count = _get_positive_int_env("TEXT2SQL_SEMAPHORE_COUNT", 1)
    semaphore_timeout = _get_positive_int_env("TEXT2SQL_SEMAPHORE_TIMEOUT", 5)
    request_timeout = _get_positive_int_env("TEXT2SQL_REQUEST_TIMEOUT", 300)
    max_tokens = _get_positive_int_env("TEXT2SQL_MAX_TOKENS", 128)
    stale_lock_timeout = max(request_timeout + 60, 300)
    semaphore_namespace = os.getenv("TEXT2SQL_SEMAPHORE_NAMESPACE", "llm_text2sql_v2")

    redis_client = Redis.from_url(redis_url)
    semaphore = Semaphore(
        redis_client,
        count=semaphore_count,
        namespace=semaphore_namespace,
        stale_client_timeout=stale_lock_timeout,
    )

    try:
        semaphore.acquire(timeout=semaphore_timeout)
    except NotAvailable:
        logger.warning(
            "Text2SQL semaphore timeout after %ss (count=%s)",
            semaphore_timeout,
            semaphore_count,
        )
        return {
            "status": "busy",
            "message": "Text2SQL worker is busy. Please retry shortly.",
        }

    try:
        model = "google/gemma-4-E4B-it"
        system_prompt = (
            "당신은 PostgreSQL용 SQL 생성기입니다. "
            "설명, 주석, 코드블록 없이 SQL 쿼리 하나만 출력하세요."
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": max_tokens,
            "stream": False,
            "reasoning_format": "none",
        }
        res = requests.post(
            f"{llm_base_url}/v1/chat/completions",
            json=payload,
            timeout=(5, request_timeout),
        )
        res.raise_for_status()
        response_payload = res.json()
        raw_content = _get_llm_message_content(response_payload)
        final_content = _extract_llm_final_content(raw_content, fallback_to_raw=True)
        if not final_content:
            return {
                "status": "error",
                "message": "Text2SQL LLM returned empty final content.",
                "raw_preview": raw_content[:1000],
            }
        if response_payload.get("choices"):
            response_payload["choices"][0].setdefault("message", {})["content"] = final_content
        return response_payload
    except requests.Timeout as e:
        logger.error("Text2SQL LLM timeout after %ss: %s", request_timeout, e)
        return {
            "status": "error",
            "message": f"Text2SQL request timed out after {request_timeout}s",
        }
    except Exception as e:
        logger.error(f"Text2SQL LLM Error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        semaphore.release()


def _build_rag_context(
    results: list,
    *,
    evidence_limit: int = 3,
    context_max_chars: int = 3000,
    evidence_text_max_chars: int = 500,
) -> tuple[str, list[dict]]:
    context_blocks = []
    citations = []
    citation_id = 1
    used_chars = 0

    for result in results or []:
        node_name = result.get("node_name", "")
        node_id = result.get("node_id")
        doc_score = result.get("doc_score")
        for evidence in result.get("evidences", []) or []:
            if citation_id > evidence_limit:
                return "\n\n".join(context_blocks), citations

            text = evidence.get("compressed_text") or evidence.get("context_text") or evidence.get("text") or ""
            text = str(text).strip()
            if not text:
                continue
            remaining_chars = context_max_chars - used_chars
            if remaining_chars <= 0:
                return "\n\n".join(context_blocks), citations

            text_limit = max(100, min(evidence_text_max_chars, remaining_chars))
            clipped_text = text[:text_limit]
            citation = {
                "id": citation_id,
                "node_id": str(node_id) if node_id is not None else "",
                "node_name": node_name,
                "chunk_id": evidence.get("chunk_id"),
                "section": evidence.get("section") or "",
                "pages": evidence.get("pages") or "",
                "doc_score": doc_score,
                "hybrid_score": evidence.get("hybrid_score"),
                "dense_score": evidence.get("dense_score"),
                "sparse_score": evidence.get("sparse_score"),
                "text": clipped_text,
            }
            block = "\n".join(
                [
                    f"[{citation_id}] {node_name}",
                    f"section: {citation['section'] or 'N/A'}",
                    f"pages: {citation['pages'] or 'N/A'}",
                    f"text: {citation['text']}",
                ]
            )
            if used_chars + len(block) > context_max_chars and citations:
                return "\n\n".join(context_blocks), citations

            citations.append(citation)
            context_blocks.append(block)
            used_chars += len(block) + 2
            citation_id += 1

    return "\n\n".join(context_blocks), citations


def _normalize_rag_answer(raw_answer: str) -> str:
    answer = _extract_llm_final_content(raw_answer, fallback_to_raw=True)
    if not answer:
        return ""

    answer = re.sub(r"<\|channel\>thought[\s\S]*?(?=<\|channel\>final|$)", "", answer).strip()
    answer = re.sub(r"<\|[^>]+?\|>", "", answer).strip()
    answer = re.sub(r"^```(?:[a-zA-Z]+)?\s*|\s*```$", "", answer).strip()
    return answer


def _clean_rag_evidence_text(text: str, *, limit: int = 220) -> str:
    cleaned = re.sub(r"<!--.*?-->", " ", str(text or ""), flags=re.DOTALL)
    cleaned = re.sub(r"\|[-:\s|]+\|", " ", cleaned)
    cleaned = re.sub(r"[|#*_`]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "..."
    return cleaned


def _answer_claims_insufficient(answer: str) -> bool:
    candidate = (answer or "").strip()
    for separator in ["주요 근거", "Key Evidence", "근거 부족", "Limitations"]:
        if separator in candidate:
            candidate = candidate.split(separator, 1)[0]
            break
    lowered = candidate[:500].lower()
    markers = [
        "근거가 부족해 답변할 수 없습니다",
        "근거가 부족하여 답변할 수 없습니다",
        "답변할 수 없습니다",
        "확인할 수 없습니다",
        "not enough evidence",
        "insufficient evidence",
        "cannot determine",
    ]
    return any(marker in lowered for marker in markers)


def _fallback_rag_answer(citations: list[dict], language: str) -> str:
    if not citations:
        return "근거가 부족해 답변할 수 없습니다." if language == "ko" else "There is not enough evidence to answer."

    selected = citations[:3]

    if language == "ko":
        lines = ["핵심 답변"]
        for citation in selected[:2]:
            text = _clean_rag_evidence_text(citation.get("text"), limit=230)
            if text:
                lines.append(f"- {text} [{citation.get('id')}]")

        lines.append("")
        lines.append("주요 근거")
        for citation in selected:
            section = citation.get("section") or "N/A"
            pages = citation.get("pages") or "N/A"
            node_name = citation.get("node_name") or "문서"
            lines.append(f"- {node_name} / section {section} / page {pages} [{citation.get('id')}]")

        lines.append("")
        lines.append("근거 부족")
        lines.append("- 위 근거 밖의 세부 사항은 확인하지 않았습니다.")
        return "\n".join(lines)

    lines = ["Answer"]
    for citation in selected[:2]:
        text = _clean_rag_evidence_text(citation.get("text"), limit=230)
        if text:
            lines.append(f"- {text} [{citation.get('id')}]")

    lines.append("")
    lines.append("Key Evidence")
    for citation in selected:
        section = citation.get("section") or "N/A"
        pages = citation.get("pages") or "N/A"
        node_name = citation.get("node_name") or "Document"
        lines.append(f"- {node_name} / section {section} / page {pages} [{citation.get('id')}]")

    lines.append("")
    lines.append("Limitations")
    lines.append("- Details outside the cited evidence were not verified.")
    return "\n".join(lines)


def _summarize_structured_filters(filters: list[dict]) -> list[str]:
    summaries = []
    for item in filters:
        scope = item.get("scope") or "node"
        field = item.get("field")
        operator = item.get("operator")
        value = item.get("value")
        if not field or not operator:
            continue
        summaries.append(f"{scope}.{field} {operator} {value}")
    return summaries


def _build_refined_query_prompt(*, mode: str, question: dict, metadata: dict) -> str:
    dense_query = (question.get("dense") or question.get("residual") or question.get("normalized") or "").strip()
    if not dense_query:
        return ""

    filter_summaries = _summarize_structured_filters(metadata.get("filters") or [])
    scope_text = ", ".join(metadata.get("target_scopes") or [])
    prompt_lines = [dense_query]

    if filter_summaries:
        prompt_lines.append("메타데이터 조건: " + "; ".join(filter_summaries))
    if scope_text:
        prompt_lines.append("대상 범위: " + scope_text)

    if mode == "rag":
        prompt_lines.append(
            "검색된 문서 근거만 사용해 한국어로 답변하고, 확인되지 않은 내용은 근거 부족으로 분리하세요."
        )
    else:
        prompt_lines.append("메타데이터 조건을 먼저 적용한 뒤 남은 의미 질의로 관련 문서를 검색하세요.")

    return "\n".join(prompt_lines)


def _fallback_query_parse(query: str, mode: str, error: Exception | None = None) -> dict:
    from search_engine.query_engine import QueryPipeline

    payload = QueryPipeline(enabled=True).passthrough(query, mode, source="fallback")
    if error is not None:
        payload["status"] = "fallback"
        payload["analysis"]["warnings"].append(
            {
                "code": "query_llm_failed",
                "message": str(error),
            }
        )
    return payload


def _query_dsl_schema_for_prompt() -> dict:
    from search_engine.query_engine.schema import QUERY_DSL_SCHEMA

    return _json_safe(QUERY_DSL_SCHEMA)


def _passthrough_query_parse(query: str, mode: str, *, source: str, warning: dict | None = None) -> dict:
    from search_engine.query_engine import QueryPipeline

    return QueryPipeline(enabled=True).passthrough(query, mode, source=source, warning=warning)


def _extract_json_object(text: str) -> dict:
    candidate = _extract_llm_final_content(text, fallback_to_raw=True)
    if not candidate:
        raise ValueError("LLM returned empty content.")

    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate).strip()

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(candidate[start : end + 1])

    if not isinstance(payload, dict):
        raise ValueError("LLM query parser output must be a JSON object.")
    return payload


def _parse_user_query_with_llm(query: str, mode: str) -> dict:
    import requests
    from redis import Redis
    from redis_semaphore import NotAvailable, Semaphore
    from search_engine.query_engine import QueryPipeline

    llm_base_url = os.getenv("QUERY_LLM_URL", os.getenv("RAG_LLM_URL", os.getenv("TEXT2SQL_LLM_URL", "http://llm-parser:8080"))).rstrip("/")
    model = os.getenv("QUERY_LLM_MODEL", os.getenv("RAG_LLM_MODEL", "google/gemma-4-E4B-it"))
    redis_url = os.getenv("QUERY_REDIS_URL", os.getenv("TEXT2SQL_REDIS_URL", os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")))
    semaphore_count = _get_positive_int_env("QUERY_SEMAPHORE_COUNT", _get_positive_int_env("TEXT2SQL_SEMAPHORE_COUNT", 1))
    semaphore_timeout = _get_positive_int_env("QUERY_SEMAPHORE_TIMEOUT", _get_positive_int_env("TEXT2SQL_SEMAPHORE_TIMEOUT", 5))
    request_timeout = _get_positive_int_env("QUERY_REQUEST_TIMEOUT", 300)
    max_tokens = _get_positive_int_env("QUERY_MAX_TOKENS", 1024)
    stale_lock_timeout = max(request_timeout + 60, 300)
    semaphore_namespace = os.getenv(
        "QUERY_SEMAPHORE_NAMESPACE",
        os.getenv("TEXT2SQL_SEMAPHORE_NAMESPACE", "llm_text2sql_v2"),
    )
    pipeline_enabled = os.getenv("QUERY_PIPELINE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    max_validation_passes = _get_positive_int_env("QUERY_PIPELINE_MAX_VALIDATION_PASSES", 2)
    query_pipeline = QueryPipeline(
        enabled=pipeline_enabled,
        max_validation_passes=max_validation_passes,
    )

    system_prompt = (
        "You convert Korean or English natural-language document search queries into a limited QueryDSL candidate. "
        "Return only compact JSON. No markdown, no explanations, no SQL, no Django ORM code, no extra text. "
        'Shape: {"semantic_query":"","filters":[],"sorts":[],"target_scopes":[]}. '
        "Each filter shape is {scope,field,operator,value,source_text,confidence}. "
        "Each sort shape is {scope,field,direction,source_text}. "
        "Use only the provided schema scopes, fields, and operators. "
        "Correct obvious typos in user language, but do not invent metadata filters. "
        "Do not create filters for trash/deleted state, upload completion, parse completion, or embedding completion. "
        "Those operational states are outside the searchable QueryDSL surface. "
        "If a condition is ambiguous, omit the filter and preserve the meaning in semantic_query."
    )
    today = timezone.localdate()
    tz = timezone.get_current_timezone()
    last_7_start = timezone.make_aware(datetime.combine(today - timedelta(days=7), time.min), tz).isoformat()
    last_7_end = timezone.make_aware(datetime.combine(today, time.min), tz).isoformat()

    user_prompt = json.dumps(
        {
            "today": timezone.localdate().isoformat(),
            "timezone": str(tz),
            "relative_date_rules": {
                "지난주": f"Use last 7 days: created_at >= {last_7_start} and created_at < {last_7_end}.",
                "last week": f"Use last 7 days: created_at >= {last_7_start} and created_at < {last_7_end}.",
            },
            "mode": mode,
            "query": query,
            "query_dsl_schema": _query_dsl_schema_for_prompt(),
            "examples": [
                {
                    "query": "지난주 업로드한 pdf 계약 문서 찾아줘",
                    "output": {
                        "semantic_query": "계약 문서",
                        "filters": [
                            {"scope": "node", "field": "node_type", "operator": "eq", "value": "file", "source_text": "문서", "confidence": 0.95},
                            {"scope": "node", "field": "ext", "operator": "eq", "value": "pdf", "source_text": "pdf", "confidence": 1.0},
                            {"scope": "node", "field": "created_at", "operator": "gte", "value": last_7_start, "source_text": "지난주 업로드", "confidence": 0.9},
                            {"scope": "node", "field": "created_at", "operator": "lt", "value": last_7_end, "source_text": "지난주 업로드", "confidence": 0.9},
                        ],
                        "sorts": [
                            {"scope": "node", "field": "created_at", "direction": "desc", "source_text": "최근 업로드"}
                        ],
                        "target_scopes": ["node"],
                    },
                },
                {
                    "query": "할인지원 농식품 보도자료",
                    "output": {
                        "semantic_query": "할인지원 농식품 보도자료",
                        "filters": [],
                        "sorts": [],
                        "target_scopes": ["node"],
                    },
                },
            ],
        },
        ensure_ascii=False,
    )

    redis_client = Redis.from_url(redis_url)
    semaphore = Semaphore(
        redis_client,
        count=semaphore_count,
        namespace=semaphore_namespace,
        stale_client_timeout=stale_lock_timeout,
    )

    try:
        semaphore.acquire(timeout=semaphore_timeout)
    except NotAvailable as exc:
        raise RuntimeError("Query LLM parser is busy. Please retry shortly.") from exc

    try:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": float(os.getenv("QUERY_TEMPERATURE", "0.0")),
            "top_p": float(os.getenv("QUERY_TOP_P", "1.0")),
            "max_tokens": max_tokens,
            "stream": False,
            "reasoning_format": os.getenv("QUERY_REASONING_FORMAT", "none"),
            "response_format": {"type": "json_object"},
        }
        response = requests.post(
            f"{llm_base_url}/v1/chat/completions",
            json=payload,
            timeout=(5, request_timeout),
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(f"Query LLM HTTP {response.status_code}: {response.text[:1000]}") from exc

        response_payload = response.json()
        raw_content = _get_llm_message_content(response_payload)
        final_content = _extract_llm_final_content(raw_content, fallback_to_raw=True)
        if not final_content:
            return _passthrough_query_parse(
                query,
                mode,
                source="llm_empty_final",
                warning={
                    "code": "query_llm_empty_final",
                    "message": "Query LLM returned no final content. Original query was used as semantic query.",
                },
            )
        try:
            raw_dsl_payload = _extract_json_object(final_content)
        except Exception as exc:
            raise RuntimeError(f"Query LLM returned invalid JSON: {raw_content[:1000]!r}") from exc

        result = query_pipeline.run(query, mode, raw_dsl_payload)
        result["debug"].update(
            {
                "llm_model": model,
                "llm_url": llm_base_url,
            }
        )
        return result
    finally:
        semaphore.release()


@shared_task(queue="query")
def parse_user_query(query: str, mode: str = "search") -> dict:
    """
    사용자 질의를 LLM 기반 QueryDSL 후보로 파싱하고 query_engine에서 검증/ORM 컴파일합니다.
    규칙 기반 QueryAnalyzer는 사용하지 않으며, 실패 시 원 질의를 semantic query로 보존합니다.
    """
    normalized_query = normalize_extracted_text(query or "").strip()
    if not normalized_query:
        return _fallback_query_parse(query, mode)

    llm_enabled = os.getenv("QUERY_LLM_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    if not llm_enabled:
        return _passthrough_query_parse(
            normalized_query,
            mode,
            source="llm_disabled",
            warning={
                "code": "query_llm_disabled",
                "message": "QUERY_LLM_ENABLED is disabled. Original query was used as semantic query.",
            },
        )

    try:
        return _parse_user_query_with_llm(normalized_query, mode)
    except Exception as exc:
        logger.warning("Query LLM parser failed; preserving original query: %s", exc)
        return _fallback_query_parse(query, mode, error=exc)


@shared_task(queue="rag", bind=True, soft_time_limit=330, time_limit=360)
def generate_rag_response(self, rag_job_id: int) -> dict:
    """
    RAG 답변 생성 태스크.
    검색은 search worker가 완료한 SearchJob 결과를 사용하고, LLM 호출은 text2sql worker와
    동일한 llm-parser 엔드포인트/세마포어 정책을 공유하되, celery rag 큐에서 실행합니다.
    """
    import requests
    from redis import Redis
    from redis_semaphore import NotAvailable, Semaphore

    from document_ai.models import RAGJob

    try:
        rag_job = RAGJob.objects.select_related("search_job").get(pk=rag_job_id)
    except RAGJob.DoesNotExist:
        logger.error("RAG job %s not found", rag_job_id)
        return {"status": "failed", "job_id": rag_job_id, "error": f"RAG job {rag_job_id} not found"}

    if not rag_job.search_job or rag_job.search_job.status != AIStatus.COMPLETED:
        rag_job.status = AIStatus.FAILED
        rag_job.completed_at = timezone.now()
        rag_job.error_message = "Search job is not completed."
        rag_job.save(update_fields=["status", "completed_at", "error_message"])
        return {"status": "failed", "job_id": rag_job_id, "error": rag_job.error_message}

    rag_job.status = AIStatus.PROCESSING
    rag_job.started_at = timezone.now()
    rag_job.error_message = ""
    rag_job.save(update_fields=["status", "started_at", "error_message"])

    llm_base_url = os.getenv("RAG_LLM_URL", os.getenv("TEXT2SQL_LLM_URL", "http://llm-parser:8080")).rstrip("/")
    redis_url = os.getenv("RAG_REDIS_URL", os.getenv("TEXT2SQL_REDIS_URL", os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")))
    semaphore_count = _get_positive_int_env("RAG_SEMAPHORE_COUNT", _get_positive_int_env("TEXT2SQL_SEMAPHORE_COUNT", 1))
    semaphore_timeout = _get_positive_int_env("RAG_SEMAPHORE_TIMEOUT", _get_positive_int_env("TEXT2SQL_SEMAPHORE_TIMEOUT", 5))
    request_timeout = _get_positive_int_env("RAG_REQUEST_TIMEOUT", _get_positive_int_env("TEXT2SQL_REQUEST_TIMEOUT", 300))
    max_tokens = _get_positive_int_env("RAG_MAX_TOKENS", 512)
    evidence_limit = _get_positive_int_env("RAG_EVIDENCE_LIMIT", 5)
    context_max_chars = _get_positive_int_env("RAG_CONTEXT_MAX_CHARS", 3000)
    evidence_text_max_chars = _get_positive_int_env("RAG_EVIDENCE_TEXT_MAX_CHARS", 500)
    stale_lock_timeout = max(request_timeout + 60, 300)
    semaphore_namespace = os.getenv(
        "RAG_SEMAPHORE_NAMESPACE",
        os.getenv("TEXT2SQL_SEMAPHORE_NAMESPACE", "llm_text2sql_v2"),
    )

    context_text, citations = _build_rag_context(
        rag_job.search_job.results,
        evidence_limit=evidence_limit,
        context_max_chars=context_max_chars,
        evidence_text_max_chars=evidence_text_max_chars,
    )
    if not citations:
        rag_job.status = AIStatus.FAILED
        rag_job.completed_at = timezone.now()
        rag_job.error_message = "No evidence was found for the question."
        rag_job.citations = []
        rag_job.save(update_fields=["status", "completed_at", "error_message", "citations"])
        return {"status": "failed", "job_id": rag_job_id, "error": rag_job.error_message}

    language_instruction = "Answer in Korean" if rag_job.language == "ko" else "Answer in English."
    system_prompt = (
        "You are an assistant for question-answering based on provided evidence. "
        "Use the following pieces of retrieved context to answer the question."
        "If any evidence is present, do not assume insufficient evidence; answer only what is confirmed in the evidence. "
        "Only state that the evidence is insufficient if the information required for the question is entirely absent from the evidence. "
        "Append citation numbers like [1], [2] at the end of each key sentence. "
        "Absolutely do not output reasoning processes, analysis processes, thoughts, channels, or step-by-step thinking. "
        "Synthesize a clear, natural-sounding final answer without repeating or explicitly listing the evidence. Do not use structural headings unless necessary. "
        "Do not create code blocks; output only the final answer text. "
        f"{language_instruction}"
    )
    user_prompt = (
        f"Question:\n{rag_job.question}\n\n"
        f"Evidence:\n{context_text}\n\n"
        "Answer strictly based on the evidence provided."
    )
    if rag_job.language == "ko":
        fewshot_user = (
            "Question:\n주요 지원 대책은 무엇인가요?\n\n"
            "Evidence:\n[1] 문서 A\nsection: 정책\npages: 1\n"
            "text: 정부는 공급 확대와 할인 지원을 병행해 가격 부담을 낮춘다.\n\n"
            "Answer concisely but sufficiently based on the evidence provided."
        )
        fewshot_assistant = (
            "주요 지원 대책은 공급 확대와 할인 지원을 병행하는 것입니다 [1]."
        )
    else:
        fewshot_user = (
            "Question:\nWhat are the main support measures?\n\n"
            "Evidence:\n[1] Document A\nsection: Policy\npages: 1\n"
            "text: The government will expand supply and provide discount support to reduce price pressure.\n\n"
            "Answer using only the evidence above."
        )
        fewshot_assistant = (
            "The main support measures include supply expansion and discount support [1]."
        )

    redis_client = Redis.from_url(redis_url)
    semaphore = Semaphore(
        redis_client,
        count=semaphore_count,
        namespace=semaphore_namespace,
        stale_client_timeout=stale_lock_timeout,
    )

    try:
        semaphore.acquire(timeout=semaphore_timeout)
    except NotAvailable:
        rag_job.status = AIStatus.PENDING
        rag_job.task_id = ""
        rag_job.error_message = "RAG worker is busy. Please retry shortly."
        rag_job.save(update_fields=["status", "task_id", "error_message"])
        return {"status": "busy", "job_id": rag_job_id, "message": rag_job.error_message}

    try:
        payload = {
            "model": os.getenv("RAG_LLM_MODEL", "google/gemma-4-E4B-it"),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": fewshot_user},
                {"role": "assistant", "content": fewshot_assistant},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": float(os.getenv("RAG_TEMPERATURE", "0.2")),
            "top_p": float(os.getenv("RAG_TOP_P", "0.9")),
            "max_tokens": max_tokens,
            "stream": False,
            "reasoning_format": "none",
        }
        response = requests.post(
            f"{llm_base_url}/v1/chat/completions",
            json=payload,
            timeout=(5, request_timeout),
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            response_text = response.text[:2000]
            raise RuntimeError(
                f"RAG LLM HTTP {response.status_code}: {response_text}"
            ) from exc
        payload = response.json()
        raw_answer = (
            payload.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        answer = _normalize_rag_answer(raw_answer)
        if not answer:
            logger.warning(
                "RAG answer normalization returned empty output: job_id=%s raw_preview=%r",
                rag_job.id,
                raw_answer[:500],
            )
            answer = _fallback_rag_answer(citations, rag_job.language)
            rag_job.error_message = f"LLM returned no final answer after normalization. raw_preview={raw_answer[:1000]}"
        elif citations and _answer_claims_insufficient(answer):
            logger.warning(
                "RAG answer incorrectly claimed insufficient evidence: job_id=%s citation_count=%s raw_preview=%r",
                rag_job.id,
                len(citations),
                raw_answer[:500],
            )
            answer = _fallback_rag_answer(citations, rag_job.language)
            rag_job.error_message = f"LLM claimed insufficient evidence despite citations. raw_preview={raw_answer[:1000]}"
        else:
            rag_job.error_message = ""

        rag_job.answer = answer
        rag_job.citations = citations
        rag_job.status = AIStatus.COMPLETED
        rag_job.completed_at = timezone.now()
        rag_job.save(update_fields=["answer", "citations", "status", "completed_at", "error_message"])

        return {"status": "success", "job_id": rag_job_id, "citation_count": len(citations)}
    except requests.Timeout as exc:
        rag_job.status = AIStatus.FAILED
        rag_job.completed_at = timezone.now()
        rag_job.error_message = f"RAG request timed out after {request_timeout}s"
        rag_job.save(update_fields=["status", "completed_at", "error_message"])
        logger.error("RAG LLM timeout after %ss: %s", request_timeout, exc)
        return {"status": "failed", "job_id": rag_job_id, "error": rag_job.error_message}
    except Exception as exc:
        rag_job.status = AIStatus.FAILED
        rag_job.completed_at = timezone.now()
        rag_job.error_message = str(exc)
        rag_job.save(update_fields=["status", "completed_at", "error_message"])
        logger.exception("RAG LLM error: job_id=%s", rag_job_id)
        return {"status": "failed", "job_id": rag_job_id, "error": str(exc)}
    finally:
        semaphore.release()
