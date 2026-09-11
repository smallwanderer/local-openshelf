from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from config.enums import QueryAnswerMode, QueryIntent
from document_ai.models import QueryUnderstandingLog
from document_ai.parsers.text_utils import normalize_extracted_text


@dataclass(frozen=True)
class RetrievalQueryPlan:
    raw_query: str
    retrieval_query: str
    mode: str
    source: str
    query_log: QueryUnderstandingLog | None = None
    intent: str = QueryIntent.DOCUMENT_QUESTION.value
    answer_mode: str = QueryAnswerMode.RAG.value
    retrieval_required: bool = True
    confidence: float = 0.0
    warnings: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _warnings_from_result(result: dict) -> list[dict[str, str]]:
    warnings = result.get("analysis", {}).get("warnings") or []
    return [
        {
            "code": str(item.get("code") or "query_warning"),
            "message": str(item.get("message") or ""),
        }
        for item in warnings
        if isinstance(item, dict)
    ]


def _create_query_log(*, owner, workspace=None, raw_query: str, mode: str, result: dict, error_message: str = ""):
    if owner is None or not getattr(owner, "is_authenticated", False):
        return None

    classification = result.get("classification") or {}
    return QueryUnderstandingLog.objects.create(
        owner=owner,
        workspace=workspace,
        mode=mode,
        raw_query=raw_query or "",
        normalized_query=result.get("normalized_query") or "",
        semantic_query=result.get("semantic_query") or "",
        intent=result.get("intent") or classification.get("intent") or QueryIntent.AMBIGUOUS.value,
        answer_mode=result.get("answer_mode") or classification.get("answer_mode") or QueryAnswerMode.AMBIGUOUS.value,
        retrieval_required=bool(result.get("retrieval_required", True)),
        confidence=float(result.get("confidence") or 0.0),
        reason=str(classification.get("reason") or "")[:255],
        source=result.get("source") or "",
        status=result.get("status") or "success",
        warnings=_warnings_from_result(result),
        classification=classification,
        query_dsl=result.get("dsl") or {},
        orm=result.get("orm") or {},
        raw_result=result,
        error_message=error_message,
    )


def _plan_from_result(raw_query: str, *, mode: str, result: dict, query_log=None) -> RetrievalQueryPlan:
    retrieval_query = result.get("semantic_query") or ""
    return RetrievalQueryPlan(
        raw_query=raw_query or "",
        retrieval_query=retrieval_query,
        mode=mode,
        source=result.get("source") or "llm_query_pipeline",
        query_log=query_log,
        intent=result.get("intent") or QueryIntent.AMBIGUOUS.value,
        answer_mode=result.get("answer_mode") or QueryAnswerMode.AMBIGUOUS.value,
        retrieval_required=bool(result.get("retrieval_required", True)),
        confidence=float(result.get("confidence") or 0.0),
        warnings=_warnings_from_result(result),
        metadata={
            "classification": result.get("classification") or {},
            "filters": result.get("metadata", {}).get("filters") or [],
            "sorts": result.get("metadata", {}).get("sorts") or [],
            "target_scopes": result.get("metadata", {}).get("target_scopes") or [],
            "orm": result.get("orm") or {},
            "dsl": result.get("dsl") or {},
        },
    )


def _passthrough_plan(raw_query: str, *, mode: str, owner=None, workspace=None, source: str = "passthrough", warning: dict | None = None, skip_log: bool = False):
    normalized_query = normalize_extracted_text(raw_query or "").strip()
    result = {
        "status": "fallback" if warning else "success",
        "mode": mode,
        "source": source,
        "raw_query": raw_query or "",
        "normalized_query": normalized_query,
        "semantic_query": normalized_query,
        "intent": QueryIntent.DOCUMENT_QUESTION.value,
        "answer_mode": QueryAnswerMode.RAG.value if mode == "rag" else QueryAnswerMode.GENERAL.value,
        "retrieval_required": bool(normalized_query),
        "confidence": 0.0,
        "classification": {
            "intent": QueryIntent.DOCUMENT_QUESTION.value,
            "answer_mode": QueryAnswerMode.RAG.value if mode == "rag" else QueryAnswerMode.GENERAL.value,
            "retrieval_required": bool(normalized_query),
            "confidence": 0.0,
            "reason": "",
        },
        "analysis": {"warnings": [warning] if warning else []},
        "metadata": {"filters": [], "sorts": [], "target_scopes": []},
        "dsl": {"semantic_query": normalized_query, "filters": [], "sorts": [], "target_scopes": []},
        "orm": {"filter_kwargs": {}, "exclude_kwargs": {}, "order_by": []},
    }
    query_log = None if skip_log else _create_query_log(owner=owner, workspace=workspace, raw_query=raw_query, mode=mode, result=result)
    return _plan_from_result(raw_query, mode=mode, result=result, query_log=query_log)


def prepare_retrieval_query(raw_query: str, *, mode: str = "search", owner=None, workspace=None) -> RetrievalQueryPlan:
    """
    Query-understanding boundary before vector retrieval.

    The current in-process implementation calls the owning parser service
    directly. This module is the boundary to replace with a remote
    query-understanding service when the deployment profile enables it.
    """
    normalized_query = normalize_extracted_text(raw_query or "").strip()
    frontend_mode = os.getenv("QUERY_UNDERSTANDING_FRONTEND_MODE", "passthrough").strip().lower()

    if not normalized_query:
        return _passthrough_plan(raw_query, mode=mode, owner=owner, workspace=workspace, source="empty_query")

    if frontend_mode in {"passthrough", "off", "disabled"}:
        return _passthrough_plan(
            raw_query,
            mode=mode,
            owner=owner,
            workspace=workspace,
            source="llm_query_frontend_disabled",
            warning={
                "code": "llm_query_frontend_disabled",
                "message": "QUERY_UNDERSTANDING_FRONTEND_MODE disabled LLM query understanding.",
            },
            skip_log=True,
        )

    try:
        from document_ai.query_understanding.parser_service import parse_user_query_sync

        result = parse_user_query_sync(normalized_query, mode)
        query_log = _create_query_log(owner=owner, workspace=workspace, raw_query=raw_query, mode=mode, result=result)
        return _plan_from_result(raw_query, mode=mode, result=result, query_log=query_log)
    except Exception as exc:
        return _passthrough_plan(
            raw_query,
            mode=mode,
            owner=owner,
            workspace=workspace,
            source="llm_query_frontend_failed",
            warning={
                "code": "llm_query_frontend_failed",
                "message": str(exc),
            },
        )
