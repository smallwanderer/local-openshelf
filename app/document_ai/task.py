# 분산 비동기 작업 기능을 모아놓는 곳
import logging

from celery import shared_task
from django.utils import timezone
from django.db import transaction

from document_ai.models import DocumentParseResult, DocumentChunk
from document_ai.parsers.config import (
    get_chunk_max_tokens,
    get_embedding_backend,
    get_embedding_max_tokens,
    get_embedding_model,
)
from document_ai.parsers.docling_parser import parse_document_entry, ParseResult
from document_ai.parsers.text_utils import serialize_meta

from config.enums import AIStatus

logger = logging.getLogger(__name__)


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

            chunk_objects.append(
                DocumentChunk(
                    parse_result=doc_result,
                    chunk_index=chunk.chunk_index,
                    text=chunk.serialized_text,
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
        text = (chunk.text or "").strip()
        if not text:
            raise ValueError("Chunk text is empty")

        from document_ai.embedding.embeding_models import bge_m3_embedder

        vector = bge_m3_embedder(
            text=text,
            model_name=embedding_model,
            backend=embedding_backend,
        )

        ChunkEmbedding.objects.update_or_create(
            chunk=chunk,
            model_name=embedding_model,
            model_version=embedding_backend,
            defaults={
                "vector": vector,
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
