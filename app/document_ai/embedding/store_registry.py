from __future__ import annotations

from .stores import EmbeddingStore, PgVectorChunkEmbeddingStore
from document_ai.services.embedding_runtime_config import (
    EmbeddingRuntimeSnapshot,
    get_active_embedding_runtime,
)

_STORE_FACTORIES = {
    PgVectorChunkEmbeddingStore.name: PgVectorChunkEmbeddingStore,
    "pgvector_chunk_640": PgVectorChunkEmbeddingStore,
    "pgvector_chunk_768": PgVectorChunkEmbeddingStore,
    "pgvector_chunk_1536": PgVectorChunkEmbeddingStore,
    "pgvector_chunk_384": PgVectorChunkEmbeddingStore,
}


def get_embedding_store_instance(
    *,
    store_name: str | None = None,
    model_name: str | None = None,
    backend: str | None = None,
    dimension: int | None = None,
    supports_sparse: bool | None = None,
    distance_strategy: str | None = None,
    runtime: EmbeddingRuntimeSnapshot | None = None,
) -> EmbeddingStore:
    """Build a store from explicit candidate values or the active runtime."""
    resolved_runtime = runtime or get_active_embedding_runtime()
    resolved_store_name = store_name or resolved_runtime.store
    resolved_model_name = model_name or resolved_runtime.model_id
    resolved_backend = backend or resolved_runtime.provider
    resolved_dimension = (
        dimension if dimension is not None else resolved_runtime.dimension
    )
    resolved_supports_sparse = (
        supports_sparse
        if supports_sparse is not None
        else resolved_runtime.supports_sparse
    )

    if resolved_dimension is None:
        raise ValueError("Embedding dimension is required for store selection.")

    try:
        factory = _STORE_FACTORIES[resolved_store_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported embedding store: {resolved_store_name}") from exc

    return factory(
        model_name=resolved_model_name,
        backend=resolved_backend,
        dimension=resolved_dimension,
        supports_sparse=resolved_supports_sparse,
        distance_strategy=distance_strategy or resolved_runtime.distance_strategy,
        generation_id=resolved_runtime.generation_id,
        model_revision=resolved_runtime.model_revision,
        runtime_fingerprint=resolved_runtime.runtime_fingerprint,
        scope=resolved_runtime.scope,
        catalog_id=resolved_runtime.catalog_id,
        store_name=resolved_store_name,
    )


def registered_embedding_stores() -> list[str]:
    return sorted(_STORE_FACTORIES)
