from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from document_ai.parsers.text_utils import normalize_extracted_text


@dataclass(frozen=True)
class RetrievalQueryPlan:
    raw_query: str
    retrieval_query: str
    mode: str
    source: str
    warnings: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def prepare_retrieval_query(raw_query: str, *, mode: str = "search") -> RetrievalQueryPlan:
    """
    Front door for future query understanding before vector retrieval.

    QueryDSL parsing is intentionally experimental and disabled by default. The
    current product path preserves the user's question as the semantic retrieval
    query so RAG/search behavior stays predictable while the parser is evaluated.
    """
    normalized_query = normalize_extracted_text(raw_query or "").strip()
    frontend_mode = os.getenv("QUERY_FRONTEND_MODE", "passthrough").strip().lower()

    if frontend_mode in {"querydsl", "experimental_querydsl"}:
        return RetrievalQueryPlan(
            raw_query=raw_query or "",
            retrieval_query=normalized_query,
            mode=mode,
            source="querydsl_experimental_passthrough",
            warnings=[
                {
                    "code": "querydsl_experimental",
                    "message": "QueryDSL parsing is experimental; retrieval currently uses the original semantic query.",
                }
            ],
            metadata={
                "querydsl_enabled": False,
                "frontend_mode": frontend_mode,
            },
        )

    return RetrievalQueryPlan(
        raw_query=raw_query or "",
        retrieval_query=normalized_query,
        mode=mode,
        source="passthrough",
    )
