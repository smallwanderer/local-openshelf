from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from hashlib import sha256

from asgiref.sync import sync_to_async
from redis import Redis
from redis_semaphore import NotAvailable, Semaphore

from document_ai.services.rag_runtime_config import get_server_rag_serving_concurrency

logger = logging.getLogger(__name__)


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


def rag_retry_after_seconds() -> int:
    return _get_positive_int_env("RAG_RETRY_AFTER_SECONDS", 30)


@dataclass
class RAGAdmissionToken:
    """Idempotent handle for a Redis-backed target admission slot."""

    semaphore: Semaphore
    target: str
    limit: int
    _released: bool = False
    _release_lock: threading.Lock = field(default_factory=threading.Lock)

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self.semaphore.release()
            self._released = True

    async def release_async(self) -> None:
        await sync_to_async(self.release, thread_sensitive=False)()


def _target_admission_policy(llm_snapshot: dict | None) -> tuple[str, int]:
    snapshot = llm_snapshot or {}
    endpoint = snapshot.get("llm_endpoint")
    if endpoint is None:
        return "server", get_server_rag_serving_concurrency()

    # External providers are independent of the local GPU gate. Until endpoint
    # capabilities are persisted, use one server-wide conservative limit and a
    # separate namespace per endpoint/model so one provider cannot block another.
    endpoint_identity = ":".join(
        [
            str(getattr(endpoint, "pk", "") or snapshot.get("llm_base_url") or "external"),
            str(snapshot.get("llm_model") or "default"),
        ]
    )
    target_hash = sha256(endpoint_identity.encode("utf-8")).hexdigest()[:16]
    return (
        f"external:{target_hash}",
        _get_positive_int_env("RAG_EXTERNAL_LLM_CONCURRENCY", 4),
    )


def acquire_rag_admission_token(llm_snapshot: dict | None = None) -> RAGAdmissionToken | None:
    """Reserve a target-specific RAG slot without blocking.

    Local requests use the calibrated serving concurrency. External endpoints
    use their own namespace and never consume local GPU capacity. Acquisition
    is immediate: overload returns 503 instead of waiting inside the app or the
    inference server.

    The returned token is idempotent and must be released when the response
    stream completes or disconnects.
    """
    redis_url = os.getenv("RAG_REDIS_URL", os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"))
    target, count = _target_admission_policy(llm_snapshot)
    namespace_base = os.getenv("RAG_ADMISSION_NAMESPACE", "rag_target_admission_v2")
    # redis-semaphore only initializes its token list once per namespace, so a
    # count-specific namespace makes a calibrated concurrency change take
    # effect immediately instead of silently keeping the old token count.
    namespace = f"{namespace_base}:{target}:c{count}"
    stale_client_timeout = _get_positive_int_env("RAG_REQUEST_TIMEOUT", 300) + 60

    redis_client = Redis.from_url(redis_url)
    semaphore = Semaphore(
        redis_client,
        count=count,
        namespace=namespace,
        stale_client_timeout=stale_client_timeout,
        blocking=False,
    )
    try:
        semaphore.acquire()
    except NotAvailable:
        return None
    return RAGAdmissionToken(semaphore=semaphore, target=target, limit=count)


async def acquire_rag_admission_token_async(
    llm_snapshot: dict | None = None,
) -> RAGAdmissionToken | None:
    # redis-semaphore is synchronous, but this is a single non-blocking Redis
    # round trip rather than a long-lived worker thread.
    return await sync_to_async(acquire_rag_admission_token, thread_sensitive=False)(
        llm_snapshot
    )
