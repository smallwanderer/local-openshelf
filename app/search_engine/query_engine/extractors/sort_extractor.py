from __future__ import annotations

from search_engine.query_engine.contracts import SortSpec
from search_engine.query_engine.extractors.base import BaseExtractor


class SortExtractor(BaseExtractor):
    name = "sort"

    def extract(self, context) -> None:
        resources = context.resources.get("sort", {})
        if not isinstance(resources, dict):
            return

        for phrase in sorted(resources.keys(), key=len, reverse=True):
            spec = resources.get(phrase)
            if not isinstance(spec, dict):
                continue

            field = spec.get("field")
            direction = spec.get("direction", "desc")
            if not isinstance(field, str) or direction not in {"asc", "desc"}:
                continue

            for match in self.find_phrase_matches(context.normalized_query, phrase):
                if not context.add_span(
                    slot="sort",
                    start=match.start(),
                    end=match.end(),
                    raw_text=match.group(0),
                    canonical=f"{field}:{direction}",
                ):
                    continue

                strict = context.is_strict_match(match.start())
                if strict:
                    context.add_strict_slot("sort")

                context.sort_specs.append(
                    SortSpec(
                        field=field,
                        direction=direction,
                        scope=str(spec.get("scope", "node")),
                        source_text=match.group(0),
                        strict=strict,
                    )
                )
                context.target_scopes.append(str(spec.get("scope", "node")))
                return
