from __future__ import annotations

from search_engine.query_engine.contracts import FilterSpec
from search_engine.query_engine.extractors.base import BaseExtractor


class FileTypeExtractor(BaseExtractor):
    name = "file_type"

    def extract(self, context) -> None:
        resources = context.resources.get("file_types", {})
        if not isinstance(resources, dict):
            return

        for phrase in sorted(resources.keys(), key=len, reverse=True):
            exts = self._resolve_exts(resources, phrase)
            if not exts:
                continue

            for match in self.find_phrase_matches(context.normalized_query, phrase):
                canonical_name = self._resolve_canonical_name(resources, phrase)
                if not context.add_span(
                    slot="file_type",
                    start=match.start(),
                    end=match.end(),
                    raw_text=match.group(0),
                    canonical=canonical_name,
                ):
                    continue

                operator = "eq" if len(exts) == 1 else "in"
                value = exts[0] if len(exts) == 1 else exts
                strict = context.is_strict_match(match.start())
                if strict:
                    context.add_strict_slot("file_type")

                if context.file_type is None:
                    context.file_type = canonical_name
                if not context.extension:
                    context.extension = exts

                context.filters.append(
                    FilterSpec(
                        field="ext",
                        operator=operator,
                        value=value,
                        scope="node",
                        source_text=match.group(0),
                    )
                )
                context.target_scopes.append("node")
                return

    def _resolve_exts(self, resources: dict, phrase: str) -> list[str]:
        raw = resources.get(phrase)
        if isinstance(raw, str):
            canonical = resources.get(raw, raw)
            if isinstance(canonical, dict):
                exts = canonical.get("exts", [])
                return [ext.lower() for ext in exts if isinstance(ext, str)]
            if isinstance(canonical, str):
                return [canonical.lower()]

        if isinstance(raw, dict):
            exts = raw.get("exts", [])
            return [ext.lower() for ext in exts if isinstance(ext, str)]

        return []

    def _resolve_canonical_name(self, resources: dict, phrase: str) -> str:
        raw = resources.get(phrase)
        if isinstance(raw, str):
            return raw
        return phrase
