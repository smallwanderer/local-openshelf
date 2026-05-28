from __future__ import annotations

from search_engine.query_engine.contracts import DSLFilter, DSLSort, QueryDSL


class QueryDSLAdapter:
    def from_search_plan(self, plan) -> QueryDSL:
        return QueryDSL(
            semantic_query=plan.semantic_query or plan.residual_query or plan.normalized_query,
            filters=[
                DSLFilter(
                    scope=spec.scope,
                    field=spec.field,
                    operator=spec.operator,
                    value=spec.value,
                    source_text=spec.source_text,
                    confidence=spec.confidence,
                )
                for spec in plan.structured_filters
                if getattr(spec, "compileable", True)
            ],
            sorts=[
                DSLSort(
                    scope=spec.scope,
                    field=spec.field,
                    direction=spec.direction,
                    source_text=spec.source_text,
                )
                for spec in plan.sort_specs
            ],
            target_scopes=list(plan.target_scopes),
        )

