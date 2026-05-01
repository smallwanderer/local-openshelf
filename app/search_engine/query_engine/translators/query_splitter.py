from __future__ import annotations


class QuerySplitter:
    def split(self, context) -> tuple[str, str, str]:
        residual = context.semantic_query.strip()
        return residual, residual, residual
