from datetime import date
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.unit

from search_engine.query_engine import QueryAnalyzer
from search_engine.query_engine.contracts import DSLFilter, DSLSort, QueryDSL
from search_engine.query_engine.query_pipeline import QueryPipeline
from search_engine.query_engine.translators import ORMCompiler, QueryDSLAdapter
from search_engine.query_engine.validators import QueryDSLValidator
from document_ai.tasks import _extract_llm_final_content


def test_query_engine_extracts_time_filetype_and_keywords():
    analyzer = QueryAnalyzer(today_factory=lambda: date(2026, 4, 10))

    plan = analyzer.analyze("last week pdf contract search")

    assert plan.intent == "search"
    assert plan.semantic_query == "contract"
    assert plan.date is not None
    assert plan.date.kind == "last_week"
    assert plan.file_type == "pdf"
    assert plan.extension == ["pdf"]
    assert any(spec.field == "ext" and spec.value == "pdf" for spec in plan.structured_filters)
    assert any(spec.field == "created_at" and spec.operator == "gte" for spec in plan.structured_filters)
    assert any(spec.field == "created_at" and spec.operator == "lt" for spec in plan.structured_filters)
    assert plan.residual_query == "contract"
    assert "node" in plan.target_scopes


def test_query_engine_extracts_status_and_symbol_field_filter():
    analyzer = QueryAnalyzer(today_factory=lambda: date(2026, 4, 10))

    plan = analyzer.analyze("uploaded size>10 budget")

    assert any(
        spec.scope == "fileblob" and spec.field == "status" and spec.value == "uploaded"
        for spec in plan.structured_filters
    )
    assert any(
        spec.scope == "fileblob" and spec.field == "size" and spec.operator == "gt" and spec.value == 10
        for spec in plan.structured_filters
    )
    assert plan.orm_filter_kwargs["blob__status"] == "uploaded"
    assert plan.orm_filter_kwargs["blob__size__gt"] == 10
    assert plan.residual_query == "budget"


def test_query_engine_extracts_owner_sort_and_strict_flags():
    analyzer = QueryAnalyzer(today_factory=lambda: date(2026, 4, 10))

    plan = analyzer.analyze("only my files newest first budget")

    assert plan.owner == "self"
    assert plan.semantic_query == "budget"
    assert "owner" in plan.strict_slots
    assert plan.sort_specs
    assert plan.sort_specs[0].field == "created_at"
    assert plan.sort_specs[0].direction == "desc"
    assert plan.orm_filter_kwargs == {}


def test_query_engine_extracts_filename_keyword_hints():
    analyzer = QueryAnalyzer(today_factory=lambda: date(2026, 4, 10))

    plan = analyzer.analyze('files named "annual report" pdf')

    assert plan.filename_keywords == ["annual report"]
    assert plan.file_type == "pdf"
    assert plan.semantic_query == "named"
    assert any(token.slot == "filename_keyword" for token in plan.matched_tokens)


def test_query_dsl_validator_compiles_only_allowed_filters():
    dsl = QueryDSL(
        semantic_query="contract",
        filters=[
            DSLFilter(scope="node", field="ext", operator="eq", value="pdf"),
            DSLFilter(scope="fileblob", field="size", operator="gt", value="10"),
            DSLFilter(scope="node", field="password", operator="contains", value="secret"),
        ],
        sorts=[
            DSLSort(scope="node", field="created_at", direction="desc"),
        ],
        target_scopes=["node", "fileblob", "unsafe_scope"],
    )

    validated = QueryDSLValidator(strict=False).validate(dsl)
    filter_kwargs, exclude_kwargs, order_by = ORMCompiler().compile_dsl(validated.dsl)

    assert validated.valid is True
    assert any(issue.code == "unknown_field" for issue in validated.issues)
    assert any(issue.code == "unknown_scope" for issue in validated.issues)
    assert filter_kwargs == {
        "ext": "pdf",
        "blob__size__gt": 10,
    }
    assert exclude_kwargs == {}
    assert order_by == ["-created_at"]


