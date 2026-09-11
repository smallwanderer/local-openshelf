from __future__ import annotations

import pytest

from document_ai.embedding.providers.base import EmbeddingResult
from document_ai.embedding.store_registry import (
    get_embedding_store_instance,
    registered_embedding_stores,
)
from document_ai.embedding.stores.pgvector_chunk import DIMENSION_FIELD_MAP

pytestmark = pytest.mark.unit


def test_registered_embedding_stores_includes_multidim():
    stores = registered_embedding_stores()
    assert "pgvector_chunk_1024" in stores
    assert "pgvector_chunk_640" in stores
    assert "pgvector_chunk_768" in stores
    assert "pgvector_chunk_1536" in stores
    assert "pgvector_chunk_384" in stores


def test_get_embedding_store_instance_for_harrier_640():
    store = get_embedding_store_instance(
        store_name="pgvector_chunk_640",
        model_name="microsoft/harrier-oss-v1-270m",
        backend="sentence_transformers",
        dimension=640,
        supports_sparse=False,
    )
    assert store.spec.name == "pgvector_chunk_640"
    assert store.spec.dimension == 640
    assert store.spec.dense_field == "vector_640"
    assert store.spec.supports_sparse is False

    # Valid embedding
    valid_emb = EmbeddingResult(
        dense_vector=[0.1] * 640,
        sparse_vector={},
        model_name="microsoft/harrier-oss-v1-270m",
        backend="sentence_transformers",
    )
    store.validate_embedding(valid_emb)

    # Invalid dimension rejected
    invalid_emb = EmbeddingResult(
        dense_vector=[0.1] * 768,
        sparse_vector={},
        model_name="microsoft/harrier-oss-v1-270m",
        backend="sentence_transformers",
    )
    with pytest.raises(ValueError, match="dimension does not match"):
        store.validate_embedding(invalid_emb)


def test_get_embedding_store_instance_for_granite_768():
    store = get_embedding_store_instance(
        store_name="pgvector_chunk_768",
        model_name="ibm-granite/granite-embedding-278m-multilingual",
        backend="sentence_transformers",
        dimension=768,
        supports_sparse=False,
    )
    assert store.spec.name == "pgvector_chunk_768"
    assert store.spec.dimension == 768
    assert store.spec.dense_field == "vector_768"


def test_get_embedding_store_instance_for_openai_1536():
    store = get_embedding_store_instance(
        store_name="pgvector_chunk_1536",
        model_name="text-embedding-3-small",
        backend="openai_compatible",
        dimension=1536,
        supports_sparse=False,
    )
    assert store.spec.name == "pgvector_chunk_1536"
    assert store.spec.dimension == 1536
    assert store.spec.dense_field == "vector_1536"


def test_dimension_field_mapping_covers_all_supported():
    assert DIMENSION_FIELD_MAP[1024] == "vector"
    assert DIMENSION_FIELD_MAP[640] == "vector_640"
    assert DIMENSION_FIELD_MAP[768] == "vector_768"
    assert DIMENSION_FIELD_MAP[1536] == "vector_1536"
    assert DIMENSION_FIELD_MAP[384] == "vector_384"
