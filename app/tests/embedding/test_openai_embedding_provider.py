from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import httpx

from document_ai.embedding.providers.base import EmbeddingBusyError
from document_ai.embedding.providers.openai_compatible import (
    OpenAIEmbeddingProvider,
    _normalize_embeddings_url,
)
from document_ai.embedding.registry import get_embedding_provider

pytestmark = pytest.mark.unit


def test_normalize_embeddings_url():
    assert _normalize_embeddings_url("https://api.openai.com") == "https://api.openai.com/v1/embeddings"
    assert _normalize_embeddings_url("https://api.openai.com/v1") == "https://api.openai.com/v1/embeddings"
    assert _normalize_embeddings_url("https://api.openai.com/v1/") == "https://api.openai.com/v1/embeddings"
    assert _normalize_embeddings_url("http://ollama:11434/v1/embeddings") == "http://ollama:11434/v1/embeddings"


def test_openai_embedding_provider_embed_document():
    provider = OpenAIEmbeddingProvider(
        model_name="text-embedding-3-small",
        base_url="https://api.openai.com/v1",
        api_key="test-key-123",
        dimension=1536,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"index": 0, "embedding": [0.1] * 1536}
        ]
    }

    with patch("httpx.Client.post", return_value=mock_resp) as mock_post:
        result = provider.embed_document("Test text")
        assert len(result.dense_vector) == 1536
        assert result.sparse_vector == {}
        assert result.backend == "openai_compatible"
        assert result.model_name == "text-embedding-3-small"

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer test-key-123"
        assert call_kwargs["json"]["model"] == "text-embedding-3-small"
        assert call_kwargs["json"]["input"] == "Test text"


def test_openai_embedding_provider_batch_reorders_by_index():
    provider = OpenAIEmbeddingProvider(
        model_name="text-embedding-3-small",
        base_url="https://api.openai.com",
    )

    # Return items out of order to verify sorting by index
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"index": 1, "embedding": [0.0, 1.0, 0.0]},
            {"index": 0, "embedding": [1.0, 0.0, 0.0]},
        ]
    }

    with patch("httpx.Client.post", return_value=mock_resp):
        results = provider.embed_documents(["doc1", "doc2"])
        assert len(results) == 2
        assert results[0].dense_vector == [1.0, 0.0, 0.0]
        assert results[1].dense_vector == [0.0, 1.0, 0.0]


def test_openai_embedding_provider_handles_rate_limit():
    provider = OpenAIEmbeddingProvider(
        model_name="text-embedding-3-small",
        base_url="https://api.openai.com",
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "3"}

    with patch("httpx.Client.post", return_value=mock_resp):
        with pytest.raises(EmbeddingBusyError) as exc_info:
            provider.embed_document("Test text")
        assert exc_info.value.retry_after_seconds == 3.0


def test_openai_embedding_provider_healthcheck():
    provider = OpenAIEmbeddingProvider(
        model_name="text-embedding-3-small",
        base_url="https://api.openai.com",
        dimension=1536,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"index": 0, "embedding": [0.1] * 1536}
        ]
    }

    with patch("httpx.Client.post", return_value=mock_resp):
        health = provider.healthcheck()
        assert health["status"] == "ready"
        assert health["dimension"] == 1536
        assert health["backend"] == "openai_compatible"


def test_registry_resolves_openai_compatible_directly():
    provider = get_embedding_provider(
        backend="openai_compatible",
        model_name="text-embedding-3-small",
        dimension=1536,
    )
    assert isinstance(provider, OpenAIEmbeddingProvider)
    assert provider.spec.dimension == 1536
