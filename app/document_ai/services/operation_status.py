from __future__ import annotations

import os
from datetime import timedelta
from time import perf_counter

from django.conf import settings
from django.db import connection
from django.db.models import Count
from django.utils import timezone

from config.enums import AIStatus
from document_ai.models import DocumentChunk, DocumentParseResult
from document_ai.services.embedding_runtime_config import get_active_embedding_runtime
from document_ai.services.operation_metrics import _safe_error_summary
from document_ai.services.server_policy import build_server_policy_payload


def _status_counts(queryset) -> dict:
    counts = {choice: 0 for choice in AIStatus.values}
    for row in queryset.values("status").annotate(count=Count("id")):
        counts[row["status"]] = row["count"]
    return counts


def _database_status() -> dict:
    started = perf_counter()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return {
            "available": True,
            "status": "healthy",
            "latency_ms": round((perf_counter() - started) * 1000, 3),
        }
    except Exception:
        return {
            "available": False,
            "status": "unavailable",
            "latency_ms": round((perf_counter() - started) * 1000, 3),
        }


def _processing_status() -> dict:
    active_runtime = get_active_embedding_runtime()
    active_generation_id = active_runtime.generation_id
    parse_counts = _status_counts(DocumentParseResult.objects.all())
    embedding_counts = _status_counts(DocumentChunk.objects.all())
    stale_before = timezone.now() - timedelta(
        minutes=settings.DOCUMENT_AI_RECOVERY_STALE_MINUTES
    )
    parse_stale = DocumentParseResult.objects.filter(
        status__in=[AIStatus.PENDING, AIStatus.PROCESSING], updated_at__lt=stale_before
    ).count()
    embedding_stale = DocumentChunk.objects.filter(
        status__in=[AIStatus.PENDING, AIStatus.PROCESSING], updated_at__lt=stale_before
    ).count()
    embedding_contract_mismatch = (
        DocumentChunk.objects.filter(
            status=AIStatus.COMPLETED,
        )
        .exclude(parse_result__embedding_generation_id="")
        .exclude(
            parse_result__embedding_generation_id=active_generation_id,
            parse_result__embedding_runtime_fingerprint=active_runtime.runtime_fingerprint,
        )
        .count()
    )

    failures = []
    for row in DocumentParseResult.objects.filter(status=AIStatus.FAILED).select_related("node").order_by("-updated_at")[:5]:
        failures.append(
            {
                "pipeline": "parse",
                "record_id": row.id,
                "document_uid": str(row.node.uid),
                "document_name": row.node.name,
                "failed_at": row.updated_at.isoformat(),
                "recovery_attempts": row.recovery_attempts,
                "error_summary": _safe_error_summary(row.errors),
            }
        )
    for row in DocumentChunk.objects.filter(status=AIStatus.FAILED).select_related("parse_result__node").order_by("-updated_at")[:5]:
        failures.append(
            {
                "pipeline": "embedding",
                "record_id": row.id,
                "document_uid": str(row.parse_result.node.uid),
                "document_name": row.parse_result.node.name,
                "failed_at": row.updated_at.isoformat(),
                "recovery_attempts": row.recovery_attempts,
                "error_summary": _safe_error_summary(row.error_message),
            }
        )
    failures.sort(key=lambda row: row["failed_at"], reverse=True)
    return {
        "parse": {"counts": parse_counts, "stale_count": parse_stale, "unit": "documents"},
        "embedding": {
            "counts": embedding_counts,
            "stale_count": embedding_stale,
            "contract_mismatch_count": embedding_contract_mismatch,
            "active_generation_id": active_generation_id,
            "unit": "chunks",
        },
        "recent_failures": failures[:5],
    }


def build_operation_status() -> dict:
    policy = build_server_policy_payload(probe_embedding=True)
    processing = _processing_status()
    database = _database_status()
    return {
        "generated_at": timezone.now().isoformat(),
        "services": {
            "app": {"available": True, "status": "healthy"},
            "database": database,
            "embedding": policy["embedding"],
            "rag": policy["rag"],
        },
        "processing": processing,
        "admission": {
            "available": False,
            "active": None,
            "limit": policy["rag"].get("serving_concurrency") or None,
            "rejected_count": None,
        },
        "server": {
            "operation_mode": policy["operation_mode"],
            "web_workers": int(os.getenv("WEB_CONCURRENCY", "2")),
            "request_timeout_seconds": int(os.getenv("GUNICORN_TIMEOUT", "360")),
            "parse_worker_concurrency": int(os.getenv("PARSE_WORKER_CONCURRENCY", "1")),
            "embedding_worker_concurrency": int(
                os.getenv("EMBEDDING_WORKER_CONCURRENCY", "1")
            ),
            "build_revision": os.getenv("DOTORI_BUILD_REVISION", os.getenv("GIT_COMMIT", "")),
        },
    }
