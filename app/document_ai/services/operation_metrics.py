from __future__ import annotations

import re
from datetime import timedelta
from numbers import Real

from django.utils import timezone

from config.enums import AIStatus
from document_ai.models import DocumentChunk, DocumentParseResult, RAGJob, SearchJob
from files.services.operation_metrics import (
    file_operation_rows,
    file_operations_for_trace,
    upload_operation_rows,
)


WINDOWS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}

PIPELINE_METRICS = {
    "upload": ["total_ms"],
    "parse": ["queue_wait_ms", "parse_processing_ms"],
    "embedding": ["queue_wait_ms", "embedding_processing_ms"],
    "search": [
        "queue_wait_ms",
        "embedding_presence_check_ms",
        "query_embedding_ms",
        "vector_query_ms",
        "rerank_and_evidence_ms",
        "contextual_compression_ms",
        "retrieval_total_ms",
        "worker_total_ms",
        "end_to_end_ms",
        "result_count",
    ],
    "rag": [
        "queue_wait_ms",
        "generation_queue_wait_ms",
        "context_build_ms",
        "llm_connect_ms",
        "llm_ttft_ms",
        "llm_generation_after_first_token_ms",
        "llm_total_ms",
        "worker_total_ms",
        "end_to_end_ms",
        "input_token_count",
        "output_token_count",
        "output_tokens_per_second",
        "citation_count",
        "context_chars",
        "output_chars",
    ],
}

PRIMARY_DURATION = {
    "upload": "total_ms",
    "parse": "parse_processing_ms",
    "embedding": "embedding_processing_ms",
    "search": "end_to_end_ms",
    "rag": "end_to_end_ms",
}

_SECRET_PATTERN = re.compile(
    r"(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+"
)
_TOTAL_METRICS = {
    "total_ms",
    "end_to_end_ms",
    "worker_total_ms",
    "retrieval_total_ms",
    "llm_total_ms",
    "request_search_ms",
}


def window_bounds(window_key: str, *, now=None) -> tuple:
    if window_key not in WINDOWS:
        raise ValueError("Unsupported metrics window.")
    end = now or timezone.now()
    return end - WINDOWS[window_key], end


