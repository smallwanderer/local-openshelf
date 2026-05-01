import os
import sys

from django.conf import settings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from document_ai.embedding import embeding_models
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


def test_embedder_dispatches_to_mean_pooling(monkeypatch):
    called = {}

    def fake_mean_pooling(**kwargs):
        called["backend"] = "hf_mean_pooling"
        return [0.1, 0.2]

    monkeypatch.setattr(embeding_models, "_embed_with_hf_mean_pooling", fake_mean_pooling)

    result = embeding_models.bge_m3_embedder(
        text="hello world",
        model_name="dummy-model",
        backend="hf_mean_pooling",
        max_length=32,
    )

    assert called["backend"] == "hf_mean_pooling"
    assert result == [0.1, 0.2]


def test_embedder_dispatches_to_legacy_backend(monkeypatch):
    called = {}

    def fake_legacy(**kwargs):
        called["backend"] = "hf_cls_legacy"
        return [0.3, 0.4]

    monkeypatch.setattr(embeding_models, "_embed_with_hf_cls_legacy", fake_legacy)

    result = embeding_models.bge_m3_embedder(
        text="hello world",
        model_name="dummy-model",
        backend="hf_cls_legacy",
        max_length=32,
    )

    assert called["backend"] == "hf_cls_legacy"
    assert result == [0.3, 0.4]
