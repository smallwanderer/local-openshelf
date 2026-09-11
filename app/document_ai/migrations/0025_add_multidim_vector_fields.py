from django.db import migrations
import pgvector.django.indexes
import pgvector.django.vector


class Migration(migrations.Migration):

    dependencies = [
        ("document_ai", "0024_workspace_scope_llm_settings"),
    ]

    operations = [
        migrations.AddField(
            model_name="chunkembedding",
            name="vector_640",
            field=pgvector.django.vector.VectorField(dimensions=640, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="chunkembedding",
            name="vector_768",
            field=pgvector.django.vector.VectorField(dimensions=768, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="chunkembedding",
            name="vector_1536",
            field=pgvector.django.vector.VectorField(dimensions=1536, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="chunkembedding",
            name="vector_384",
            field=pgvector.django.vector.VectorField(dimensions=384, null=True, blank=True),
        ),
        migrations.AddIndex(
            model_name="chunkembedding",
            index=pgvector.django.indexes.HnswIndex(
                name="chunk_emb_vec_640_hnsw_idx",
                fields=["vector_640"],
                m=16,
                ef_construction=64,
                opclasses=["vector_ip_ops"],
            ),
        ),
        migrations.AddIndex(
            model_name="chunkembedding",
            index=pgvector.django.indexes.HnswIndex(
                name="chunk_emb_vec_768_hnsw_idx",
                fields=["vector_768"],
                m=16,
                ef_construction=64,
                opclasses=["vector_ip_ops"],
            ),
        ),
        migrations.AddIndex(
            model_name="chunkembedding",
            index=pgvector.django.indexes.HnswIndex(
                name="chunk_emb_vec_1536_hnsw_idx",
                fields=["vector_1536"],
                m=16,
                ef_construction=64,
                opclasses=["vector_ip_ops"],
            ),
        ),
        migrations.AddIndex(
            model_name="chunkembedding",
            index=pgvector.django.indexes.HnswIndex(
                name="chunk_emb_vec_384_hnsw_idx",
                fields=["vector_384"],
                m=16,
                ef_construction=64,
                opclasses=["vector_ip_ops"],
            ),
        ),
    ]
