from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from search_engine.query_engine.contracts import FilterSpec
from search_engine.query_engine.extractors.base import BaseExtractor


class TimeExtractor(BaseExtractor):
    name = "time"

    def __init__(self, *, today_factory=None, tz_name: str = "Asia/Seoul") -> None:
        self.today_factory = today_factory or date.today
        self.tzinfo = ZoneInfo(tz_name)

    def extract(self, context) -> None:
        resources = context.resources.get("time", {})
        if not isinstance(resources, dict):
            return

        for phrase in sorted(resources.keys(), key=len, reverse=True):
            value = resources.get(phrase)
            if not isinstance(value, dict):
                continue

            for match in self.find_phrase_matches(context.normalized_query, phrase):
                if not context.add_span(
                    slot="date",
                    start=match.start(),
                    end=match.end(),
                    raw_text=match.group(0),
                    canonical=value.get("canonical") or phrase.replace(" ", "_"),
                ):
                    continue

                start, end = self._resolve_range(value)
                if start is None or end is None:
                    continue

                if context.date is None:
                    context.date = self._build_date_range(value, phrase, start, end)

                strict = context.is_strict_match(match.start())
                if strict:
                    context.add_strict_slot("date")

                context.filters.extend(
                    [
                        FilterSpec(
                            field="created_at",
                            operator="gte",
                            value=start,
                            scope="node",
                            source_text=match.group(0),
                        ),
                        FilterSpec(
                            field="created_at",
                            operator="lt",
                            value=end,
                            scope="node",
                            source_text=match.group(0),
                        ),
                    ]
                )
                context.target_scopes.append("node")
                return

    def _resolve_range(self, spec: dict) -> tuple[datetime | None, datetime | None]:
        kind = spec.get("type")
        value = spec.get("value")
        today = self.today_factory()

        if kind == "relative_day" and isinstance(value, int):
            start_date = today + timedelta(days=value)
            end_date = start_date + timedelta(days=1)
            return self._at_midnight(start_date), self._at_midnight(end_date)

        if kind == "relative_week" and isinstance(value, int):
            end_date = today + timedelta(weeks=value + 1)
            start_date = end_date - timedelta(days=7)
            return self._at_midnight(start_date), self._at_midnight(end_date)

        if kind == "recent" and isinstance(value, int):
            start_date = today - timedelta(days=value)
            end_date = today + timedelta(days=1)
            return self._at_midnight(start_date), self._at_midnight(end_date)

        return None, None

    def _build_date_range(self, spec: dict, phrase: str, start: datetime, end: datetime):
        from search_engine.query_engine.contracts import DateRange

        return DateRange(
            kind=spec.get("canonical") or phrase.replace(" ", "_"),
            start=start,
            end=end,
        )

    def _at_midnight(self, target_date: date) -> datetime:
        return datetime.combine(target_date, time.min, tzinfo=self.tzinfo)
