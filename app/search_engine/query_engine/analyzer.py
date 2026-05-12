from __future__ import annotations

from pathlib import Path

from search_engine.query_engine.engine.locale_selector import LocaleSelector
from search_engine.query_engine.engine.pipeline import QueryAnalysisPipeline
from search_engine.query_engine.engine.resource_loader import ResourceLoader
from search_engine.query_engine.engine.text_normalizer import TextNormalizer
from search_engine.query_engine.extractors import (
    ContentKeywordExtractor,
    FieldFilterExtractor,
    FilenameKeywordExtractor,
    FileTypeExtractor,
    IntentExtractor,
    OwnerExtractor,
    ScopeExtractor,
    SortExtractor,
    StatusExtractor,
    StrictnessExtractor,
    TimeExtractor,
)
from search_engine.query_engine.translators import ConflictResolver, ORMCompiler, QuerySplitter


def build_default_pipeline(*, resources_base_dir: str | Path | None = None, today_factory=None):
    base_dir = Path(resources_base_dir or Path(__file__).parent / "resources")
    resource_loader = ResourceLoader(base_dir)

    return QueryAnalysisPipeline(
        # 언어 탐지
        locale_selector=LocaleSelector(resources_base_dir=base_dir),
        # 사전 로드
        resource_loader=resource_loader,
        # 사전 정규화
        text_normalizer=TextNormalizer(),
        extractors=[
            IntentExtractor(),
            StrictnessExtractor(),
            TimeExtractor(today_factory=today_factory),
            StatusExtractor(),
            FileTypeExtractor(),
            OwnerExtractor(),
            SortExtractor(),
            FieldFilterExtractor(),
            ScopeExtractor(),
            FilenameKeywordExtractor(),
            ContentKeywordExtractor(),
        ],
        conflict_resolver=ConflictResolver(),
        orm_compiler=ORMCompiler(),
        query_splitter=QuerySplitter(),
    )


class QueryAnalyzer:
    def __init__(self, pipeline=None, *, resources_base_dir: str | Path | None = None, today_factory=None):
        self.pipeline = pipeline or build_default_pipeline(
            resources_base_dir=resources_base_dir,
            today_factory=today_factory,
        )

    def analyze(self, query: str):
        return self.pipeline.process(query)
