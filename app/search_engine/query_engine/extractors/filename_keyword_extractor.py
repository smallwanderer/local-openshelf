from __future__ import annotations

import re

from search_engine.query_engine.extractors.base import BaseExtractor


class FilenameKeywordExtractor(BaseExtractor):
    name = "filename_keyword"

    QUOTED_PATTERN = re.compile(r"(?P<quote>[\"'])(?P<value>.+?)(?P=quote)")
    HINT_PATTERN = re.compile(
        r"\b(?:named|called|titled|filename)\s+(?P<value>[a-z0-9][a-z0-9._ -]{0,63})"
    )

    def extract(self, context) -> None:
        for match in self.QUOTED_PATTERN.finditer(context.normalized_query):
            keyword = match.group("value").strip()
            if not keyword:
                continue
            if not context.add_span(
                slot="filename_keyword",
                start=match.start(),
                end=match.end(),
                raw_text=match.group(0),
                canonical=keyword,
            ):
                continue
            if keyword not in context.filename_keywords:
                context.filename_keywords.append(keyword)

        for match in self.HINT_PATTERN.finditer(context.normalized_query):
            raw_value = match.group("value").strip()
            keyword = re.sub(r"\s+", " ", raw_value).strip(" .")
            if not keyword:
                continue
            if not context.add_span(
                slot="filename_keyword",
                start=match.start(),
                end=match.end(),
                raw_text=match.group(0),
                canonical=keyword,
            ):
                continue
            if keyword not in context.filename_keywords:
                context.filename_keywords.append(keyword)
