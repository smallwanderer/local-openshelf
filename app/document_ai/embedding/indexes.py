"""Runtime-owned pgvector index maintenance.

Only one embedding dimension is active on a Dotori server. Keeping one HNSW
index and replacing it during the rare model-change maintenance window avoids
permanent indexes for every catalog dimension.
"""

from __future__ import annotations

from django.db import connection

from document_ai.embedding.stores.pgvector_chunk import DIMENSION_FIELD_MAP
from document_ai.models import ChunkEmbedding


ACTIVE_HNSW_INDEX = "chunk_embedding_active_hnsw_idx"
LEGACY_HNSW_INDEXES = (
    "chunk_embedding_vector_hnsw_idx",
    "chunk_emb_vec_640_hnsw_idx",
    "chunk_emb_vec_768_hnsw_idx",
    "chunk_emb_vec_1536_hnsw_idx",
    "chunk_emb_vec_384_hnsw_idx",
)


def replace_active_hnsw_index(*, dimension: int) -> None:
    """Replace the single HNSW index for the selected runtime dimension.

    Model changes already run as an explicit maintenance operation, so a
    regular CREATE INDEX is intentional here: it finishes before the runtime
    is activated and avoids leaving a half-switched serving contract.
    """

    try:
        field_name = DIMENSION_FIELD_MAP[dimension]
    except KeyError as exc:
        raise ValueError(f"Unsupported indexed embedding dimension: {dimension}") from exc

    table = connection.ops.quote_name(ChunkEmbedding._meta.db_table)
    column = connection.ops.quote_name(
        ChunkEmbedding._meta.get_field(field_name).column
    )
    active_index = connection.ops.quote_name(ACTIVE_HNSW_INDEX)

    with connection.cursor() as cursor:
        for index_name in (*LEGACY_HNSW_INDEXES, ACTIVE_HNSW_INDEX):
            cursor.execute(
                f"DROP INDEX IF EXISTS {connection.ops.quote_name(index_name)}"
            )
        cursor.execute(
            f"CREATE INDEX {active_index} ON {table} "
            f"USING hnsw ({column} vector_ip_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )
