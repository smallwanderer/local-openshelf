from __future__ import annotations


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
