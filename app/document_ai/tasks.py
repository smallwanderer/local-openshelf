import logging
import os
from datetime import timedelta

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
from document_ai.parsers.docling_parser import parse_document_entry, ParseResult
from document_ai.parsers.text_utils import normalize_extracted_text, serialize_meta

from config.enums import AIStatus

logger = logging.getLogger(__name__)


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


def _recovery_cutoff():
    return timezone.now() - timedelta(minutes=_get_recovery_stale_minutes())


def _get_parse_recovery_node_ids(limit: int) -> list[int]:
    from files.models import Node

    cutoff = _recovery_cutoff()
    node_ids = set(
        Node.objects.select_related("blob", "parse_result")
        .filter(
            node_type="file",
            trashed=False,
            blob__isnull=False,
        )
        .filter(
            Q(parse_result__isnull=True)
            | Q(parse_result__status=AIStatus.FAILED, parse_result__updated_at__lte=cutoff)
            | Q(parse_result__status=AIStatus.PENDING, parse_result__updated_at__lte=cutoff)
            | Q(parse_result__status=AIStatus.PROCESSING, parse_result__updated_at__lte=cutoff)
        )
        .order_by("id")
        .values_list("id", flat=True)
    )
    chunk_gap_ids = DocumentParseResult.objects.filter(
        node__trashed=False,
        node__blob__isnull=False,
        status=AIStatus.COMPLETED,
        updated_at__lte=cutoff,
    ).annotate(
        actual_chunk_rows=Count("chunks", distinct=True),
    ).filter(
        actual_chunk_rows__lt=F("chunk_count")
    ).values_list("node_id", flat=True)
    node_ids.update(chunk_gap_ids)
    return sorted(node_ids)[:limit]


def _get_embedding_recovery_chunk_ids(limit: int) -> list[int]:
    cutoff = _recovery_cutoff()
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
    for node_id in parse_node_ids:
        if redis is not None:
            lock_key = f"recovery:parse:{node_id}"
            if not _try_acquire_recovery_lock(redis, lock_key, lock_ttl):
                skipped_parse_count += 1
                logger.debug("Parse recovery dedup skip: node_id=%s", node_id)
                continue
        parse_document_with_docling.delay(node_id)
        recovered_parse_count += 1

    # --- 임베딩 복구 ---
    # 복구된 청크를 node 단위로 묶어 enqueue_embedding_tasks 를 경유합니다.
    # enqueue_embedding_tasks 는 PENDING 청크를 PROCESSING 으로 전환한 뒤
    # embedding_document_with_bge 를 큐에 넣으므로, 상태 검사를 올바르게 통과합니다.
    # (기존: embedding_document_with_bge 를 직접 호출 → PROCESSING 상태가 아니어서 skip 됨)
    chunk_ids = _get_embedding_recovery_chunk_ids(embed_limit)
    reset_count = _reset_chunks_to_pending(chunk_ids)
    embed_node_ids = _get_node_ids_for_chunks(chunk_ids)
    recovered_embed_count = 0
    skipped_embed_count = 0
    for node_id in embed_node_ids:
        if redis is not None:
            lock_key = f"recovery:embed:{node_id}"
            if not _try_acquire_recovery_lock(redis, lock_key, lock_ttl):
                skipped_embed_count += 1
                logger.debug("Embed recovery dedup skip: node_id=%s", node_id)
                continue
        enqueue_embedding_tasks.delay(node_id)
        recovered_embed_count += 1

    summary = {
        "status": "success",
        "parse_requeued": recovered_parse_count,
        "parse_skipped_dedup": skipped_parse_count,
        "embedding_nodes_requeued": recovered_embed_count,
        "embedding_nodes_skipped_dedup": skipped_embed_count,
        "chunks_reset_to_pending": reset_count,
        "stale_minutes": stale_minutes,
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

    try:
        node = Node.objects.select_related("blob").get(pk=node_id)
        if node.node_type != "file":
            raise ValueError(f"Node {node_id} is not a file")
        if not hasattr(node, "blob") or not node.blob.file:
            raise ValueError(f"Node {node_id} has no attached file blob")

        file_path = node.blob.file.path

        # 1. 파서 호출 (순수 Pydantic 결과)
        parse_result = parse_document_entry(file_path)

        # 2. DB 저장 (Pydantic → ORM 매핑)
        doc_result = save_parse_result(node, parse_result)

        enqueue_embedding_tasks.delay(node_id)

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

        from document_ai.embedding.embeding_models import bge_m3_embedder

        embedding = bge_m3_embedder(
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
        return res.json()
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
