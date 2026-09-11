from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .base import EmbeddingBusyError, EmbeddingProviderSpec, EmbeddingResult
from .bgem3 import coerce_dense_vector, validate_text

logger = logging.getLogger(__name__)


def _normalize_embeddings_url(base_url: str) -> str:
    cleaned = base_url.strip().rstrip("/")
    if cleaned.endswith("/embeddings"):
        return cleaned
    if cleaned.endswith("/v1"):
        return f"{cleaned}/embeddings"
    return f"{cleaned}/v1/embeddings"


class OpenAIEmbeddingProvider:
    backend = "openai_compatible"

    def __init__(
        self,
        *,
        model_name: str,
        base_url: str | None = None,
        api_key: str | None = None,
        model_revision: str = "",
        dimension: int | None = None,
        normalize_embeddings: bool = True,
        query_prefix: str = "",
        document_prefix: str = "",
        default_distance: str = "inner_product",
        timeout_seconds: float = 30.0,
    ):
        raw_base_url = (
            base_url
            or os.getenv("OPENAI_EMBEDDING_BASE_URL")
            or os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com")
        )
        self.endpoint_url = _normalize_embeddings_url(raw_base_url)
        self.api_key = (
            api_key
            or os.getenv("OPENAI_EMBEDDING_API_KEY")
            or os.getenv("EMBEDDING_API_KEY", "")
        )
        self.timeout_seconds = timeout_seconds

        self.spec = EmbeddingProviderSpec(
            backend=self.backend,
            model_name=model_name,
            model_revision=model_revision,
            dimension=dimension,
            supports_sparse=False,
            supports_dense=True,
            default_distance=default_distance,
            normalize_embeddings=normalize_embeddings,
            query_prefix=query_prefix,
            document_prefix=document_prefix,
        )

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def embed_document(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        normalized_text = validate_text(text)
        if self.spec.document_prefix:
            normalized_text = f"{self.spec.document_prefix}{normalized_text}"
        results = self._send_request([normalized_text])
        if not results:
            raise RuntimeError(f"OpenAI embedding provider returned no results for model {self.spec.model_name}")
        return results[0]

    def embed_query(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        normalized_text = validate_text(text)
        if self.spec.query_prefix:
            normalized_text = f"{self.spec.query_prefix}{normalized_text}"
        results = self._send_request([normalized_text])
        if not results:
            raise RuntimeError(f"OpenAI embedding provider returned no results for query with {self.spec.model_name}")
        return results[0]

    def embed_documents(self, texts: list[str], *, max_length: int | None = None) -> list[EmbeddingResult]:
        if not texts:
            return []
        prepared = []
        for t in texts:
            norm = validate_text(t)
            if self.spec.document_prefix:
                norm = f"{self.spec.document_prefix}{norm}"
            prepared.append(norm)
        return self._send_request(prepared)

    def healthcheck(self) -> dict:
        try:
            results = self._send_request(["healthcheck"])
            if not results:
                return {"status": "error", "error": "No embedding returned"}
            dim = len(results[0].dense_vector)
            return {
                "status": "ready",
                "backend": self.backend,
                "model_name": self.spec.model_name,
                "endpoint_url": self.endpoint_url,
                "dimension": dim,
                "supports_sparse": False,
            }
        except Exception as exc:
            logger.warning("OpenAI embedding healthcheck failed: %s", exc)
            return {
                "status": "error",
                "backend": self.backend,
                "model_name": self.spec.model_name,
                "endpoint_url": self.endpoint_url,
                "error": str(exc),
            }

    def _send_request(self, texts: list[str]) -> list[EmbeddingResult]:
        payload: dict[str, Any] = {
            "model": self.spec.model_name,
            "input": texts if len(texts) > 1 else texts[0],
        }
        if self.spec.dimension is not None and "text-embedding-3" in self.spec.model_name:
            payload["dimensions"] = self.spec.dimension

        headers = self._get_headers()
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(self.endpoint_url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"OpenAI embedding request timed out after {self.timeout_seconds}s ({self.endpoint_url})"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"OpenAI embedding request failed: {exc}") from exc

        if response.status_code == 429 or response.status_code == 503:
            retry_after = 5.0
            raw_retry = response.headers.get("Retry-After")
            if raw_retry:
                try:
                    retry_after = float(raw_retry)
                except (ValueError, TypeError):
                    pass
            raise EmbeddingBusyError(
                f"OpenAI embedding endpoint busy or rate limited ({response.status_code})",
                retry_after_seconds=retry_after,
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"OpenAI embedding endpoint returned error {response.status_code}: {response.text[:500]}"
            )

        data = response.json()
        items = data.get("data") or []
        if not isinstance(items, list):
            raise RuntimeError(f"Unexpected response format from embedding API: {data}")

        # Sort items by index to guarantee ordering matches input texts
        sorted_items = sorted(items, key=lambda x: x.get("index", 0))

        results: list[EmbeddingResult] = []
        for item in sorted_items:
            embedding_arr = item.get("embedding")
            if not embedding_arr:
                raise RuntimeError("Empty embedding in OpenAI response item")
            dense_vector = coerce_dense_vector(embedding_arr)
            results.append(
                EmbeddingResult(
                    dense_vector=dense_vector,
                    sparse_vector={},
                    model_name=self.spec.model_name,
                    backend=self.backend,
                    dimension=len(dense_vector),
                )
            )
        return results
