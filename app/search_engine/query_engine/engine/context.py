from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from search_engine.query_engine.contracts import AnalysisWarning, DateRange, FilterSpec, SortSpec


@dataclass(slots=True)
class AnalysisContext:
    raw_query: str
    normalized_query: str = ""

    primary_locale: str = "ko"
    active_locales: list[str] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)

    intent: str | None = None
    target_scopes: list[str] = field(default_factory=list)
    filters: list[FilterSpec] = field(default_factory=list)
    sort_specs: list[SortSpec] = field(default_factory=list)
    semantic_query: str = ""
    filename_keywords: list[str] = field(default_factory=list)
    date: DateRange | None = None
    file_type: str | None = None
    extension: list[str] = field(default_factory=list)
    owner: str | None = None
    strict_slots: list[str] = field(default_factory=list)

    matched_spans: list[dict[str, Any]] = field(default_factory=list)
    strict_markers: list[dict[str, Any]] = field(default_factory=list)
    consumed_terms: list[str] = field(default_factory=list)
    content_keywords: list[str] = field(default_factory=list)

    warnings: list[AnalysisWarning] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)

    def span_overlaps(self, start: int, end: int) -> bool:
        for span in self.matched_spans:
            if start < span["end"] and end > span["start"]:
                return True
        return False

    def add_span(
        self,
        *,
        slot: str,
        start: int,
        end: int,
        raw_text: str,
        canonical: str | None = None,
        confidence: float = 1.0,
        strict: bool = False,
    ) -> bool:
        if self.span_overlaps(start, end):
            return False

        span = {
            "slot": slot,
            "start": start,
            "end": end,
            "raw_text": raw_text,
            "canonical": canonical,
            "confidence": confidence,
            "strict": strict,
        }
        self.matched_spans.append(span)
        self.consumed_terms.append(raw_text)
        return True

    def add_strict_marker(self, *, start: int, end: int, raw_text: str) -> bool:
        if self.span_overlaps(start, end):
            return False

        marker = {
            "slot": "strict",
            "start": start,
            "end": end,
            "raw_text": raw_text,
            "canonical": None,
            "confidence": 1.0,
            "strict": False,
        }
        self.strict_markers.append(marker)
        self.matched_spans.append(marker)
        self.consumed_terms.append(raw_text)
        return True

    def is_strict_match(self, start: int) -> bool:
        for marker in self.strict_markers:
            if marker["end"] > start:
                continue
            between = self.normalized_query[marker["end"]:start]
            if between.strip():
                continue
            return True
        return False

    def add_strict_slot(self, slot: str) -> None:
        if slot not in self.strict_slots:
            self.strict_slots.append(slot)
