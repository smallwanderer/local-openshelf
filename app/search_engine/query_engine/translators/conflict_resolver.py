from __future__ import annotations


class ConflictResolver:
    def resolve(self, context) -> None:
        deduped_filters = []
        seen_filters = set()
        for spec in context.filters:
            key = (spec.scope, spec.field, spec.operator, repr(spec.value))
            if key in seen_filters:
                continue
            seen_filters.add(key)
            deduped_filters.append(spec)
        context.filters = deduped_filters
        context.sort_specs = list(
            {
                (spec.scope, spec.field, spec.direction): spec
                for spec in context.sort_specs
            }.values()
        )

        context.target_scopes = list(dict.fromkeys(context.target_scopes))
        context.consumed_terms = list(dict.fromkeys(context.consumed_terms))
        context.strict_slots = list(dict.fromkeys(context.strict_slots))
