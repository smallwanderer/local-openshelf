from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.utils import timezone

from workspaces.models import WorkspaceQualityProfileRevision


SCHEMA_VERSION = 1

RETRIEVAL_SCHEMA: dict[str, dict[str, Any]] = {
    "dense_weight": {"type": "number", "tier": "core", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "effects": ["retrieval_quality"]},
    "sparse_weight": {"type": "number", "tier": "core", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "effects": ["retrieval_quality"]},
    "search_top_k": {"type": "integer", "tier": "core", "minimum": 1, "maximum": 50, "step": 1, "effects": ["retrieval_quality", "latency"]},
    "rag_search_top_k": {"type": "integer", "tier": "core", "minimum": 1, "maximum": 10, "step": 1, "effects": ["context_quality", "latency"]},
    "retrieval_threshold": {"type": "number", "tier": "core", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "effects": ["retrieval_quality"], "nullable": True},
    "evidence_top_k": {"type": "integer", "tier": "core", "minimum": 1, "maximum": 10, "step": 1, "effects": ["context_quality"]},
    "evidence_context_window": {"type": "integer", "tier": "core", "minimum": 0, "maximum": 3, "step": 1, "effects": ["context_quality", "latency"]},
    "candidate_multiplier": {"type": "integer", "tier": "advanced", "minimum": 1, "maximum": 50, "step": 1, "effects": ["retrieval_quality", "latency"]},
    "per_node_candidate_cap": {"type": "integer", "tier": "advanced", "minimum": 1, "maximum": 20, "step": 1, "effects": ["retrieval_quality"]},
    "query_sparse_top_n": {"type": "integer", "tier": "advanced", "minimum": 1, "maximum": 256, "step": 1, "effects": ["retrieval_quality", "latency"]},
    "pooling_method": {"type": "enum", "tier": "advanced", "choices": ["normalized_logsumexp", "normalized_softmax", "max"], "effects": ["retrieval_quality"]},
    "pool_top_k": {"type": "integer", "tier": "advanced", "minimum": 1, "maximum": 20, "step": 1, "effects": ["retrieval_quality"]},
    "pool_tau": {"type": "number", "tier": "advanced", "minimum": 0.1, "maximum": 20.0, "step": 0.1, "effects": ["retrieval_quality"]},
    "doc_length_penalty_alpha": {"type": "number", "tier": "advanced", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "effects": ["retrieval_quality"]},
    "contextual_compression": {"type": "boolean", "tier": "advanced", "effects": ["context_quality", "latency"]},
}

GENERATION_SCHEMA: dict[str, dict[str, Any]] = {
    "max_output_tokens": {"type": "integer", "tier": "core", "minimum": 64, "maximum": 8192, "step": 64, "effects": ["generation_quality", "latency"]},
    "temperature": {"type": "number", "tier": "core", "minimum": 0.0, "maximum": 2.0, "step": 0.05, "effects": ["generation_quality"]},
    "top_p": {"type": "number", "tier": "advanced", "minimum": 0.01, "maximum": 1.0, "step": 0.01, "effects": ["generation_quality"]},
}


@dataclass
class RetrievalProfileError(Exception):
    code: str
    message: str
    status: int
    details: dict[str, Any] | None = None

    def __str__(self):
        return self.message


def retrieval_defaults() -> dict[str, Any]:
    return {
        "dense_weight": float(getattr(settings, "EMBEDDING_HYBRID_DENSE_WEIGHT", 0.3)),
        "sparse_weight": float(getattr(settings, "EMBEDDING_HYBRID_SPARSE_WEIGHT", 0.7)),
        "search_top_k": 5,
        "rag_search_top_k": int(getattr(settings, "RAG_SEARCH_TOP_K", 3)),
        "retrieval_threshold": getattr(settings, "RAG_RETRIEVAL_THRESHOLD", None),
        "evidence_top_k": int(getattr(settings, "EMBEDDING_EVIDENCE_TOP_K", 3)),
        "evidence_context_window": int(getattr(settings, "EMBEDDING_EVIDENCE_CONTEXT_WINDOW", 1)),
        "candidate_multiplier": int(getattr(settings, "EMBEDDING_HYBRID_CANDIDATE_MULTIPLIER", 12)),
        "per_node_candidate_cap": int(getattr(settings, "EMBEDDING_PER_NODE_CANDIDATE_CAP", 4)),
        "query_sparse_top_n": int(getattr(settings, "EMBEDDING_QUERY_SPARSE_TOP_N", 32)),
        "pooling_method": getattr(settings, "EMBEDDING_DOC_POOLING_METHOD", "normalized_logsumexp"),
        "pool_top_k": int(getattr(settings, "EMBEDDING_DOC_POOL_TOP_K", 5)),
        "pool_tau": float(getattr(settings, "EMBEDDING_DOC_POOL_TAU", 5.0)),
        "doc_length_penalty_alpha": float(getattr(settings, "EMBEDDING_DOC_LENGTH_PENALTY_ALPHA", 0.1)),
        "contextual_compression": {"enabled": bool(getattr(settings, "CONTEXTUAL_COMPRESSION_ENABLED", False))},
    }


def generation_defaults() -> dict[str, Any]:
    return {
        "max_output_tokens": int(getattr(settings, "RAG_MAX_TOKENS", 512)),
        "temperature": float(getattr(settings, "RAG_TEMPERATURE", 0.2)),
        "top_p": float(getattr(settings, "RAG_TOP_P", 0.9)),
    }


