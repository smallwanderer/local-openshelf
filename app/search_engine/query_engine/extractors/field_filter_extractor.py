from __future__ import annotations

import re

from search_engine.query_engine.contracts import FilterSpec
from search_engine.query_engine.extractors.base import BaseExtractor


class FieldFilterExtractor(BaseExtractor):
    name = "field_filter"

    SYMBOL_OPERATORS = {
        ">=": "gte",
        "<=": "lte",
        "!=": "neq",
        ":": "eq",
        "=": "eq",
        ">": "gt",
        "<": "lt",
    }

    def extract(self, context) -> None:
        field_aliases = context.resources.get("field_alias", {})
        model_scope = context.resources.get("model_scope", {})
        if not isinstance(field_aliases, dict):
            return

        for alias in sorted(field_aliases.keys(), key=len, reverse=True):
            canonical_field = field_aliases.get(alias)
            if not isinstance(alias, str) or not isinstance(canonical_field, str):
                continue

            pattern = re.compile(
                rf"(?P<expr>\b{re.escape(alias)}\b\s*(?P<op>>=|<=|!=|:|=|>|<)\s*(?P<value>[^\s]+))"
            )
            for match in pattern.finditer(context.normalized_query):
                if not context.add_span(
                    slot="field_filter",
                    start=match.start("expr"),
                    end=match.end("expr"),
                    raw_text=match.group("expr"),
                    canonical=canonical_field,
                ):
                    continue

                operator = self.SYMBOL_OPERATORS[match.group("op")]
                raw_value = match.group("value")
                parsed_value = self._parse_value(raw_value)

                context.filters.append(
                    FilterSpec(
                        field=canonical_field,
                        operator=operator,
                        value=parsed_value,
                        scope=self._infer_scope(model_scope, canonical_field),
                        source_text=match.group("expr"),
                    )
                )
                context.target_scopes.append(self._infer_scope(model_scope, canonical_field))

    def _parse_value(self, raw_value: str):
        if raw_value.isdigit():
            return int(raw_value)
        try:
            return float(raw_value)
        except ValueError:
            return raw_value.strip("\"'")

    def _infer_scope(self, model_scope: dict, field: str) -> str:
        if not isinstance(model_scope, dict):
            return "node"

        for scope, spec in model_scope.items():
            fields = spec.get("fields", []) if isinstance(spec, dict) else []
            if field in fields:
                return scope
        return "node"
