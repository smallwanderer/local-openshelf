from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from document_ai.embedding.providers.sentence_transformers import (
    SentenceTransformersEmbeddingProvider,
)
from document_ai.embedding.registry import get_embedding_provider

pytestmark = pytest.mark.unit


class FakeModel:
    def __init__(self, dimension=640):
        self.dimension = dimension
        self.max_seq_length = 512

    def encode(self, texts, batch_size=None, normalize_embeddings=True, show_progress_bar=False):
        if isinstance(texts, str):
            texts = [texts]
        # Generate predictable float vectors of length self.dimension
        return [[float(i + 1) / 100.0] * self.dimension for i, _ in enumerate(texts)]

    def to(self, device):
        return self


def test_sentence_transformers_provider_embed_document(monkeypatch):
    fake_model = FakeModel(dimension=640)
    monkeypatch.setattr(
        "document_ai.embedding.providers.sentence_transformers.get_sentence_transformer_model",
        lambda *args, **kwargs: fake_model,
    )

    provider = SentenceTransformersEmbeddingProvider(
        model_name="microsoft/harrier-oss-v1-270m",
        dimension=640,
        query_prefix="Instruct: Given a query, retrieve passages\nQuery: ",
        document_prefix="",
    )

    result = provider.embed_document("This is a sample document.")
    assert len(result.dense_vector) == 640
    assert result.sparse_vector == {}
    assert result.dimension == 640
    assert result.backend == "sentence_transformers"
    assert result.model_name == "microsoft/harrier-oss-v1-270m"
    assert provider.spec.supports_sparse is False


def test_sentence_transformers_provider_embed_query_with_prefix(monkeypatch):
    captured_texts = []

    class CapturingModel(FakeModel):
        def encode(self, texts, **kwargs):
            captured_texts.extend(texts)
            return super().encode(texts, **kwargs)

    capturing_model = CapturingModel(dimension=640)
    monkeypatch.setattr(
        "document_ai.embedding.providers.sentence_transformers.get_sentence_transformer_model",
        lambda *args, **kwargs: capturing_model,
    )

    prefix = "Instruct: Given a web search query, retrieve relevant passages\nQuery: "
    provider = SentenceTransformersEmbeddingProvider(
        model_name="microsoft/harrier-oss-v1-270m",
        dimension=640,
        query_prefix=prefix,
    )

    result = provider.embed_query("protein intake for female")
    assert captured_texts == [f"{prefix}protein intake for female"]
    assert len(result.dense_vector) == 640


def test_sentence_transformers_provider_embed_documents_batch(monkeypatch):
    fake_model = FakeModel(dimension=768)
    monkeypatch.setattr(
        "document_ai.embedding.providers.sentence_transformers.get_sentence_transformer_model",
        lambda *args, **kwargs: fake_model,
    )

    provider = SentenceTransformersEmbeddingProvider(
        model_name="ibm-granite/granite-embedding-278m-multilingual",
        dimension=768,
    )

    results = provider.embed_documents(["First doc", "Second doc", "Third doc"])
    assert len(results) == 3
    for r in results:
        assert len(r.dense_vector) == 768
        assert r.sparse_vector == {}


def test_sentence_transformers_provider_healthcheck(monkeypatch):
    fake_model = FakeModel(dimension=640)
    monkeypatch.setattr(
        "document_ai.embedding.providers.sentence_transformers.get_sentence_transformer_model",
        lambda *args, **kwargs: fake_model,
    )

    provider = SentenceTransformersEmbeddingProvider(
        model_name="microsoft/harrier-oss-v1-270m",
        dimension=640,
    )

    health = provider.healthcheck()
    assert health["status"] == "ready"
    assert health["backend"] == "sentence_transformers"
    assert health["dimension"] == 640
    assert health["supports_sparse"] is False


def test_registry_resolves_sentence_transformers_in_model_process(monkeypatch):
    monkeypatch.setenv("DOTORI_EMBEDDING_MODEL_PROCESS", "1")
    provider = get_embedding_provider(
        backend="sentence_transformers",
        model_name="microsoft/harrier-oss-v1-270m",
        dimension=640,
    )
    assert isinstance(provider, SentenceTransformersEmbeddingProvider)
    assert provider.spec.dimension == 640


def test_registry_resolves_proxy_in_worker_process(monkeypatch):
    monkeypatch.delenv("DOTORI_EMBEDDING_MODEL_PROCESS", raising=False)
    provider = get_embedding_provider(
        backend="sentence_transformers",
        model_name="microsoft/harrier-oss-v1-270m",
        dimension=640,
    )
    from document_ai.embedding.providers.remote import RemoteEmbeddingProxyProvider
    assert isinstance(provider, RemoteEmbeddingProxyProvider)
    assert provider.spec.dimension == 640
    assert provider.backend == "sentence_transformers"
