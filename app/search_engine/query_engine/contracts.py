from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Operator = Literal["eq", "neq", "lt", "lte", "gt", "gte", "contains", "in"]
SortDirection = Literal["asc", "desc"]


@dataclass(slots=True)
class FilterSpec:
    field: str
    operator: Operator
    value: Any
    scope: str
    source_text: str = ""
    confidence: float = 1.0
    compileable: bool = True


@dataclass(slots=True)
class SortSpec:
    field: str
    direction: SortDirection = "desc"
    scope: str = "node"
    source_text: str = ""
    strict: bool = False


@dataclass(slots=True)
class DateRange:
    kind: str | None = None
    start: Any = None
    end: Any = None


@dataclass(slots=True)
class MatchedToken:
    slot: str
    raw_text: str
    canonical: str | None = None
    start: int = 0
    end: int = 0
    confidence: float = 1.0


@dataclass(slots=True)
class AnalysisWarning:
    code: str
    message: str


@dataclass(slots=True)
class DSLFilter:
    scope: str
    field: str
    operator: Operator
    value: Any
    source_text: str = ""
    confidence: float = 1.0


@dataclass(slots=True)
class DSLSort:
    scope: str
    field: str
    direction: SortDirection = "desc"
    source_text: str = ""


@dataclass(slots=True)
class QueryDSL:
    semantic_query: str = ""
    filters: list[DSLFilter] = field(default_factory=list)
    sorts: list[DSLSort] = field(default_factory=list)
    target_scopes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ValidationIssue:
    code: str
    message: str
    path: str = ""
    level: Literal["warning", "error"] = "warning"


@dataclass(slots=True)
class ValidatedQueryDSL:
    dsl: QueryDSL
    valid: bool = True
    issues: list[ValidationIssue] = field(default_factory=list)


@dataclass(slots=True)
class SearchPlan:
    raw_query: str
    normalized_query: str
    primary_locale: str
    active_locales: list[str]

    intent: str | None = None
    target_scopes: list[str] = field(default_factory=list)
    semantic_query: str = ""
    filename_keywords: list[str] = field(default_factory=list)
    date: DateRange | None = None
    file_type: str | None = None
    extension: list[str] = field(default_factory=list)
    owner: str | None = None

    structured_filters: list[FilterSpec] = field(default_factory=list)
    sort_specs: list[SortSpec] = field(default_factory=list)
    strict_slots: list[str] = field(default_factory=list)
    matched_tokens: list[MatchedToken] = field(default_factory=list)

    orm_filter_kwargs: dict[str, Any] = field(default_factory=dict)
    orm_exclude_kwargs: dict[str, Any] = field(default_factory=dict)

    residual_query: str = ""
    dense_query: str = ""
    sparse_query: str = ""

    warnings: list[AnalysisWarning] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)