def _effective(overrides: dict[str, Any] | None) -> dict[str, Any]:
    effective = retrieval_defaults()
    effective.update(overrides or {})
    return effective


def _effective_generation(overrides: dict[str, Any] | None) -> dict[str, Any]:
    effective = generation_defaults()
    effective.update(overrides or {})
    return effective


def _same(left: Any, right: Any) -> bool:
    return left == right


def _validate_value(name: str, value: Any, spec: dict[str, Any], errors: dict[str, list[str]]):
    if value is None and spec.get("nullable"):
        return
    kind = spec["type"]
    if kind == "integer":
        valid_type = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "number":
        valid_type = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
    elif kind == "boolean":
        valid_type = isinstance(value, dict) and isinstance(value.get("enabled"), bool) and set(value) == {"enabled"}
    elif kind == "enum":
        valid_type = isinstance(value, str) and value in spec["choices"]
    else:
        valid_type = False
    if not valid_type:
        errors.setdefault(name, []).append(f"Invalid {kind} value.")
        return
    if kind in {"integer", "number"}:
        if value < spec["minimum"] or value > spec["maximum"]:
            errors.setdefault(name, []).append(
                f"Must be between {spec['minimum']} and {spec['maximum']}."
            )


def validate_retrieval_config(config: dict[str, Any]) -> list[str]:
    errors: dict[str, list[str]] = {}
    unknown = sorted(set(config) - set(RETRIEVAL_SCHEMA))
    for name in unknown:
        errors.setdefault(name, []).append("Unknown retrieval setting.")
    for name, spec in RETRIEVAL_SCHEMA.items():
        if name not in config:
            errors.setdefault(name, []).append("This setting is required.")
        else:
            _validate_value(name, config[name], spec, errors)
    if not errors:
        if abs(float(config["dense_weight"]) + float(config["sparse_weight"]) - 1.0) > 1e-6:
            errors["weights"] = ["dense_weight and sparse_weight must add up to 1.0."]
        if config["pooling_method"] == "max" and config["pool_tau"] != retrieval_defaults()["pool_tau"]:
            errors["pool_tau"] = ["pool_tau cannot be overridden when pooling_method is max."]
    if errors:
        raise RetrievalProfileError(
            "PROFILE_VALIDATION_FAILED",
            "Retrieval profile validation failed.",
            400,
            errors,
        )
    warnings = []
    if config["evidence_top_k"] > config["pool_top_k"]:
        warnings.append("evidence_top_k is greater than pool_top_k.")
    return warnings


def validate_generation_config(config: dict[str, Any]) -> list[str]:
    errors: dict[str, list[str]] = {}
    unknown = sorted(set(config) - set(GENERATION_SCHEMA))
    for name in unknown:
        errors.setdefault(name, []).append("Unknown generation setting.")
    for name, spec in GENERATION_SCHEMA.items():
        if name not in config:
            errors.setdefault(name, []).append("This setting is required.")
        else:
            _validate_value(name, config[name], spec, errors)
    if errors:
        raise RetrievalProfileError(
            "PROFILE_VALIDATION_FAILED",
            "Generation profile validation failed.",
            400,
            errors,
        )
    return []


def _sparse_overrides(config: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in config.items() if not _same(value, defaults[key])}


def _active_for_workspace(workspace, *, actor=None, lock=False):
    query = WorkspaceQualityProfileRevision.objects.filter(
        workspace=workspace,
        status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
    )
    if lock:
        active = query.select_for_update().first()
    else:
        active = query.select_related("created_by", "based_on").first()
    if active is None:
        active = WorkspaceQualityProfileRevision.objects.create(
            workspace=workspace,
            version=1,
            revision=1,
            status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
            changed_axes=[WorkspaceQualityProfileRevision.AXIS_RETRIEVAL],
            retrieval_config={},
            validation_state="verified",
            created_by=actor,
            applied_at=timezone.now(),
            note="Server defaults",
        )
    return active


def get_effective_retrieval_config(workspace) -> dict[str, Any]:
    if workspace is None:
        return retrieval_defaults()
    active = WorkspaceQualityProfileRevision.objects.filter(
        workspace=workspace,
        status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
    ).only("retrieval_config").first()
    return _effective(active.retrieval_config if active else {})


def get_effective_generation_config(workspace) -> dict[str, Any]:
    if workspace is None:
        return generation_defaults()
    active = WorkspaceQualityProfileRevision.objects.filter(
        workspace=workspace,
        status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
    ).only("generation_config").first()
    return _effective_generation(active.generation_config if active else {})


def retrieval_tuning_params(config: dict[str, Any]) -> dict[str, Any]:
    excluded = {"search_top_k", "rag_search_top_k", "retrieval_threshold"}
    return {key: value for key, value in config.items() if key not in excluded}


def profile_threshold_to_retriever(threshold: float | None) -> float | None:
    """Translate the profile's normalized minimum relevance into the active store contract."""
    if threshold is None:
        return None
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    strategy = get_active_embedding_runtime().distance_strategy
    if strategy == "cosine":
        return 1.0 - threshold
    if strategy == "l2":
        return None if threshold <= 0 else (1.0 / threshold) - 1.0
    return threshold
