from __future__ import annotations

import logging
import os

import requests
from django.conf import settings

from .base import EmbeddingBusyError, EmbeddingProviderSpec, EmbeddingResult

logger = logging.getLogger(__name__)


def _parse_retry_after(response) -> float:
    raw_value = response.headers.get("Retry-After", "5")
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return 5.0


def _embedding_service_url() -> str:
    return os.getenv("EMBEDDING_SERVICE_URL", "http://embedding-executor:8001")


def _embedding_service_timeout() -> float:
    # A local model loads lazily on embedding-executor's first request after a restart
    # (no GPU, this can take tens of seconds); steady-state calls are ~1-2s.
    # The timeout must cover that one-time cold start, not just the warm case.
    raw_value = os.getenv("EMBEDDING_SERVICE_TIMEOUT", "60")
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return 60.0


def _embedding_batch_service_timeout() -> float:
    """Allow CPU document batches longer than latency-sensitive queries.

    A document batch can legitimately take more than the query-oriented 60s
    timeout on CPU. Keep the two failure-detection budgets independent so a
    larger ingestion timeout does not make interactive query failures slower.
    """
    raw_value = os.getenv("EMBEDDING_BATCH_SERVICE_TIMEOUT", "180")
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return 180.0


def _auth_headers() -> dict[str, str]:
    token = settings.EMBEDDING_INTERNAL_TOKEN
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


class RemoteEmbeddingProxyProvider:
    """Proxy to embedding-executor's internal /embed endpoint for both query and
    document embedding. Models (BGE-M3, SentenceTransformers, etc.) load in
    exactly one process (embedding-executor's model-owning gunicorn worker) so they
    aren't duplicated across processes and competing for GPU/CPU.
    This provider never loads the model itself.
    """

    backend = "remote_proxy"

    def __init__(
        self,
        *,
        model_name: str,
        backend: str | None = None,
        model_revision: str = "",
        dimension: int | None = None,
        supports_sparse: bool = True,
        normalize_embeddings: bool = True,
        query_prefix: str = "",
        document_prefix: str = "",
        default_distance: str = "inner_product",
    ):
        if backend:
            self.backend = backend
        self.spec = EmbeddingProviderSpec(
            backend=self.backend,
            model_name=model_name,
            model_revision=model_revision,
            dimension=dimension,
            supports_sparse=supports_sparse,
            default_distance=default_distance,
            normalize_embeddings=normalize_embeddings,
            query_prefix=query_prefix,
            document_prefix=document_prefix,
        )

    def healthcheck(self) -> dict:
        result = self.embed_query("embedding healthcheck", max_length=32)
        return {
            "backend": result.backend,
            "model_name": result.model_name,
            "dimension": result.dimension,
            "supports_sparse": bool(result.sparse_vector),
        }

    def embed_query(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        return self._embed_remote(text, input_type="query", max_length=max_length)

    def embed_document(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        return self._embed_remote(text, input_type="document", max_length=max_length)

    def embed_documents(self, texts: list[str], *, max_length: int | None = None) -> list[EmbeddingResult]:
        try:
            response = requests.post(
                f"{_embedding_service_url()}/embed/",
                json={"input_type": "document", "texts": texts, "max_length": max_length},
                headers=_auth_headers(),
                timeout=_embedding_batch_service_timeout(),
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"embedding-executor batch document embedding request failed: {exc}") from exc

        if response.status_code == 503:
            retry_after = _parse_retry_after(response)
            raise EmbeddingBusyError(
                f"embedding-executor is busy (EMBEDDING_BUSY), retry after {retry_after}s",
                retry_after_seconds=retry_after,
            )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"embedding-executor batch document embedding failed: HTTP {response.status_code}"
            ) from exc

        payload = response.json()
        return [
            EmbeddingResult(
                dense_vector=item["dense_vector"],
                sparse_vector=item.get("sparse_vector") or {},
                model_name=item.get("model_name", self.spec.model_name),
                backend=item.get("backend", self.spec.backend),
                dimension=item.get("dimension"),
            )
            for item in payload["results"]
        ]

    def _embed_remote(self, text: str, *, input_type: str, max_length: int | None) -> EmbeddingResult:
        try:
            response = requests.post(
                f"{_embedding_service_url()}/embed/",
                json={"input_type": input_type, "text": text, "max_length": max_length},
                headers=_auth_headers(),
                timeout=_embedding_service_timeout(),
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"embedding-executor {input_type} embedding request failed: {exc}"
            ) from exc

        if response.status_code == 503:
            retry_after = _parse_retry_after(response)
            raise EmbeddingBusyError(
                f"embedding-executor is busy (EMBEDDING_BUSY), retry after {retry_after}s",
                retry_after_seconds=retry_after,
            )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"embedding-executor {input_type} embedding failed: HTTP {response.status_code}"
            ) from exc

        payload = response.json()
        return EmbeddingResult(
            dense_vector=payload["dense_vector"],
            sparse_vector=payload.get("sparse_vector") or {},
            model_name=payload.get("model_name", self.spec.model_name),
            backend=payload.get("backend", self.spec.backend),
            dimension=payload.get("dimension"),
        )


RemoteBGEM3Provider = RemoteEmbeddingProxyProvider
