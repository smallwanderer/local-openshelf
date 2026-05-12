from __future__ import annotations

import re
from abc import ABC, abstractmethod

from search_engine.query_engine.engine.context import AnalysisContext


class BaseExtractor(ABC):
    name: str = "base"

    @abstractmethod
    def extract(self, context: AnalysisContext) -> None:
        raise NotImplementedError

    def find_phrase_matches(self, text: str, phrase: str):
        pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")
        return pattern.finditer(text)
