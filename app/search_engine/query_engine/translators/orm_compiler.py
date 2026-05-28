from __future__ import annotations

from search_engine.query_engine.schema import QUERY_DSL_SCHEMA


class ORMCompiler:
    SCOPE_PREFIXES = {
        "node": "",
        "fileblob": "blob__",
        "parse_result": "parse_result__",
        "chunk": "parse_result__chunks__",
        "embedding": "parse_result__chunks__embeddings__",
        "user": "owner__",
        "user_storage": "owner__storage__",
    }

    def compile(self, context) -> tuple[dict, dict]:
        filter_kwargs = {}
        exclude_kwargs = {}

        for spec in context.filters:
            if not spec.compileable:
                continue
            prefix = self.SCOPE_PREFIXES.get(spec.scope, "")
            lookup = f"{prefix}{spec.field}"
            if spec.operator not in {"eq", "neq"}:
                lookup = f"{lookup}__{spec.operator}"

            if spec.operator == "neq":
                exclude_kwargs[lookup] = spec.value
            else:
                filter_kwargs[lookup] = spec.value

        return filter_kwargs, exclude_kwargs

    def compile_dsl(self, dsl, schema: dict | None = None) -> tuple[dict, dict, list[str]]:
        schema = schema or QUERY_DSL_SCHEMA
        filter_kwargs = {}
        exclude_kwargs = {}
        order_by = []

        for spec in dsl.filters:
            prefix = schema.get(spec.scope, {}).get("prefix", "")
            lookup = f"{prefix}{spec.field}"
            if spec.operator not in {"eq", "neq"}:
                lookup = f"{lookup}__{spec.operator}"

            if spec.operator == "neq":
                exclude_kwargs[lookup] = spec.value
            else:
                filter_kwargs[lookup] = spec.value

        for spec in dsl.sorts:
            prefix = schema.get(spec.scope, {}).get("prefix", "")
            lookup = f"{prefix}{spec.field}"
            order_by.append(f"-{lookup}" if spec.direction == "desc" else lookup)

        return filter_kwargs, exclude_kwargs, order_by
