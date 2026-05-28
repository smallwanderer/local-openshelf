from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from uuid import UUID

from document_ai.parsers.text_utils import normalize_extracted_text

from search_engine.query_engine.contracts import DSLFilter, DSLSort, QueryDSL
from search_engine.query_engine.schema import QUERY_DSL_SCHEMA
from search_engine.query_engine.translators import ORMCompiler
from search_engine.query_engine.validators import QueryDSLValidator


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


class QueryPipeline:
    """
    LLM이 추출한 제한 QueryDSL 후보를 시스템 스키마 기준으로 검증하고 ORM kwargs로 컴파일한다.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        schema: dict | None = None,
        max_validation_passes: int = 2,
    ):
        self.enabled = enabled
        self.schema = schema or QUERY_DSL_SCHEMA
        self.max_validation_passes = max(1, max_validation_passes)
        self.validator = QueryDSLValidator(self.schema, strict=False)
        self.compiler = ORMCompiler()

    def run(self, raw_query: str, mode: str, llm_payload: dict, *, source: str = "llm_query_pipeline") -> dict:
        if not self.enabled:
            return self.passthrough(
                raw_query,
                mode,
                source="query_pipeline_disabled",
                warning={
                    "code": "query_pipeline_disabled",
                    "message": "QUERY_PIPELINE_ENABLED is disabled. Original query was used as semantic query.",
                },
            )

        normalized_query = normalize_extracted_text(raw_query or "").strip()
        dsl = self._payload_to_dsl(llm_payload, fallback_query=normalized_query)
        validation_passes = []
        validated = None
        current = dsl
        collected_issues = []

        for pass_index in range(self.max_validation_passes):
            validated = self.validator.validate(current)
            issues = _json_safe(validated.issues)
            validation_passes.append(
                {
                    "pass": pass_index + 1,
                    "valid": validated.valid,
                    "issues": issues,
                    "dsl": _json_safe(validated.dsl),
                }
            )
            collected_issues.extend(issues)
            if not issues:
                break
            current = validated.dsl

        if validated is None:
            validated = self.validator.validate(dsl)

        filter_kwargs, exclude_kwargs, order_by = self.compiler.compile_dsl(validated.dsl, self.schema)
        semantic_query = validated.dsl.semantic_query or normalized_query
        question = self._build_question(raw_query, normalized_query, semantic_query)
        unique_issues = self._dedupe_issues(collected_issues)

        return {
            "status": "partial" if unique_issues else "success",
            "mode": mode,
            "source": source,
            "raw_query": raw_query or "",
            "normalized_query": normalized_query,
            "is_empty": not bool(normalized_query),
            "analysis": {
                "primary_locale": "ko",
                "active_locales": ["ko"],
                "warnings": [
                    {"code": "query_dsl_validation", "message": issue["message"]}
                    for issue in unique_issues
                    if issue.get("level") != "error"
                ],
            },
            "semantic_query": semantic_query,
            "question": question,
            "metadata": {
                "filters": _json_safe(validated.dsl.filters),
                "sorts": _json_safe(validated.dsl.sorts),
                "target_scopes": _json_safe(validated.dsl.target_scopes),
            },
            "dsl": _json_safe(validated.dsl),
            "orm": {
                "base_model": self.schema.get("node", {}).get("model", "files.Node"),
                "filter_kwargs": _json_safe(filter_kwargs),
                "exclude_kwargs": _json_safe(exclude_kwargs),
                "order_by": _json_safe(order_by),
            },
            "validation": {
                "valid": validated.valid,
                "issues": unique_issues,
                "passes": validation_passes,
            },
            "debug": {
                "raw_llm_payload": _json_safe(llm_payload),
                "max_validation_passes": self.max_validation_passes,
            },
        }

    def passthrough(self, raw_query: str, mode: str, *, source: str, warning: dict | None = None) -> dict:
        normalized_query = normalize_extracted_text(raw_query or "").strip()
        question = self._build_question(raw_query, normalized_query, normalized_query)
        warnings = [warning] if warning else []
        return {
            "status": "fallback" if warning else "success",
            "mode": mode,
            "source": source,
            "raw_query": raw_query or "",
            "normalized_query": normalized_query,
            "is_empty": not bool(normalized_query),
            "analysis": {"primary_locale": "ko", "active_locales": ["ko"], "warnings": warnings},
            "semantic_query": normalized_query,
            "question": question,
            "metadata": {"filters": [], "sorts": [], "target_scopes": []},
            "dsl": {"semantic_query": normalized_query, "filters": [], "sorts": [], "target_scopes": []},
            "orm": {
                "base_model": self.schema.get("node", {}).get("model", "files.Node"),
                "filter_kwargs": {},
                "exclude_kwargs": {},
                "order_by": [],
            },
            "validation": {"valid": True, "issues": [], "passes": []},
            "debug": {"passthrough": True},
        }

    def _payload_to_dsl(self, payload: dict, *, fallback_query: str) -> QueryDSL:
        filters = []
        for item in payload.get("filters") or []:
            if not isinstance(item, dict):
                continue
            filters.append(
                DSLFilter(
                    scope=str(item.get("scope") or "node"),
                    field=str(item.get("field") or ""),
                    operator=str(item.get("operator") or "eq"),
                    value=item.get("value"),
                    source_text=str(item.get("source_text") or ""),
                    confidence=self._confidence(item.get("confidence")),
                )
            )

        sorts = []
        for item in payload.get("sorts") or []:
            if not isinstance(item, dict):
                continue
            sorts.append(
                DSLSort(
                    scope=str(item.get("scope") or "node"),
                    field=str(item.get("field") or ""),
                    direction=str(item.get("direction") or "desc"),
                    source_text=str(item.get("source_text") or ""),
                )
            )

        target_scopes = [
            str(scope)
            for scope in (payload.get("target_scopes") or [])
            if isinstance(scope, str) and scope.strip()
        ]
        semantic_query = str(payload.get("semantic_query") or fallback_query or "").strip()

        return QueryDSL(
            semantic_query=semantic_query,
            filters=filters,
            sorts=sorts,
            target_scopes=target_scopes,
        )

    def _confidence(self, value) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 1.0

    def _build_question(self, raw_query: str, normalized_query: str, semantic_query: str) -> dict:
        query = semantic_query or normalized_query or raw_query or ""
        return {
            "original": raw_query or "",
            "normalized": normalized_query or "",
            "semantic": query,
            "residual": query,
            "dense": query,
            "sparse": query,
        }

    def _dedupe_issues(self, issues: list[dict]) -> list[dict]:
        seen = set()
        deduped = []
        for issue in issues:
            key = (
                issue.get("code"),
                issue.get("path"),
                issue.get("message"),
                issue.get("level"),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(issue)
        return deduped
