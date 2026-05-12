from __future__ import annotations

from search_engine.query_engine.contracts import MatchedToken, SearchPlan
from search_engine.query_engine.engine.context import AnalysisContext


class QueryAnalysisPipeline:
    def __init__(
        self,
        locale_selector,
        resource_loader,
        text_normalizer,
        extractors,
        conflict_resolver,
        orm_compiler,
        query_splitter,
    ):
        self.locale_selector = locale_selector
        self.resource_loader = resource_loader
        self.text_normalizer = text_normalizer
        self.extractors = extractors
        self.conflict_resolver = conflict_resolver
        self.orm_compiler = orm_compiler
        self.query_splitter = query_splitter

    def process(self, query: str) -> SearchPlan:
        context = AnalysisContext(raw_query=query)

        locale_result = self.locale_selector.select(query)
        context.primary_locale = locale_result.primary_locale
        context.active_locales = locale_result.active_locales
        context.debug["locale_matches"] = [
            {
                "locale": match.locale,
                "score": match.score,
                "matched_terms": match.matched_terms,
            }
            for match in locale_result.matches
        ]

        bundle = self.resource_loader.load_bundle(
            primary_locale=context.primary_locale,
            active_locales=[loc for loc in context.active_locales if loc != "common"],
        )
        context.resources = bundle.merged
        context.normalized_query = self.text_normalizer.normalize(
            query,
            resources=context.resources,
        )

        for extractor in self.extractors:
            extractor.extract(context)

        self.conflict_resolver.resolve(context)
        orm_filter_kwargs, orm_exclude_kwargs = self.orm_compiler.compile(context)
        residual_query, dense_query, sparse_query = self.query_splitter.split(context)

        return SearchPlan(
            raw_query=context.raw_query,
            normalized_query=context.normalized_query,
            primary_locale=context.primary_locale,
            active_locales=context.active_locales,
            intent=context.intent,
            target_scopes=context.target_scopes,
            semantic_query=context.semantic_query,
            filename_keywords=context.filename_keywords,
            date=context.date,
            file_type=context.file_type,
            extension=context.extension,
            owner=context.owner,
            structured_filters=context.filters,
            sort_specs=context.sort_specs,
            strict_slots=context.strict_slots,
            matched_tokens=[
                MatchedToken(
                    slot=span["slot"],
                    raw_text=span["raw_text"],
                    canonical=span.get("canonical"),
                    start=span["start"],
                    end=span["end"],
                    confidence=span.get("confidence", 1.0),
                )
                for span in sorted(context.matched_spans, key=lambda item: (item["start"], item["end"]))
            ],
            orm_filter_kwargs=orm_filter_kwargs,
            orm_exclude_kwargs=orm_exclude_kwargs,
            residual_query=residual_query,
            dense_query=dense_query,
            sparse_query=sparse_query,
            warnings=context.warnings,
            debug=context.debug,
        )
