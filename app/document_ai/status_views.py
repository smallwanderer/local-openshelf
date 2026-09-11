from __future__ import annotations

import os

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from accounts.api_responses import session_access_error
from document_ai.services.embedding_runtime_config import (
    EmbeddingRuntimeConfigError,
    load_embedding_runtime,
)
from document_ai.services.rag_runtime_config import (
    load_llm_runtime_status,
    probe_server_rag_runtime,
    target_from_persisted_config,
)


def _rag_summary() -> dict:
    target = target_from_persisted_config()
    status = load_llm_runtime_status()
    available = probe_server_rag_runtime(target) if target else False
    status_name = "healthy" if available else "unavailable"
    serving_profile = target.serving_profile if target else {}
    return {
        "configured": target is not None,
        "available": available,
        "status": status_name if target else "not_configured",
        "reason_code": str(status.get("reason_code") or ("RUNTIME_HEALTHCHECK_FAILED" if target and not available else "")),
        "updated_at": status.get("updated_at"),
        "model": target.model if target else "",
        "runtime": target.runtime if target else "",
        "priority_preset": target.priority_preset if target else "balanced",
        "selection_mode": target.selection_mode if target else "automatic",
        "serving_concurrency": serving_profile.get("serving_concurrency", 0),
    }


def _embedding_summary(operation_mode: str) -> dict:
    enabled = operation_mode in {"rag", "search"}
    try:
        runtime = load_embedding_runtime()
    except EmbeddingRuntimeConfigError:
        return {
            "enabled": enabled,
            "configured": False,
            "status": "invalid_config",
            "model": "",
            "provider": "",
            "dimension": 0,
            "sparse_enabled": False,
            "distance_strategy": "",
        }
    return {
        "enabled": enabled,
        "configured": True,
        "status": "configured" if enabled else "disabled",
        "model": runtime.model_id,
        "provider": runtime.provider,
        "dimension": runtime.dimension,
        "sparse_enabled": runtime.supports_sparse,
        "distance_strategy": runtime.distance_strategy,
    }


@require_GET
def server_policy(request):
    access_error = session_access_error(request.user)
    if access_error is not None:
        return access_error

    operation_mode = os.environ.get("DOTORI_OPERATION_MODE", "search").strip().lower()
    return JsonResponse(
        {
            "ok": True,
            "operation_mode": operation_mode,
            "policy": {
                "search_strategy": "hybrid",
                "search_top_k": settings.RAG_SEARCH_TOP_K,
                "retrieval_threshold": settings.RAG_RETRIEVAL_THRESHOLD,
            },
            "rag": _rag_summary(),
            "embedding": _embedding_summary(operation_mode),
        }
    )
