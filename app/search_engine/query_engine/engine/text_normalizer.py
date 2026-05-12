from __future__ import annotations

import re


class TextNormalizer:
    """
    Normalize query text conservatively before resource-driven extraction.

    This stage avoids aggressive rewriting so extractors can still rely on
    original phrases defined in locale resources.
    """

    def normalize(self, query: str, resources: dict | None = None) -> str:
        text = query.strip().lower()
        text = text.replace("\u3000", " ")
        text = text.replace("“", '"').replace("”", '"')
        text = text.replace("‘", "'").replace("’", "'")
        text = re.sub(r"\s*([><=!]=?|:)\s*", r"\1", text)
        text = re.sub(r"\s+", " ", text)
        return text
