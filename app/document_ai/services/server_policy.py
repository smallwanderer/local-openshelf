from __future__ import annotations

import os

import requests

from document_ai.services.embedding_runtime_config import (
    EmbeddingRuntimeConfigError,
    load_embedding_runtime,
)
from document_ai.services.rag_runtime_config import (
    load_llm_runtime_status,
    probe_server_rag_runtime,
    target_from_persisted_config,
)


def build_rag_summary() -> dict:
    target = target_from_persisted_config()
    status = load_llm_runtime_status()
    available = probe_server_rag_runtime(target) if target else False
    serving_profile = target.serving_profile if target else {}
    return {
        "configured": target is not None,
        "available": available,
        "status": ("healthy" if available else "unavailable") if target else "not_configured",
        "reason_code": str(
            status.get("reason_code")
            or ("RUNTIME_HEALTHCHECK_FAILED" if target and not available else "")
        ),
        "updated_at": status.get("updated_at"),
        "model": target.model if target else "",
        "runtime": target.runtime if target else "",
        "priority_preset": target.priority_preset if target else "balanced",
        "selection_mode": target.selection_mode if target else "automatic",
        "serving_concurrency": serving_profile.get("serving_concurrency", 0),
    }


def build_embedding_summary(operation_mode: str, *, probe: bool = False) -> dict:
    enabled = operation_mode in {"rag", "search"}
    try:
        runtime = load_embedding_runtime()
    except EmbeddingRuntimeConfigError:
        return {
            "enabled": enabled,
            "configured": False,
            "available": False,
            "status": "invalid_config",
            "reason_code": "EMBEDDING_CONFIG_INVALID",
            "model": "",
            "provider": "",
            "dimension": 0,
            "sparse_enabled": False,
            "distance_strategy": "",
        }

    available = enabled
    status_name = "configured" if enabled else "disabled"
    reason_code = ""
    if enabled and probe:
        service_url = os.getenv("EMBEDDING_SERVICE_URL", "http://embedding-executor:8001").rstrip("/")
        try:
            response = requests.get(f"{service_url}/readyz", timeout=2)
            available = response.ok
            status_name = "ready" if response.ok else "not_ready"
            if not response.ok:
                reason_code = "EMBEDDING_READINESS_FAILED"
        except requests.RequestException:
            available = False
            status_name = "unavailable"
            reason_code = "EMBEDDING_SERVICE_UNAVAILABLE"

    return {
        "enabled": enabled,
        "configured": True,
        "available": available,
        "status": status_name,
        "reason_code": reason_code,
        "model": runtime.model_id,
        "provider": runtime.provider,
        "dimension": runtime.dimension,
        "sparse_enabled": runtime.supports_sparse,
        "distance_strategy": runtime.distance_strategy,
    }


def build_server_policy_payload(*, probe_embedding: bool = False) -> dict:
    operation_mode = os.environ.get("DOTORI_OPERATION_MODE", "search").strip().lower()
    return {
        "operation_mode": operation_mode,
        "rag": build_rag_summary(),
        "embedding": build_embedding_summary(operation_mode, probe=probe_embedding),
    }
