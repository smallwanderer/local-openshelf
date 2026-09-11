from __future__ import annotations

import re

from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from accounts.api_responses import api_error_response
from accounts.decorators import operator_api_required
from document_ai.services.operation_metrics import (
    WINDOWS,
    build_events_payload,
    build_metrics_payload,
    build_trace_payload,
)
from document_ai.services.operation_status import build_operation_status
from document_ai.services.resource_snapshot import (
    collect_resource_snapshots,
    latest_resource_snapshots,
    serialize_resource_snapshot,
)


_TRACE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _window(request):
    value = request.GET.get("window", "24h")
    if value not in WINDOWS:
        return None, api_error_response(
            "INVALID_REQUEST",
            "window must be one of: 1h, 24h, 7d.",
            status=400,
            details={"window": sorted(WINDOWS)},
        )
    return value, None


@require_GET
@operator_api_required
def operation_status(request):
    return JsonResponse({"ok": True, **build_operation_status()})


@require_GET
@operator_api_required
def operation_metrics(request):
    window, error = _window(request)
    if error is not None:
        return error
    return JsonResponse({"ok": True, **build_metrics_payload(window)})


@require_GET
@operator_api_required
def operation_events(request):
    window, error = _window(request)
    if error is not None:
        return error
    try:
        limit = int(request.GET.get("limit", "10"))
    except (TypeError, ValueError):
        limit = 10
    limit = min(max(limit, 1), 20)
    return JsonResponse({"ok": True, **build_events_payload(window, limit=limit)})


@require_GET
@operator_api_required
def operation_trace(request, trace_id):
    if not _TRACE_PATTERN.fullmatch(trace_id):
        return api_error_response(
            "INVALID_REQUEST", "Invalid trace id.", status=400
        )
    payload = build_trace_payload(trace_id)
    if payload is None:
        return api_error_response("NOT_FOUND", "Trace was not found.", status=404)
    return JsonResponse({"ok": True, **payload})


@require_GET
@operator_api_required
def operation_resources(request):
    rows = [serialize_resource_snapshot(row) for row in latest_resource_snapshots()]
    return JsonResponse({"ok": True, "snapshots": rows})


@require_POST
@operator_api_required
def collect_operation_resources(request):
    result = collect_resource_snapshots()
    return JsonResponse(
        {
            "ok": True,
            "snapshots": [serialize_resource_snapshot(row) for row in result.rows],
            "skipped": result.skipped,
        }
    )
