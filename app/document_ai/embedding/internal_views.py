from __future__ import annotations

import json
import logging
import os
import threading

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .admission import AdmissionRejected, EmbeddingPriorityAdmission
from .providers.base import EmbeddingBusyError
from . import embeding_models
from .registry import get_embedding_provider
from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

logger = logging.getLogger(__name__)

_ALLOWED_INPUT_TYPES = {"query", "document"}
_MAX_TEXT_CHARS = 20000
# Server-side backstop matching EMBEDDING_DOCUMENT_BATCH_MAX_CHUNKS -- the real
# cap is enforced by the caller (enqueue_embedding_tasks_sync's batch grouping);
# this just rejects a malformed/oversized request outright.
_MAX_BATCH_SIZE = 16


def _get_positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default
    if value < 1:
        logger.warning("%s must be >= 1, falling back to %s", name, default)
        return default
    return value


def _get_nonnegative_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default
    if value < 0:
        logger.warning("%s must be >= 0, falling back to %s", name, default)
        return default
    return value


def _get_nonnegative_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default
    if value < 0:
        logger.warning("%s must be >= 0, falling back to %s", name, default)
        return default
    return value


# Bounds concurrent model.encode() calls across gunicorn's threads and orders
# waiting work at the single model admission point: query before document,
# FIFO within the same request type.
# Concurrent BGE-M3 encode() calls were stress-tested (dense+sparse output
# verified against sequential ground truth across many concurrent rounds --
# see `manage.py check_embedding_concurrency`) with no race conditions found,
# so this is no longer forced to 1. Gunicorn keeps additional threads for
# queued requests and healthchecks; re-run check_embedding_concurrency on the
# target hardware before raising the model concurrency itself.
_EMBED_CONCURRENCY = _get_positive_int_env("EMBEDDING_MODEL_CONCURRENCY", 2)
_EMBED_QUEUE_CAPACITY = _get_nonnegative_int_env("EMBEDDING_REQUEST_QUEUE_CAPACITY", 4)
_EMBED_QUERY_RESERVE = min(
    _get_nonnegative_int_env("EMBEDDING_QUERY_QUEUE_RESERVE", 1),
    _EMBED_QUEUE_CAPACITY,
)
_EMBED_QUEUE_TIMEOUT = _get_nonnegative_float_env(
    "EMBEDDING_REQUEST_QUEUE_TIMEOUT_SECONDS", 15.0
)
_EMBED_ADMISSION = EmbeddingPriorityAdmission(
    concurrency=_EMBED_CONCURRENCY,
    queue_capacity=_EMBED_QUEUE_CAPACITY,
    query_reserve=_EMBED_QUERY_RESERVE,
)

# Docker polls /readyz every few seconds. A deep provider healthcheck performs
# a real model.encode(), so cache the successful result for the active runtime
# generation instead of continuously competing with real embedding requests.
# The probe lock prevents overlapping initial model loads when a probe takes
# longer than Docker's healthcheck timeout.
_READINESS_CACHE_LOCK = threading.Lock()
_READINESS_PROBE_LOCK = threading.Lock()
_READINESS_CACHE: tuple[str, dict] | None = None


def _get_cached_readiness(runtime_fingerprint: str) -> dict | None:
    with _READINESS_CACHE_LOCK:
        if _READINESS_CACHE is None or _READINESS_CACHE[0] != runtime_fingerprint:
            return None
        return dict(_READINESS_CACHE[1])


def _set_cached_readiness(runtime_fingerprint: str, payload: dict) -> None:
    global _READINESS_CACHE
    with _READINESS_CACHE_LOCK:
        _READINESS_CACHE = (runtime_fingerprint, dict(payload))


def _reset_readiness_cache() -> None:
    """Clear process-local readiness state after tests or an in-process reload."""
    global _READINESS_CACHE
    with _READINESS_CACHE_LOCK:
        _READINESS_CACHE = None


def livez(request):
    return JsonResponse({"status": "ok"})