def test_query_analyzer_plan_can_be_adapted_to_validated_dsl():
    analyzer = QueryAnalyzer(today_factory=lambda: date(2026, 4, 10))
    plan = analyzer.analyze("last week pdf contract search")

    dsl = QueryDSLAdapter().from_search_plan(plan)
    validated = QueryDSLValidator(strict=False).validate(dsl)
    filter_kwargs, _, _ = ORMCompiler().compile_dsl(validated.dsl)

    assert validated.valid is True
    assert validated.issues == []
    assert filter_kwargs["ext"] == "pdf"
    assert "created_at__gte" in filter_kwargs
    assert "created_at__lt" in filter_kwargs


def test_llm_final_content_extractor_requires_final_after_thought():
    assert _extract_llm_final_content("<|channel>thought\nreasoning only") == ""
    assert (
        _extract_llm_final_content('<|channel>thought\nreasoning\n<|channel>final\n{"semantic_query":"계약"}')
        == '{"semantic_query":"계약"}'
    )
    assert (
        _extract_llm_final_content('<|channel>thought\nreasoning\n<channel|>{"semantic_query":"계약"}')
        == '{"semantic_query":"계약"}'
    )
    assert _extract_llm_final_content('{"semantic_query":"계약"}') == '{"semantic_query":"계약"}'


def test_query_pipeline_compiles_llm_dsl_to_orm_kwargs():
    payload = {
        "semantic_query": "계약 문서",
        "filters": [
            {"scope": "node", "field": "ext", "operator": "eq", "value": "pdf"},
            {"scope": "node", "field": "created_at", "operator": "gte", "value": "2026-04-03T00:00:00+09:00"},
            {"scope": "node", "field": "created_at", "operator": "lt", "value": "2026-04-10T00:00:00+09:00"},
        ],
        "sorts": [{"scope": "node", "field": "created_at", "direction": "desc"}],
        "target_scopes": ["node"],
    }

    result = QueryPipeline(max_validation_passes=2).run("지난주 pdf 계약 문서", "search", payload)

    assert result["status"] == "success"
    assert result["semantic_query"] == "계약 문서"
    assert result["orm"]["filter_kwargs"]["ext"] == "pdf"
    assert "created_at__gte" in result["orm"]["filter_kwargs"]
    assert "created_at__lt" in result["orm"]["filter_kwargs"]
    assert result["orm"]["order_by"] == ["-created_at"]


def test_query_pipeline_prunes_invalid_llm_filters_recursively():
    payload = {
        "semantic_query": "계약 문서",
        "filters": [
            {"scope": "node", "field": "password", "operator": "contains", "value": "secret"},
            {"scope": "node", "field": "trashed", "operator": "eq", "value": True},
            {"scope": "node", "field": "deleted_at", "operator": "gte", "value": "2026-04-01"},
            {"scope": "node", "field": "node_type", "operator": "eq", "value": "trash"},
            {"scope": "fileblob", "field": "status", "operator": "eq", "value": "uploaded"},
            {"scope": "node", "field": "ext", "operator": "eq", "value": "pdf"},
        ],
        "sorts": [
            {"scope": "node", "field": "unsafe", "direction": "desc"},
            {"scope": "node", "field": "deleted_at", "direction": "desc"},
            {"scope": "fileblob", "field": "status", "direction": "asc"},
        ],
        "target_scopes": ["node", "unsafe_scope"],
    }

    result = QueryPipeline(max_validation_passes=2).run("pdf 계약 문서", "search", payload)

    assert result["status"] == "partial"
    assert result["orm"]["filter_kwargs"] == {"ext": "pdf"}
    assert result["orm"]["order_by"] == []
    assert any(issue["code"] == "unknown_field" for issue in result["validation"]["issues"])
    assert any(issue["code"] == "value_not_allowed" for issue in result["validation"]["issues"])
    assert any(item["pass"] == 2 and item["issues"] == [] for item in result["validation"]["passes"])


def test_query_pipeline_can_be_disabled():
    result = QueryPipeline(enabled=False).run("pdf 계약 문서", "search", {"semantic_query": "계약"})

    assert result["source"] == "query_pipeline_disabled"
    assert result["semantic_query"] == "pdf 계약 문서"
    assert result["orm"]["filter_kwargs"] == {}
