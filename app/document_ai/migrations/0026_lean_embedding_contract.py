import django.db.models.deletion
import pgvector.django.indexes
from django.db import migrations, models


ACTIVE_INDEX = "chunk_embedding_active_hnsw_idx"
DIMENSION_COLUMNS = {
    1024: "vector",
    640: "vector_640",
    768: "vector_768",
    1536: "vector_1536",
    384: "vector_384",
}


def compact_embedding_contract(apps, schema_editor):
    ChunkEmbedding = apps.get_model("document_ai", "ChunkEmbedding")
    EmbeddingGeneration = apps.get_model("document_ai", "EmbeddingGeneration")

    active = EmbeddingGeneration.objects.filter(status="ACTIVE").order_by(
        "-activated_at", "-updated_at"
    ).first()
    runtime_fingerprint = active.runtime_fingerprint if active else ""
    try:
        from document_ai.services.embedding_runtime_config import load_embedding_runtime

        runtime = load_embedding_runtime(scope="production")
        configured = EmbeddingGeneration.objects.filter(
            generation_id=runtime.generation_id
        ).first()
        if configured is not None:
            active = configured
            runtime_fingerprint = runtime.runtime_fingerprint
            if not configured.runtime_fingerprint:
                configured.runtime_fingerprint = runtime_fingerprint
                configured.save(update_fields=["runtime_fingerprint", "updated_at"])
    except Exception:
        pass

    if active is None:
        return

    embedding_table = schema_editor.quote_name(ChunkEmbedding._meta.db_table)
    parse_table = schema_editor.quote_name(
        apps.get_model("document_ai", "DocumentParseResult")._meta.db_table
    )
    chunk_table = schema_editor.quote_name(
        apps.get_model("document_ai", "DocumentChunk")._meta.db_table
    )

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE {parse_table} AS parse_result
            SET embedding_generation_id = %s,
                embedding_runtime_fingerprint = %s
            WHERE EXISTS (
                SELECT 1 FROM {chunk_table} chunk
                WHERE chunk.parse_result_id = parse_result.id
            )
              AND NOT EXISTS (
                SELECT 1
                FROM {chunk_table} chunk
                LEFT JOIN {embedding_table} embedding
                  ON embedding.chunk_id = chunk.id
                 AND embedding.generation_id = %s
                 AND embedding.status = 'completed'
                WHERE chunk.parse_result_id = parse_result.id
                  AND embedding.id IS NULL
            )
            """,
            [active.generation_id, runtime_fingerprint, active.generation_id],
        )
        cursor.execute(
            f"""
            DELETE FROM {embedding_table}
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY chunk_id
                               ORDER BY
                                   CASE
                                       WHEN generation_id = %s AND status = 'completed' THEN 0
                                       WHEN status = 'completed' THEN 1
                                       ELSE 2
                                   END,
                                   embedded_at DESC NULLS LAST,
                                   id DESC
                           ) AS row_number
                    FROM {embedding_table}
                ) ranked
                WHERE row_number > 1
            )
            """,
            [active.generation_id],
        )


def align_runtime_hnsw_index(apps, schema_editor):
    try:
        from document_ai.services.embedding_runtime_config import load_embedding_runtime

        dimension = load_embedding_runtime(scope="production").dimension
    except Exception:
        dimension = 1024
    column_name = DIMENSION_COLUMNS.get(dimension, "vector")
    ChunkEmbedding = apps.get_model("document_ai", "ChunkEmbedding")
    table = schema_editor.quote_name(ChunkEmbedding._meta.db_table)
    column = schema_editor.quote_name(column_name)
    index = schema_editor.quote_name(ACTIVE_INDEX)
    if column_name == "vector":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f"DROP INDEX IF EXISTS {index}")
        cursor.execute(
            f"CREATE INDEX {index} ON {table} USING hnsw "
            f"({column} vector_ip_ops) WITH (m = 16, ef_construction = 64)"
        )


class Migration(migrations.Migration):
    dependencies = [("document_ai", "0025_add_multidim_vector_fields")]

    operations = [
        migrations.AddField(
            model_name="documentparseresult",
            name="embedding_generation_id",
            field=models.CharField(blank=True, max_length=96),
        ),
        migrations.AddField(
            model_name="documentparseresult",
            name="embedding_runtime_fingerprint",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.RunPython(compact_embedding_contract, migrations.RunPython.noop),
        migrations.RemoveIndex(model_name="documentparseresult", name="document_ai_status_2b6457_idx"),
        migrations.RemoveIndex(model_name="documentchunk", name="document_ai_parse_r_6d5c6d_idx"),
        migrations.AlterField(
            model_name="documentchunk",
            name="parse_result",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="chunks",
                to="document_ai.documentparseresult",
            ),
        ),
        migrations.RemoveIndex(model_name="embeddinggeneration", name="document_ai_model_i_ba50c4_idx"),
        migrations.AlterField(
            model_name="embeddinggeneration",
            name="scope",
            field=models.CharField(default="production", max_length=32),
        ),
        migrations.AlterField(
            model_name="embeddinggeneration",
            name="runtime_fingerprint",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AlterField(
            model_name="embeddinggeneration",
            name="status",
            field=models.CharField(default="READY", max_length=32),
        ),
        migrations.RemoveConstraint(
            model_name="chunkembedding",
            name="uniq_embedding_per_chunk_generation",
        ),
        migrations.RemoveIndex(model_name="chunkembedding", name="document_ai_model_n_3ebeeb_idx"),
        migrations.RemoveIndex(model_name="chunkembedding", name="document_ai_generat_0668eb_idx"),
        migrations.RemoveIndex(model_name="chunkembedding", name="chunk_embedding_vector_hnsw_idx"),
        migrations.RemoveIndex(model_name="chunkembedding", name="chunk_emb_vec_640_hnsw_idx"),
        migrations.RemoveIndex(model_name="chunkembedding", name="chunk_emb_vec_768_hnsw_idx"),
        migrations.RemoveIndex(model_name="chunkembedding", name="chunk_emb_vec_1536_hnsw_idx"),
        migrations.RemoveIndex(model_name="chunkembedding", name="chunk_emb_vec_384_hnsw_idx"),
        migrations.RemoveField(model_name="chunkembedding", name="generation"),
        migrations.RemoveField(model_name="chunkembedding", name="model_name"),
        migrations.RemoveField(model_name="chunkembedding", name="model_version"),
        migrations.RemoveField(model_name="chunkembedding", name="model_revision"),
        migrations.RemoveField(model_name="chunkembedding", name="provider"),
        migrations.AlterField(
            model_name="chunkembedding",
            name="chunk",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="embedding",
                to="document_ai.documentchunk",
            ),
        ),
        migrations.AlterField(
            model_name="chunkembedding",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("processing", "Processing"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                    ("canceled", "Canceled"),
                ],
                default="pending",
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="chunkembedding",
            index=pgvector.django.indexes.HnswIndex(
                ef_construction=64,
                fields=["vector"],
                m=16,
                name=ACTIVE_INDEX,
                opclasses=["vector_ip_ops"],
            ),
        ),
        migrations.DeleteModel(name="ChunkSentenceEmbedding"),
        migrations.RemoveConstraint(
            model_name="chunksegmentembedding",
            name="uniq_segment_emb_per_chunk_generation_window_index",
        ),
        migrations.RemoveIndex(model_name="chunksegmentembedding", name="document_ai_chunk_i_41e103_idx"),
        migrations.RemoveField(model_name="chunksegmentembedding", name="generation"),
        migrations.AlterField(
            model_name="chunksegmentembedding",
            name="chunk",
            field=models.ForeignKey(
                db_index=False,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="segment_embeddings",
                to="document_ai.documentchunk",
            ),
        ),
        migrations.AddConstraint(
            model_name="chunksegmentembedding",
            constraint=models.UniqueConstraint(
                fields=("chunk", "window_size", "segment_index"),
                name="uniq_segment_emb_per_chunk_window_index",
            ),
        ),
        migrations.RunPython(align_runtime_hnsw_index, migrations.RunPython.noop),
    ]