def readyz(request):
    try:
        runtime = get_active_embedding_runtime()
    except Exception:
        logger.exception("Embedding runtime readiness check failed")
        return JsonResponse({"status": "not_ready"}, status=503)

    runtime_fingerprint = str(runtime.runtime_fingerprint)
    cached = _get_cached_readiness(runtime_fingerprint)
    if cached is not None:
        return JsonResponse(cached)

    if not _READINESS_PROBE_LOCK.acquire(blocking=False):
        return JsonResponse(
            {"status": "not_ready", "reason": "model_initializing"}, status=503
        )

    readiness_lease = None
    try:
        # Another request may have populated the cache just before this request
        # acquired the probe lock.
        cached = _get_cached_readiness(runtime_fingerprint)
        if cached is not None:
            return JsonResponse(cached)

        # The one-time deep probe is a real encode call and therefore shares
        # the same admission limit as /embed/.
        readiness_lease = _EMBED_ADMISSION.try_acquire()
        if readiness_lease is None:
            return JsonResponse(
                {"status": "not_ready", "reason": "embedding_busy"}, status=503
            )

        provider = get_embedding_provider(runtime=runtime)
        info = provider.healthcheck()
        if runtime.dimension and info.get("dimension") != runtime.dimension:
            return JsonResponse(
                {
                    "status": "not_ready",
                    "error": (
                        f"loaded model dimension {info.get('dimension')} does not "
                        f"match active runtime dimension {runtime.dimension}"
                    ),
                },
                status=503,
            )

        payload = {
            "status": "ready",
            **info,
            "runtime_fingerprint": runtime_fingerprint,
        }
        _set_cached_readiness(runtime_fingerprint, payload)
        return JsonResponse(payload)
    except Exception:
        logger.exception("Embedding model readiness probe failed")
        return JsonResponse({"status": "not_ready"}, status=503)
    finally:
        if readiness_lease is not None:
            readiness_lease.release()
        _READINESS_PROBE_LOCK.release()


def _token_is_valid(request) -> bool:
    expected = settings.EMBEDDING_INTERNAL_TOKEN
    if not expected:
        # Fail closed: an unconfigured token must never be treated as "no
        # auth required" for an endpoint that does real model work.
        return False
    return request.headers.get("Authorization", "") == f"Bearer {expected}"


def run_embedding_with_admission(
    input_type: str,
    *,
    text: str | None = None,
    texts: list[str] | None = None,
    max_length: int | None = None,
    **provider_kwargs,
):
    """Run one model operation through the process-wide priority admission.

    Both the public internal `/embed/` handler and durable document executor
    work call this function. Keeping it here preserves the existing singleton
    and prevents a model-process job from bypassing query priority.
    """

    from .inference_context import admitted
    try:
        lease = _EMBED_ADMISSION.acquire(input_type, timeout=_EMBED_QUEUE_TIMEOUT)
    except AdmissionRejected as exc:
        raise EmbeddingBusyError(
            f"embedding service is busy ({exc.reason})",
            retry_after_seconds=float(os.getenv("EMBEDDING_BUSY_RETRY_AFTER_SECONDS", "5")),
        ) from exc
    token = admitted.set(True)
    try:
        if texts is not None:
            return embeding_models.embed_documents(texts, max_length=max_length, **provider_kwargs)
        embed_fn = (
            embeding_models.embed_query
            if input_type == "query"
            else embeding_models.embed_document
        )
        return [embed_fn(text or "", max_length=max_length, **provider_kwargs)]
    finally:
        admitted.reset(token)
        lease.release()


@csrf_exempt
@require_POST
def embed(request):
    if not _token_is_valid(request):
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "Malformed JSON request body."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Expected a JSON object."}, status=400)

    input_type = payload.get("input_type")
    if input_type not in _ALLOWED_INPUT_TYPES:
        return JsonResponse({"error": "'input_type' must be 'query' or 'document'."}, status=400)

    is_batch = "texts" in payload
    if is_batch and "text" in payload:
        return JsonResponse({"error": "Provide either 'text' or 'texts', not both."}, status=400)
    if is_batch and input_type != "document":
        # Query embedding is always latency-sensitive/single -- batching it
        # would make an interactive search wait behind other queries in the
        # same call, which defeats the point of query priority.
        return JsonResponse(
            {"error": "'texts' (batch) is only supported for input_type='document'."}, status=400
        )

    max_length = payload.get("max_length")
    if max_length is not None and not isinstance(max_length, int):
        return JsonResponse({"error": "'max_length' must be an integer."}, status=400)

    if is_batch:
        texts = payload.get("texts")
        if not isinstance(texts, list) or not texts:
            return JsonResponse({"error": "'texts' must be a non-empty array."}, status=400)
        if len(texts) > _MAX_BATCH_SIZE:
            return JsonResponse({"error": f"'texts' exceeds {_MAX_BATCH_SIZE} items."}, status=422)
        for item in texts:
            if not isinstance(item, str) or not item.strip():
                return JsonResponse({"error": "Every item in 'texts' must be non-empty text."}, status=400)
            if len(item) > _MAX_TEXT_CHARS:
                return JsonResponse(
                    {"error": f"An item in 'texts' exceeds {_MAX_TEXT_CHARS} characters."}, status=422
                )
    else:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return JsonResponse({"error": "'text' is required."}, status=400)
        if len(text) > _MAX_TEXT_CHARS:
            return JsonResponse({"error": f"'text' exceeds {_MAX_TEXT_CHARS} characters."}, status=422)

    # model_name/backend are deliberately not accepted from the caller -- this
    # endpoint always uses the server's own active embedding runtime, so app
    # and embedding-executor can never race on which model/generation to use.
    try:
        # A batch is admitted as ONE "document" job -- a single lease covers
        # the whole model.encode() call no matter how many texts it holds.
        results = run_embedding_with_admission(
            input_type,
            text=None if is_batch else text,
            texts=texts if is_batch else None,
            max_length=max_length,
        )
    except EmbeddingBusyError as exc:
        queue_state = _EMBED_ADMISSION.snapshot()
        logger.warning(
            "Embedding admission rejected: input_type=%s reason=%s state=%s",
            input_type,
            str(exc),
            queue_state,
        )
        response = JsonResponse(
            {
                "ok": False,
                "error": {
                    "code": "EMBEDDING_BUSY",
                    "message": "Embedding service is busy.",
                    "details": {"reason": str(exc).rsplit("(", 1)[-1].rstrip(")")},
                },
            },
            status=503,
        )
        response["Retry-After"] = os.getenv("EMBEDDING_BUSY_RETRY_AFTER_SECONDS", "5")
        return response

    except Exception:
        logger.exception("Internal %s embedding failed", input_type)
        return JsonResponse({"error": "embedding failed"}, status=500)

    runtime = get_active_embedding_runtime()
    for result in results:
        if runtime.dimension and len(result.dense_vector) != runtime.dimension:
            logger.error(
                "Embedding dimension mismatch: got=%s expected=%s runtime=%s",
                len(result.dense_vector),
                runtime.dimension,
                runtime.runtime_fingerprint,
            )
            return JsonResponse({"error": "embedding dimension mismatch"}, status=500)

    if is_batch:
        return JsonResponse(
            {
                "results": [
                    {
                        "dense_vector": result.dense_vector,
                        "sparse_vector": result.sparse_vector,
                        "model_name": result.model_name,
                        "backend": result.backend,
                        "dimension": result.dimension,
                    }
                    for result in results
                ],
                "runtime_fingerprint": runtime.runtime_fingerprint,
            }
        )

    result = results[0]
    return JsonResponse(
        {
            "dense_vector": result.dense_vector,
            "sparse_vector": result.sparse_vector,
            "model_name": result.model_name,
            "backend": result.backend,
            "dimension": result.dimension,
            "runtime_fingerprint": runtime.runtime_fingerprint,
        }
    )
