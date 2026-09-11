from __future__ import annotations

from django.utils import timezone

from config.tracing import get_trace_id, new_trace_id


def enqueue_kwargs(trace_id: str | None = None) -> dict:
    """Build the trace_id/enqueued_at kwargs a Celery task needs to report
    queue_wait_ms and continue a trace across the parse/embed pipeline.
    Pass an explicit trace_id to continue an existing trace (e.g. a parse task
    forwarding its own trace_id to the embedding tasks it queues); otherwise the
    current request/task's trace_id is reused, or a new one is minted."""
    return {
        "trace_id": trace_id or get_trace_id() or new_trace_id(),
        "enqueued_at": timezone.now().isoformat(),
    }


def put_task_identity(
    metrics: dict,
    *,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
) -> None:
    """Persist queue identity without overwriting legacy/direct-call metrics."""
    if job_id:
        metrics["job_id"] = job_id
    if parent_job_id:
        metrics["parent_job_id"] = parent_job_id
    if work_kind:
        metrics["work_kind"] = work_kind
    if envelope_version is not None:
        metrics["task_envelope_version"] = envelope_version
