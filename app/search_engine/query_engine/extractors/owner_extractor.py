from __future__ import annotations

from search_engine.query_engine.contracts import FilterSpec
from search_engine.query_engine.extractors.base import BaseExtractor


class OwnerExtractor(BaseExtractor):
    name = "owner"

    def extract(self, context) -> None:
        resources = context.resources.get("owner", {})
        if not isinstance(resources, dict):
            return

        for phrase in sorted(resources.keys(), key=len, reverse=True):
            value = resources.get(phrase)
            canonical = value if isinstance(value, str) else None
            if canonical is None:
                continue

            for match in self.find_phrase_matches(context.normalized_query, phrase):
                if not context.add_span(
                    slot="owner",
                    start=match.start(),
                    end=match.end(),
                    raw_text=match.group(0),
                    canonical=canonical,
                ):
                    continue

                strict = context.is_strict_match(match.start())
                if strict:
                    context.add_strict_slot("owner")

                context.owner = canonical
                context.filters.append(
                    FilterSpec(
                        field="owner",
                        operator="eq",
                        value=canonical,
                        scope="node",
                        source_text=match.group(0),
                        compileable=False,
                    )
                )
                context.target_scopes.append("node")
                return
