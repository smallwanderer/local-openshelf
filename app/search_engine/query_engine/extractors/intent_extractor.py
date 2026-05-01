from __future__ import annotations

from search_engine.query_engine.extractors.base import BaseExtractor


class IntentExtractor(BaseExtractor):
    name = "intent"

    def extract(self, context) -> None:
        resources = context.resources.get("intents", {})
        if not isinstance(resources, dict):
            return

        for phrase in sorted(resources.keys(), key=len, reverse=True):
            value = resources.get(phrase)
            for match in self.find_phrase_matches(context.normalized_query, phrase):
                if not context.add_span(
                    slot="intent",
                    start=match.start(),
                    end=match.end(),
                    raw_text=match.group(0),
                    canonical=phrase,
                ):
                    continue

                if isinstance(value, str):
                    context.intent = value
                elif isinstance(value, dict):
                    context.intent = value.get("value")
                return
