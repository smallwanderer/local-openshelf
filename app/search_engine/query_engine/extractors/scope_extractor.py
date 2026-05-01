from __future__ import annotations

from search_engine.query_engine.extractors.base import BaseExtractor


class ScopeExtractor(BaseExtractor):
    name = "scope"

    def extract(self, context) -> None:
        for spec in context.filters:
            context.target_scopes.append(spec.scope)
