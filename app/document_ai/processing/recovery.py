from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.db.models import Count, Exists, F, OuterRef, Q
from django.utils import timezone

from config.enums import AIStatus
from document_ai.embedding.store_registry import get_embedding_store_instance
from document_ai.models import DocumentChunk, DocumentParseResult
from document_ai.orchestration import enqueue_embedding, enqueue_parse
from document_ai.services.environment import get_positive_int_env
from document_ai.tracing_utils import enqueue_kwargs


logger = logging.getLogger(__name__)


def node_has_active_execution(node_id):
    from document_ai.models import ExecutorJobReceipt
    for receipt in ExecutorJobReceipt.objects.filter(status="running"):
        if receipt.target_key == f"node:{node_id}" or node_id in receipt.source_snapshot.get("node_ids", []):
            return True
        # Compatibility for receipts written before snapshots included node IDs.
        if receipt.target_key.startswith("chunks:"):
            ids = [int(value) for value in receipt.target_key[7:].split(",")]
            if DocumentChunk.objects.filter(pk__in=ids, parse_result__node_id=node_id).exists():
                return True
    return False


def get_recovery_stale_minutes() -> int:
    return get_positive_int_env("DOCUMENT_AI_RECOVERY_STALE_MINUTES", 30)


def get_recovery_parse_batch_size() -> int:
    return get_positive_int_env("DOCUMENT_AI_RECOVERY_PARSE_BATCH_SIZE", 50)


def get_recovery_embedding_batch_size() -> int:
    return get_positive_int_env("DOCUMENT_AI_RECOVERY_EMBED_BATCH_SIZE", 200)


def get_max_recovery_attempts() -> int:
    return get_positive_int_env("DOCUMENT_AI_MAX_RECOVERY_ATTEMPTS", 5)


def recovery_cutoff():
    return timezone.now() - timedelta(minutes=get_recovery_stale_minutes())


