from __future__ import annotations

import re

from search_engine.query_engine.extractors.base import BaseExtractor


class ContentKeywordExtractor(BaseExtractor):
    name = "content_keyword"

    DEFAULT_STOPWORDS = {
        "a",
        "an",
        "about",
        "by",
        "file",
        "files",
        "find",
        "for",
        "from",
        "me",
        "of",
        "please",
        "search",
        "show",
        "the",
        "to",
        "with",
    }

    def extract(self, context) -> None:
        residual = self._remove_matched_spans(context.normalized_query, context.matched_spans)
        stopwords = self._load_stopwords(context)

        keywords = []
        for token in re.split(r"\s+", residual):
            cleaned = token.strip(" ,.:;!?()[]{}\"'")
            if not cleaned:
                continue
            if cleaned in stopwords:
                continue
            keywords.append(cleaned)

        context.content_keywords = keywords
        context.semantic_query = " ".join(keywords).strip()
        if context.semantic_query:
            context.target_scopes.extend(["node", "chunk"])

    def _remove_matched_spans(self, text: str, spans: list[dict]) -> str:
        if not spans:
            return text

        chars = list(text)
        for span in spans:
            for idx in range(span["start"], span["end"]):
                chars[idx] = " "
        return re.sub(r"\s+", " ", "".join(chars)).strip()

    def _load_stopwords(self, context) -> set[str]:
        resources = context.resources.get("stopwords", [])
        stopwords = set(self.DEFAULT_STOPWORDS)
        if isinstance(resources, list):
            stopwords.update(str(item) for item in resources)
        return stopwords
