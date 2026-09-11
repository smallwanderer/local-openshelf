from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmbeddingProviderSpec:
    backend: str
    model_name: str
    model_revision: str
    dimension: int | None
    supports_sparse: bool
    supports_dense: bool = True
    default_distance: str = "inner_product"
    normalize_embeddings: bool = True
    query_prefix: str = ""
    document_prefix: str = ""


@dataclass
class EmbeddingResult:
    dense_vector: list[float]
    sparse_vector: dict[str, float]
    model_name: str = ""
    backend: str = ""
    dimension: int | None = None


class EmbeddingBusyError(RuntimeError):
    """embedding-executor's admission queue rejected the request (EMBEDDING_BUSY
    / HTTP 503). A distinct type from other RuntimeErrors so callers (search,
    RAG) can surface it as a retryable 503 to the client instead of a generic
    500 -- the caller is demonstrably busy with real traffic, not broken.
    """

    def __init__(self, message: str, *, retry_after_seconds: float = 5.0):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class EmbeddingProvider(Protocol):
    spec: EmbeddingProviderSpec

    def embed_document(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        ...

    def embed_query(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        ...

    def embed_documents(self, texts: list[str], *, max_length: int | None = None) -> list[EmbeddingResult]:
        ...

    def healthcheck(self) -> dict:
        ...
