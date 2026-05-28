import os
import sys

import pytest
from django.conf import settings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

from document_ai.embedding import embeding_models
from document_ai.embedding.embeding_models import EmbeddingResult
from document_ai.parsers import config


def test_embedding_token_budget_uses_chunk_budget_plus_headroom():
    settings.CHUNK_MAX_TOKENS = 1024
    settings.EMBEDDING_TOKEN_HEADROOM = 192
    if hasattr(settings, "EMBEDDING_MAX_TOKENS"):
        delattr(settings, "EMBEDDING_MAX_TOKENS")

    assert config.get_chunk_max_tokens() == 1024
    assert config.get_embedding_token_headroom() == 192
    assert config.get_embedding_max_tokens() == 1216


def test_embedding_max_tokens_respects_explicit_override():
    settings.CHUNK_MAX_TOKENS = 1024
    settings.EMBEDDING_TOKEN_HEADROOM = 256
    settings.EMBEDDING_MAX_TOKENS = 1536

    assert config.get_embedding_max_tokens() == 1536


def test_embedder_dispatches_to_bgem3_hybrid(monkeypatch):
    called = {}

    def fake_hybrid(**kwargs):
        called["backend"] = "bgem3_hybrid"
        return EmbeddingResult(
            dense_vector=[0.1, 0.2],
            sparse_vector={"101": 0.7},
        )

    monkeypatch.setattr(embeding_models, "_embed_with_bgem3_hybrid", fake_hybrid)

    result = embeding_models.bge_m3_embedder(
        text="hello world",
        model_name="dummy-model",
        backend="bgem3_hybrid",
        max_length=32,
    )

    assert called["backend"] == "bgem3_hybrid"
    assert result.dense_vector == [0.1, 0.2]
    assert result.sparse_vector == {"101": 0.7}


def test_document_and_query_entrypoints_share_embedder_policy(monkeypatch):
    calls = []

    def fake_embedder(**kwargs):
        calls.append(kwargs)
        return EmbeddingResult(dense_vector=[1.0], sparse_vector={})

    monkeypatch.setattr(embeding_models, "bge_m3_embedder", fake_embedder)
    settings.QUERY_EMBEDDING_MAX_TOKENS = 64

    embeding_models.embed_document(
        text="document chunk",
        model_name="dummy-model",
        backend="bgem3_hybrid",
        max_length=128,
    )
    embeding_models.embed_query(
        query="query",
        model_name="dummy-model",
        backend="bgem3_hybrid",
    )

    assert calls[0]["text"] == "document chunk"
    assert calls[0]["max_length"] == 128
    assert calls[1]["text"] == "query"
    assert calls[1]["max_length"] == 64


def test_sparse_vector_is_l2_normalized():
    normalized = embeding_models._normalize_sparse_vector(
        {"10": 3.0, "20": 4.0}
    )

    assert normalized["10"] == 0.6
    assert normalized["20"] == 0.8
