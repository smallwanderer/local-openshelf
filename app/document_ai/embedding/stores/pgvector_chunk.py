from __future__ import annotations

from django.utils import timezone
from pgvector.django import CosineDistance, L2Distance, MaxInnerProduct

from config.enums import AIStatus
from document_ai.embedding.providers.base import EmbeddingResult
from document_ai.models import ChunkEmbedding

from .base import EmbeddingStoreSpec


DIMENSION_FIELD_MAP: dict[int, str] = {
    1024: "vector",
    640: "vector_640",
    768: "vector_768",
    1536: "vector_1536",
    384: "vector_384",
}


class PgVectorChunkEmbeddingStore:
    name = "pgvector_chunk_1024"

    def __init__(
        self,
        *,
        model_name: str,
        backend: str,
        dimension: int,
        supports_sparse: bool,
        distance_strategy: str | None = None,
        generation_id: str = "legacy-bge-m3",
        model_revision: str = "legacy",
        runtime_fingerprint: str = "",
        scope: str = "production",
        catalog_id: str = "",
        store_name: str | None = None,
    ):
        self.runtime_fingerprint = runtime_fingerprint
        self.scope = scope
        self.catalog_id = catalog_id
        resolved_name = store_name or (f"pgvector_chunk_{dimension}" if dimension != 1024 else self.name)
        dense_field = DIMENSION_FIELD_MAP.get(dimension, "vector")
        self.spec = EmbeddingStoreSpec(
            name=resolved_name,
            dimension=dimension,
            dense_field=dense_field,
            sparse_field="sparse_vector" if supports_sparse else None,
            supports_sparse=supports_sparse,
            model_name=model_name,
            backend=backend,
            generation_id=generation_id,
            model_revision=model_revision,
            distance_strategy=distance_strategy or "inner_product",
        )

    def validate_embedding(self, embedding: EmbeddingResult) -> None:
        actual_dimension = len(embedding.dense_vector or [])
        if actual_dimension != self.spec.dimension:
            raise ValueError(
                "Embedding dimension does not match active store: "
                f"store={self.spec.name}, expected={self.spec.dimension}, actual={actual_dimension}"
            )
        if self.spec.model_name != embedding.model_name:
            raise ValueError(
                "Embedding model does not match active store: "
                f"store={self.spec.model_name}, embedding={embedding.model_name}"
            )
        if self.spec.backend != embedding.backend:
            raise ValueError(
                "Embedding backend does not match active store: "
                f"store={self.spec.backend}, embedding={embedding.backend}"
            )
        if self.spec.supports_sparse and not embedding.sparse_vector:
            raise ValueError("Active embedding store expects sparse vectors, but embedding returned none.")

    def save_chunk_embedding(self, *, chunk, embedding: EmbeddingResult, status: str = AIStatus.COMPLETED) -> None:
        from document_ai.services.executor_snapshot import validate_execution
        validate_execution()
        self.validate_embedding(embedding)
        vector_defaults = {field: None for field in DIMENSION_FIELD_MAP.values()}
        vector_defaults[self.spec.dense_field] = embedding.dense_vector
        ChunkEmbedding.objects.update_or_create(
            chunk=chunk,
            defaults={
                **vector_defaults,
                self.spec.sparse_field or "sparse_vector": embedding.sparse_vector or {},
                "embedded_at": timezone.now(),
                "status": status,
                "error_message": "",
            },
        )

    def mark_chunk_embedding_failed(self, *, chunk, error_message: str, status: str = AIStatus.FAILED) -> None:
        ChunkEmbedding.objects.update_or_create(
            chunk=chunk,
            defaults={
                **{field: None for field in DIMENSION_FIELD_MAP.values()},
                self.spec.sparse_field or "sparse_vector": {},
                "embedded_at": None,
                "status": status,
                "error_message": error_message,
            },
        )

    def completed_embedding_exists(self, *, chunk_id_ref):
        return ChunkEmbedding.objects.filter(
            chunk_id=chunk_id_ref,
            status=AIStatus.COMPLETED,
        )

    def completed_chunk_ids(self):
        return ChunkEmbedding.objects.filter(
            status=AIStatus.COMPLETED,
            chunk__parse_result__embedding_generation_id=self.spec.generation_id,
            chunk__parse_result__embedding_runtime_fingerprint=self.runtime_fingerprint,
        ).values_list("chunk_id", flat=True)

    def base_queryset(self):
        return (
            ChunkEmbedding.objects.select_related(
                "chunk",
                "chunk__parse_result",
                "chunk__parse_result__node",
            )
            .filter(
                status=AIStatus.COMPLETED,
                chunk__status=AIStatus.COMPLETED,
                chunk__parse_result__embedding_generation_id=self.spec.generation_id,
                chunk__parse_result__embedding_runtime_fingerprint=self.runtime_fingerprint,
                chunk__parse_result__node__trashed=False,
            )
        )

    def distance_annotation(self, query_vector):
        if self.spec.distance_strategy == "cosine":
            return CosineDistance(self.spec.dense_field, query_vector)
        if self.spec.distance_strategy == "l2":
            return L2Distance(self.spec.dense_field, query_vector)
        if self.spec.distance_strategy == "inner_product":
            return MaxInnerProduct(self.spec.dense_field, query_vector)
        raise ValueError(f"Unknown distance strategy: {self.spec.distance_strategy}")
