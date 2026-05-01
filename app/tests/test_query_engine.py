from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from search_engine.query_engine import QueryAnalyzer


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
    assert plan.semantic_query == ""
    assert any(token.slot == "filename_keyword" for token in plan.matched_tokens)
