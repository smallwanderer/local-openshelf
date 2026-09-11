"""Small input identity snapshots; no document bodies or vectors."""
from document_ai.models import DocumentChunk
from document_ai.services.embedding_runtime_config import load_embedding_runtime
from contextvars import ContextVar
import time

active_execution = ContextVar("active_executor_execution", default=None)


class StaleExecution(ValueError):
    """The executor must stop without overwriting newer domain state."""


def validate_execution():
    context = active_execution.get()
    if context is None:
        return
    envelope, expected_source, expected_runtime = context
    if time.time() >= float(envelope.get("deadline_at", time.time() + 1800)):
        raise StaleExecution("Executor deadline exceeded before persistence")
    source, runtime = capture_snapshot(envelope)
    if source != expected_source or runtime != expected_runtime:
        raise StaleExecution("Executor input or runtime changed before persistence")


def capture_snapshot(envelope):
    from files.models import FileBlob

    payload = envelope["payload"]
    node_ids = [payload["node_id"]] if "node_id" in payload else []
    if "chunk_ids" in payload:
        node_ids = list(DocumentChunk.objects.filter(pk__in=payload["chunk_ids"])
                        .order_by().values_list("parse_result__node_id", flat=True).distinct())
    blobs = list(FileBlob.objects.filter(node_id__in=node_ids).order_by("node_id")
                 .values("node_id", "uuid", "file", "size"))
    for blob in blobs:
        blob["uuid"] = str(blob["uuid"])
    return ({"payload": payload, "node_ids": sorted(node_ids), "blobs": blobs},
            {"fingerprint": load_embedding_runtime().runtime_fingerprint})
