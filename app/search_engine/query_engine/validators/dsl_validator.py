from __future__ import annotations

from datetime import date, datetime, time
from uuid import UUID

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from search_engine.query_engine.contracts import (
    DSLFilter,
    DSLSort,
    QueryDSL,
    ValidatedQueryDSL,
    ValidationIssue,
)
from search_engine.query_engine.schema import QUERY_DSL_SCHEMA


class QueryDSLValidator:
    def __init__(self, schema: dict | None = None, *, strict: bool = False):
        self.schema = schema or QUERY_DSL_SCHEMA
        self.strict = strict

    def validate(self, dsl: QueryDSL) -> ValidatedQueryDSL:
        issues: list[ValidationIssue] = []
        filters = []
        sorts = []

        for index, item in enumerate(dsl.filters):
            filter_spec = self._validate_filter(item, path=f"filters[{index}]", issues=issues)
            if filter_spec is not None:
                filters.append(filter_spec)

        for index, item in enumerate(dsl.sorts):
            sort_spec = self._validate_sort(item, path=f"sorts[{index}]", issues=issues)
            if sort_spec is not None:
                sorts.append(sort_spec)

        target_scopes = []
        for scope in dsl.target_scopes:
            if scope in self.schema and scope not in target_scopes:
                target_scopes.append(scope)
            elif scope not in self.schema:
                issues.append(
                    ValidationIssue(
                        code="unknown_scope",
                        message=f"Scope '{scope}' is not allowed.",
                        path="target_scopes",
                        level="warning",
                    )
                )

        validated = QueryDSL(
            semantic_query=str(dsl.semantic_query or "").strip(),
            filters=filters,
            sorts=sorts,
            target_scopes=target_scopes,
        )
        has_error = any(issue.level == "error" for issue in issues)
        return ValidatedQueryDSL(dsl=validated, valid=not has_error, issues=issues)

    def _validate_filter(
        self,
        item: DSLFilter,
        *,
        path: str,
        issues: list[ValidationIssue],
    ) -> DSLFilter | None:
        field_spec = self._field_spec(item.scope, item.field, path=path, issues=issues)
        if field_spec is None:
            return None

        allowed_operators = field_spec.get("operators", set())
        if item.operator not in allowed_operators:
            issues.append(
                ValidationIssue(
                    code="operator_not_allowed",
                    message=f"Operator '{item.operator}' is not allowed for {item.scope}.{item.field}.",
                    path=f"{path}.operator",
                    level=self._issue_level(),
                )
            )
            return None

        value = self._coerce_value(
            item.value,
            field_type=field_spec.get("type", "str"),
            operator=item.operator,
            path=f"{path}.value",
            issues=issues,
        )
        if value is None and item.value is not None:
            return None
        if not self._validate_choices(
            value,
            choices=field_spec.get("choices"),
            operator=item.operator,
            path=f"{path}.value",
            issues=issues,
        ):
            return None

        return DSLFilter(
            scope=item.scope,
            field=item.field,
            operator=item.operator,
            value=value,
            source_text=item.source_text,
            confidence=item.confidence,
        )

    def _validate_sort(self, item: DSLSort, *, path: str, issues: list[ValidationIssue]) -> DSLSort | None:
        scope_spec = self.schema.get(item.scope)
        if not scope_spec:
            issues.append(
                ValidationIssue(
                    code="unknown_scope",
                    message=f"Scope '{item.scope}' is not allowed.",
                    path=f"{path}.scope",
                    level=self._issue_level(),
                )
            )
            return None

        sortable_fields = scope_spec.get("sortable_fields", set())
        if item.field not in sortable_fields:
            issues.append(
                ValidationIssue(
                    code="sort_field_not_allowed",
                    message=f"Sort field '{item.scope}.{item.field}' is not allowed.",
                    path=f"{path}.field",
                    level=self._issue_level(),
                )
            )
            return None

        if item.direction not in {"asc", "desc"}:
            issues.append(
                ValidationIssue(
                    code="sort_direction_not_allowed",
                    message=f"Sort direction '{item.direction}' is not allowed.",
                    path=f"{path}.direction",
                    level=self._issue_level(),
                )
            )
            return None

        return item

    def _field_spec(
        self,
        scope: str,
        field: str,
        *,
        path: str,
        issues: list[ValidationIssue],
    ) -> dict | None:
        scope_spec = self.schema.get(scope)
        if not scope_spec:
            issues.append(
                ValidationIssue(
                    code="unknown_scope",
                    message=f"Scope '{scope}' is not allowed.",
                    path=f"{path}.scope",
                    level=self._issue_level(),
                )
            )
            return None

        fields = scope_spec.get("fields", {})
        field_spec = fields.get(field)
        if not field_spec:
            issues.append(
                ValidationIssue(
                    code="unknown_field",
                    message=f"Field '{scope}.{field}' is not allowed.",
                    path=f"{path}.field",
                    level=self._issue_level(),
                )
            )
            return None

        return field_spec

    def _coerce_value(
        self,
        value,
        *,
        field_type: str,
        operator: str,
        path: str,
        issues: list[ValidationIssue],
    ):
        if operator == "in":
            if not isinstance(value, list):
                issues.append(
                    ValidationIssue(
                        code="invalid_value_type",
                        message="'in' operator requires a list value.",
                        path=path,
                        level=self._issue_level(),
                    )
                )
                return None
            coerced = [
                self._coerce_single_value(item, field_type=field_type, path=path, issues=issues)
                for item in value
            ]
            return [item for item in coerced if item is not None]

        return self._coerce_single_value(value, field_type=field_type, path=path, issues=issues)

    def _coerce_single_value(self, value, *, field_type: str, path: str, issues: list[ValidationIssue]):
        if value is None:
            return None
        if field_type == "json":
            return value
        if field_type == "str":
            return str(value)
        if field_type == "int":
            try:
                return int(value)
            except (TypeError, ValueError):
                return self._invalid_value(value, path, issues, "integer")
        if field_type == "bool":
            return self._coerce_bool(value, path, issues)
        if field_type == "uuid":
            try:
                return str(UUID(str(value)))
            except (TypeError, ValueError):
                return self._invalid_value(value, path, issues, "uuid")
        if field_type == "datetime":
            return self._coerce_datetime(value, path, issues)
        return value

    def _validate_choices(
        self,
        value,
        *,
        choices,
        operator: str,
        path: str,
        issues: list[ValidationIssue],
    ) -> bool:
        if not choices:
            return True
        values = value if operator == "in" and isinstance(value, list) else [value]
        invalid_values = [item for item in values if item not in choices]
        if not invalid_values:
            return True
        issues.append(
            ValidationIssue(
                code="value_not_allowed",
                message=f"Value {invalid_values[0]!r} is not allowed for this field.",
                path=path,
                level=self._issue_level(),
            )
        )
        return False

    def _coerce_bool(self, value, path: str, issues: list[ValidationIssue]):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y", "on", "참", "예"}:
                return True
            if normalized in {"false", "0", "no", "n", "off", "거짓", "아니오"}:
                return False
        return self._invalid_value(value, path, issues, "boolean")

    def _coerce_datetime(self, value, path: str, issues: list[ValidationIssue]):
        if isinstance(value, datetime):
            candidate = value
        elif isinstance(value, date):
            candidate = datetime.combine(value, time.min)
        elif isinstance(value, str):
            candidate = parse_datetime(value)
            if candidate is None:
                parsed_date = parse_date(value)
                if parsed_date is not None:
                    candidate = datetime.combine(parsed_date, time.min)
        else:
            candidate = None

        if candidate is None:
            return self._invalid_value(value, path, issues, "datetime")
        if timezone.is_naive(candidate):
            candidate = timezone.make_aware(candidate, timezone.get_current_timezone())
        return candidate

    def _invalid_value(self, value, path: str, issues: list[ValidationIssue], expected: str):
        issues.append(
            ValidationIssue(
                code="invalid_value_type",
                message=f"Value {value!r} must be {expected}.",
                path=path,
                level=self._issue_level(),
            )
        )
        return None

    def _issue_level(self) -> str:
        return "error" if self.strict else "warning"
