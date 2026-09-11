from __future__ import annotations

from config.enums import FileOperation
from files.models import FileOperationLog


def file_operation_rows(*, since, operation: str | None = None) -> list[dict]:
    """Return the content-free projection used by server operations."""

    queryset = FileOperationLog.objects.filter(created_at__gte=since)
    if operation is not None:
        queryset = queryset.filter(operation=operation)
    return list(
        queryset.values(
            "id",
            "operation",
            "status",
            "error_message",
            "performance_metrics",
            "created_at",
            "node_id",
        )
    )


def upload_operation_rows(*, since) -> list[dict]:
    return file_operation_rows(since=since, operation=FileOperation.UPLOAD)


def file_operations_for_trace(trace_id: str) -> list[dict]:
    return list(
        FileOperationLog.objects.filter(performance_metrics__trace_id=trace_id).values(
            "id",
            "operation",
            "status",
            "error_message",
            "performance_metrics",
            "created_at",
            "node_id",
        )
    )
