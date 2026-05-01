from __future__ import annotations

from search_engine.query_engine.contracts import FilterSpec
from search_engine.query_engine.extractors.base import BaseExtractor


class StatusExtractor(BaseExtractor):
    name = "status"

    def extract(self, context) -> None:
        resources = context.resources.get("status", {})
        if not isinstance(resources, dict):
            return

        for phrase in sorted(resources.keys(), key=len, reverse=True):
            value = resources.get(phrase)
            if not isinstance(value, dict):
                continue

            for match in self.find_phrase_matches(context.normalized_query, phrase):
                scope = value.get("scope")
                status_value = value.get("value")
                if not isinstance(scope, str) or not isinstance(status_value, str):
                    continue

                if not context.add_span(
                    slot="status",
                    start=match.start(),
                    end=match.end(),
                    raw_text=match.group(0),
                    canonical=status_value,
                ):
                    continue

                context.filters.append(
                    FilterSpec(
                        field="status",
                        operator="eq",
                        value=status_value,
                        scope=scope,
                        source_text=match.group(0),
                    )
                )
                context.target_scopes.append(scope)
                return