def _numeric(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    return float(value)


def _metric_stat(rows: list[dict], key: str) -> dict:
    values = []
    for row in rows:
        value = _numeric((row.get("performance_metrics") or {}).get(key))
        if value is not None:
            values.append(value)
    unit = "ms" if key.endswith("_ms") else "count"
    if key == "output_tokens_per_second":
        unit = "tokens_per_second"
    return {
        "average": round(sum(values) / len(values), 3) if values else None,
        "maximum": round(max(values), 3) if values else None,
        "measured_count": len(values),
        "total_count": len(rows),
        "unit": unit,
    }


def _status_summary(rows: list[dict], duration_metric: str) -> dict:
    statuses = [str(row.get("status") or "") for row in rows]
    terminal = [status for status in statuses if status in {AIStatus.COMPLETED, AIStatus.FAILED, AIStatus.CANCELED}]
    success_count = statuses.count(AIStatus.COMPLETED)
    timeout_count = sum(
        1 for row in rows if (row.get("performance_metrics") or {}).get("timeout") is True
    )
    return {
        "total_count": len(rows),
        "terminal_count": len(terminal),
        "success_count": success_count,
        "failure_count": statuses.count(AIStatus.FAILED),
        "canceled_count": statuses.count(AIStatus.CANCELED),
        "in_progress_count": statuses.count(AIStatus.PENDING) + statuses.count(AIStatus.PROCESSING),
        "timeout_count": timeout_count,
        "success_rate": round(success_count / len(terminal) * 100, 1) if terminal else None,
        "duration": _metric_stat(rows, duration_metric),
    }


def _parse_rows(since) -> list[dict]:
    return list(
        DocumentParseResult.objects.filter(created_at__gte=since).values(
            "id",
            "status",
            "errors",
            "performance_metrics",
            "created_at",
            "updated_at",
            "parsed_at",
            "node_id",
            "chunk_count",
        )
    )


def _embedding_rows(since) -> list[dict]:
    return list(
        DocumentChunk.objects.filter(created_at__gte=since).values(
            "id",
            "status",
            "error_message",
            "performance_metrics",
            "created_at",
            "updated_at",
            "parse_result_id",
            "chunk_index",
        )
    )


def _search_rows(since) -> list[dict]:
    return list(
        SearchJob.objects.filter(created_at__gte=since).values(
            "id",
            "status",
            "error_message",
            "performance_metrics",
            "created_at",
            "started_at",
            "completed_at",
        )
    )


def _rag_rows(since) -> list[dict]:
    return list(
        RAGJob.objects.filter(created_at__gte=since).values(
            "id",
            "search_job_id",
            "status",
            "stage",
            "error_message",
            "performance_metrics",
            "created_at",
            "started_at",
            "completed_at",
        )
    )


def _pipeline(name: str, rows: list[dict]) -> dict:
    return {
        "name": name,
        "count": len(rows),
        "primary_duration": PRIMARY_DURATION[name],
        "metrics": {
            key: _metric_stat(rows, key) for key in PIPELINE_METRICS[name]
        },
    }


def build_metrics_payload(window_key: str) -> dict:
    since, end = window_bounds(window_key)
    upload_rows = upload_operation_rows(since=since)
    parse_rows = _parse_rows(since)
    embedding_rows = _embedding_rows(since)
    search_rows = _search_rows(since)
    rag_rows = _rag_rows(since)
    return {
        "generated_at": end.isoformat(),
        "window": {
            "key": window_key,
            "from": since.isoformat(),
            "to": end.isoformat(),
            "timezone": timezone.get_current_timezone_name(),
        },
        "summary": {
            "upload": _status_summary(upload_rows, "total_ms"),
            "search": _status_summary(search_rows, "end_to_end_ms"),
            "rag": _status_summary(rag_rows, "end_to_end_ms"),
        },
        "pipelines": [
            _pipeline("upload", upload_rows),
            _pipeline("parse", parse_rows),
            _pipeline("embedding", embedding_rows),
            _pipeline("search", search_rows),
            _pipeline("rag", rag_rows),
        ],
    }


def _safe_error_summary(value) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("message") or value.get("error") or "Recorded processing error"
    text = " ".join(str(value).split())
    text = _SECRET_PATTERN.sub(r"\1=[redacted]", text)
    return text[:180]


def _safe_metrics(metrics: dict) -> dict:
    safe = {}
    for key, value in (metrics or {}).items():
        if key == "trace_id":
            safe[key] = str(value)[:128]
        elif isinstance(value, bool) or isinstance(value, Real):
            safe[key] = value
    return safe


def _event_duration(kind: str, metrics: dict):
    primary = PRIMARY_DURATION.get(kind)
    value = _numeric(metrics.get(primary)) if primary else None
    if kind in {"parse", "embedding"}:
        wait = _numeric(metrics.get("queue_wait_ms"))
        if value is not None and wait is not None:
            return round(value + wait, 3), "recorded_total_ms"
    return (round(value, 3), primary) if value is not None else (None, primary)


def _dominant_metric(metrics: dict, fallback: str | None) -> dict | None:
    candidates = []
    for key, raw_value in metrics.items():
        value = _numeric(raw_value)
        if value is not None and key.endswith("_ms") and key not in _TOTAL_METRICS:
            candidates.append((key, value))
    if not candidates and fallback:
        value = _numeric(metrics.get(fallback))
        if value is not None:
            candidates.append((fallback, value))
    if not candidates:
        return None
    key, value = max(candidates, key=lambda item: item[1])
    return {"key": key, "value": round(value, 3), "unit": "ms"}


def _event(kind: str, row: dict, *, operation: str = "") -> dict:
    metrics = row.get("performance_metrics") or {}
    duration, duration_metric = _event_duration(kind, metrics)
    error_value = row.get("error_message") or row.get("errors")
    timed_out = metrics.get("timeout") is True
    return {
        "key": f"{kind}:{row['id']}",
        "pipeline": kind,
        "operation": operation,
        "record_id": row["id"],
        "status": "timeout" if timed_out else row.get("status"),
        "created_at": row["created_at"].isoformat(),
        "duration_ms": duration,
        "duration_metric": duration_metric,
        "dominant_metric": _dominant_metric(metrics, duration_metric),
        "trace_id": str(metrics.get("trace_id") or ""),
        "error_summary": _safe_error_summary(error_value),
        "is_failure": row.get("status") == AIStatus.FAILED or timed_out,
    }


def build_events_payload(window_key: str, *, limit: int = 10) -> dict:
    since, end = window_bounds(window_key)
    events = []
    events.extend(
        _event("upload" if row["operation"] == "upload" else "file", row, operation=row["operation"])
        for row in file_operation_rows(since=since)
    )
    events.extend(_event("parse", row) for row in _parse_rows(since))
    events.extend(_event("embedding", row) for row in _embedding_rows(since))
    events.extend(_event("search", row) for row in _search_rows(since))
    events.extend(_event("rag", row) for row in _rag_rows(since))

    slow = sorted(
        (event for event in events if event["duration_ms"] is not None),
        key=lambda event: event["duration_ms"],
        reverse=True,
    )[:5]
    failures = sorted(
        (event for event in events if event["is_failure"]),
        key=lambda event: event["created_at"],
        reverse=True,
    )[:5]
    combined = []
    seen = set()
    for event in [*slow, *failures]:
        if event["key"] in seen:
            continue
        seen.add(event["key"])
        combined.append(event)
        if len(combined) >= limit:
            break
    return {
        "generated_at": end.isoformat(),
        "window": {
            "key": window_key,
            "from": since.isoformat(),
            "to": end.isoformat(),
            "timezone": timezone.get_current_timezone_name(),
        },
        "events": combined,
    }


def _trace_record(kind: str, row: dict, *, operation: str = "") -> dict:
    metrics = _safe_metrics(row.get("performance_metrics") or {})
    duration, duration_metric = _event_duration(kind, metrics)
    metadata = {}
    for key in ("chunk_count", "chunk_index", "search_job_id", "stage"):
        if row.get(key) is not None:
            metadata[key] = row[key]
    for key in ("result_count", "citation_count", "input_token_count", "output_token_count"):
        if key in metrics:
            metadata[key] = metrics[key]
    return {
        "pipeline": kind,
        "operation": operation,
        "record_id": row["id"],
        "status": "timeout" if metrics.get("timeout") is True else row.get("status"),
        "created_at": row["created_at"].isoformat(),
        "started_at": row.get("started_at").isoformat() if row.get("started_at") else None,
        "completed_at": row.get("completed_at").isoformat() if row.get("completed_at") else None,
        "duration_ms": duration,
        "duration_metric": duration_metric,
        "metrics": metrics,
        "metadata": metadata,
        "error_summary": _safe_error_summary(row.get("error_message") or row.get("errors")),
    }


def build_trace_payload(trace_id: str) -> dict | None:
    records = []
    records.extend(
        _trace_record("upload" if row["operation"] == "upload" else "file", row, operation=row["operation"])
        for row in file_operations_for_trace(trace_id)
    )
    parse_rows = DocumentParseResult.objects.filter(performance_metrics__trace_id=trace_id).values(
        "id", "status", "errors", "performance_metrics", "created_at", "updated_at", "parsed_at", "node_id", "chunk_count"
    )
    embedding_rows = DocumentChunk.objects.filter(performance_metrics__trace_id=trace_id).values(
        "id", "status", "error_message", "performance_metrics", "created_at", "updated_at", "parse_result_id", "chunk_index"
    )
    search_rows = SearchJob.objects.filter(performance_metrics__trace_id=trace_id).values(
        "id", "status", "error_message", "performance_metrics", "created_at", "started_at", "completed_at"
    )
    rag_rows = RAGJob.objects.filter(performance_metrics__trace_id=trace_id).values(
        "id", "search_job_id", "status", "stage", "error_message", "performance_metrics", "created_at", "started_at", "completed_at"
    )
    records.extend(_trace_record("parse", row) for row in parse_rows)
    records.extend(_trace_record("embedding", row) for row in embedding_rows)
    records.extend(_trace_record("search", row) for row in search_rows)
    records.extend(_trace_record("rag", row) for row in rag_rows)
    if not records:
        return None
    records.sort(key=lambda row: row["created_at"])
    return {
        "trace_id": trace_id,
        "records": records,
        "log_hint": f"rg -n --fixed-strings 'trace_id={trace_id}' data/logs/*.log",
    }
