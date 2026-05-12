from __future__ import annotations

from search_engine.query_engine.extractors.base import BaseExtractor


class StrictnessExtractor(BaseExtractor):
    name = "strictness"

    DEFAULT_MARKERS = ("only", "just")

    def extract(self, context) -> None:
        resources = context.resources.get("strict_markers", self.DEFAULT_MARKERS)
        if isinstance(resources, dict):
            markers = tuple(str(key) for key in resources.keys())
        elif isinstance(resources, list):
            markers = tuple(str(item) for item in resources)
        else:
            markers = self.DEFAULT_MARKERS

        for marker in sorted(markers, key=len, reverse=True):
            for match in self.find_phrase_matches(context.normalized_query, marker):
                context.add_strict_marker(
                    start=match.start(),
                    end=match.end(),
                    raw_text=match.group(0),
                )
