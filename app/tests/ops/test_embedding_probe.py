import json
from unittest.mock import MagicMock, patch
import urllib.error

import pytest

from llm_installation.embedding_probe import (
    build_external_embedding_entry,
    load_host_embedding_catalog,
    normalize_openai_embeddings_url,
    probe_openai_embedding_endpoint,
    sanitize_model_slug,
    stage_external_embedding_generation,
)


pytestmark = pytest.mark.unit


def test_normalize_openai_embeddings_url():
    assert (
        normalize_openai_embeddings_url("http://localhost:11434")
        == "http://localhost:11434/v1/embeddings"
    )
    assert (
        normalize_openai_embeddings_url("http://localhost:11434/")
        == "http://localhost:11434/v1/embeddings"
    )
    assert (
        normalize_openai_embeddings_url("http://localhost:11434/v1")
        == "http://localhost:11434/v1/embeddings"
    )
    assert (
        normalize_openai_embeddings_url("http://localhost:11434/v1/embeddings")
        == "http://localhost:11434/v1/embeddings"
    )
    assert (
        normalize_openai_embeddings_url("https://api.openai.com")
        == "https://api.openai.com/v1/embeddings"
    )


def test_sanitize_model_slug():
    assert sanitize_model_slug("text-embedding-3-small") == "text-embedding-3-small"
    assert sanitize_model_slug("BAAI/bge-m3") == "baai-bge-m3"
    assert sanitize_model_slug("model@v1.0") == "model-v1-0"


def test_probe_openai_embedding_endpoint_success():
    dummy_response = {
        "data": [
            {
                "embedding": [0.01] * 1536,
                "index": 0,
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(dummy_response).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = probe_openai_embedding_endpoint(
            base_url="http://host.docker.internal:11434",
            model_name="text-embedding-3-small",
            api_key="test-key",
        )

    assert res.ok is True
    assert res.dimension == 1536
    assert res.store == "pgvector_chunk_1536"
    assert res.store_supported is True
    assert res.status_code == 200
    assert not res.error_message


def test_probe_openai_embedding_endpoint_unsupported_dimension():
    dummy_response = {
        "data": [
            {
                "embedding": [0.01] * 512,
                "index": 0,
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(dummy_response).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = probe_openai_embedding_endpoint(
            base_url="http://host.docker.internal:11434",
            model_name="custom-512-model",
        )

    assert res.ok is True
    assert res.dimension == 512
    assert res.store_supported is False
    assert "not supported by Dotori stores" in res.error_message


def test_probe_openai_embedding_endpoint_http_error():
    err = urllib.error.HTTPError(
        url="http://api.test/v1/embeddings",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"error": {"message": "Invalid API key"}}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        res = probe_openai_embedding_endpoint(
            base_url="http://api.test",
            model_name="test-model",
            api_key="bad-key",
        )

    assert res.ok is False
    assert res.status_code == 401
    assert "Invalid API key" in res.error_message


def test_build_external_embedding_entry_and_stage(tmp_path):
    entry = build_external_embedding_entry(
        model_name="text-embedding-3-small",
        dimension=1536,
        store="pgvector_chunk_1536",
    )
    assert entry.id == "openai-text-embedding-3-small"
    assert entry.dimension == 1536
    assert entry.provider == "openai_compatible"
    assert entry.store == "pgvector_chunk_1536"

    generation_id, gen_dir = stage_external_embedding_generation(
        scope="production",
        entry=entry,
        repo_root=tmp_path,
    )
    assert (gen_dir / "runtime.json").exists()
    payload = json.loads((gen_dir / "runtime.json").read_text(encoding="utf-8"))
    assert payload["generation_id"] == generation_id
    assert payload["dimension"] == 1536
    assert payload["provider"] == "openai_compatible"
    assert payload["runtime_fingerprint"]


def test_load_host_embedding_catalog():
    catalog = load_host_embedding_catalog()
    assert len(catalog) > 0
    ids = [item.id for item in catalog]
    assert "bge-m3-hybrid" in ids
    bge = next(item for item in catalog if item.id == "bge-m3-hybrid")
    assert bge.dimension == 1024
    assert bge.store == "pgvector_chunk_1024"