def get_parse_recovery_node_ids(limit: int) -> list[int]:
    from files.models import Node

    cutoff = recovery_cutoff()
    max_attempts = get_max_recovery_attempts()
    node_ids = set(
        Node.objects.select_related("blob", "parse_result")
        .filter(
            node_type="file",
            trashed=False,
            ai_processing_enabled=True,
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
    chunk_gap_ids = (
        DocumentParseResult.objects.filter(
            node__trashed=False,
            node__ai_processing_enabled=True,
            node__blob__isnull=False,
            status=AIStatus.COMPLETED,
            recovery_attempts__lt=max_attempts,
        )
        .annotate(actual_chunk_rows=Count("chunks", distinct=True))
        .filter(actual_chunk_rows__lt=F("chunk_count"))
        .values_list("node_id", flat=True)
    )
    node_ids.update(chunk_gap_ids)
    return sorted(node_ids)[:limit]


def get_embedding_recovery_chunk_ids(limit: int) -> list[int]:
    cutoff = recovery_cutoff()
    max_attempts = get_max_recovery_attempts()
    store = get_embedding_store_instance()
    existing_embedding_qs = store.completed_embedding_exists(
        chunk_id_ref=OuterRef("pk")
    )
    chunk_qs = (
        DocumentChunk.objects.select_related("parse_result", "parse_result__node")
        .annotate(has_completed_embedding=Exists(existing_embedding_qs))
        .filter(
            parse_result__node__ai_processing_enabled=True,
            parse_result__status=AIStatus.COMPLETED,
            recovery_attempts__lt=max_attempts,
        )
        .filter(
            Q(status=AIStatus.PENDING, updated_at__lte=cutoff)
            | Q(status=AIStatus.PENDING, created_at__lte=cutoff)
            | Q(status=AIStatus.FAILED, updated_at__lte=cutoff)
            | Q(status=AIStatus.FAILED, created_at__lte=cutoff)
            | Q(status=AIStatus.PROCESSING, updated_at__lte=cutoff)
            | Q(status=AIStatus.PROCESSING, created_at__lte=cutoff)
        )
        .filter(has_completed_embedding=False)
        .order_by("id")
    )
    return list(chunk_qs.values_list("id", flat=True)[:limit])


def reset_chunks_to_pending(chunk_ids: list[int]) -> int:
    if not chunk_ids:
        return 0
    return (
        DocumentChunk.objects.filter(id__in=chunk_ids)
        .exclude(status=AIStatus.PENDING)
        .update(status=AIStatus.PENDING, error_message={})
    )


def get_node_ids_for_chunks(chunk_ids: list[int]) -> list[int]:
    if not chunk_ids:
        return []
    return list(
        DocumentChunk.objects.filter(id__in=chunk_ids)
        .order_by()
        .values_list("parse_result__node_id", flat=True)
        .distinct()
    )


def recovery_redis_client():
    from redis import Redis

    redis_url = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    return Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)


def try_acquire_recovery_lock(redis_client, key: str, ttl_seconds: int) -> bool:
    return bool(redis_client.set(key, "1", nx=True, ex=ttl_seconds))


def acquire_recovery_lock(redis_client, key: str, ttl_seconds: int):
    if redis_client is None:
        return True, None
    try:
        return try_acquire_recovery_lock(redis_client, key, ttl_seconds), redis_client
    except Exception as exc:
        logger.warning(
            "Redis unavailable for recovery dedup, proceeding without dedup: %s",
            exc,
        )
        return True, None


def release_recovery_lock(redis_client, key: str) -> None:
    if redis_client is None:
        return
    try:
        redis_client.delete(key)
    except Exception as exc:
        logger.warning("Unable to release recovery lock %s: %s", key, exc)


def recover_document_pipeline_backlog_sync() -> dict:
    parse_limit = get_recovery_parse_batch_size()
    embed_limit = get_recovery_embedding_batch_size()
    stale_minutes = get_recovery_stale_minutes()
    lock_ttl = stale_minutes * 60

    try:
        redis = recovery_redis_client()
    except Exception as exc:
        logger.warning(
            "Redis unavailable for recovery dedup, proceeding without dedup: %s", exc
        )
        redis = None

    parse_node_ids = get_parse_recovery_node_ids(parse_limit)
    recovered_parse_count = 0
    skipped_parse_count = 0
    failed_parse_publish_count = 0
    queued_parse_node_ids = []
    for node_id in parse_node_ids:
        if node_has_active_execution(node_id):
            skipped_parse_count += 1
            continue
        lock_key = f"recovery:parse:{node_id}"
        acquired, redis = acquire_recovery_lock(redis, lock_key, lock_ttl)
        if not acquired:
            skipped_parse_count += 1
            logger.debug("Parse recovery dedup skip: node_id=%s", node_id)
            continue
        try:
            enqueue_parse(node_id, **enqueue_kwargs())
        except Exception as exc:
            failed_parse_publish_count += 1
            release_recovery_lock(redis, lock_key)
            logger.exception(
                "Parse recovery publish failed: node_id=%s, error=%s", node_id, exc
            )
            continue
        queued_parse_node_ids.append(node_id)
        recovered_parse_count += 1

    if queued_parse_node_ids:
        now = timezone.now()
        DocumentParseResult.objects.filter(node_id__in=queued_parse_node_ids).update(
            recovery_attempts=F("recovery_attempts") + 1,
            last_recovered_at=now,
        )

    chunk_ids = get_embedding_recovery_chunk_ids(embed_limit)
    reset_count = 0
    chunk_to_node = (
        dict(
            DocumentChunk.objects.filter(id__in=chunk_ids).values_list(
                "id", "parse_result__node_id"
            )
        )
        if chunk_ids
        else {}
    )
    node_to_chunks: dict[int, list[int]] = {}
    for chunk_id, node_id in chunk_to_node.items():
        node_to_chunks.setdefault(node_id, []).append(chunk_id)

    recovered_embed_count = 0
    skipped_embed_count = 0
    failed_embed_publish_count = 0
    queued_embed_chunk_ids: list[int] = []
    for node_id, node_chunk_ids in node_to_chunks.items():
        if node_has_active_execution(node_id):
            skipped_embed_count += 1
            continue
        lock_key = f"recovery:embed:{node_id}"
        acquired, redis = acquire_recovery_lock(redis, lock_key, lock_ttl)
        if not acquired:
            skipped_embed_count += 1
            logger.debug("Embed recovery dedup skip: node_id=%s", node_id)
            continue
        reset_count += reset_chunks_to_pending(node_chunk_ids)
        try:
            enqueue_embedding(node_id, **enqueue_kwargs())
        except Exception as exc:
            failed_embed_publish_count += 1
            release_recovery_lock(redis, lock_key)
            logger.exception(
                "Embedding recovery publish failed: node_id=%s, error=%s",
                node_id,
                exc,
            )
            continue
        queued_embed_chunk_ids.extend(node_chunk_ids)
        recovered_embed_count += 1

    if queued_embed_chunk_ids:
        now = timezone.now()
        DocumentChunk.objects.filter(id__in=queued_embed_chunk_ids).update(
            recovery_attempts=F("recovery_attempts") + 1,
            last_recovered_at=now,
        )

    summary = {
        "status": (
            "partial"
            if failed_parse_publish_count or failed_embed_publish_count
            else "success"
        ),
        "parse_requeued": recovered_parse_count,
        "parse_skipped_dedup": skipped_parse_count,
        "parse_publish_failed": failed_parse_publish_count,
        "embedding_nodes_requeued": recovered_embed_count,
        "embedding_nodes_skipped_dedup": skipped_embed_count,
        "embedding_nodes_publish_failed": failed_embed_publish_count,
        "chunks_reset_to_pending": reset_count,
        "stale_minutes": stale_minutes,
        "max_recovery_attempts": get_max_recovery_attempts(),
    }
    logger.info("Recovered document pipeline backlog: %s", summary)
    return summary
