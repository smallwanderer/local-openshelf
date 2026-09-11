import json
import os
import sys

import pytest
from django.conf import settings

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.unit

from document_ai.embedding import embeding_models
from document_ai.embedding.embeding_models import EmbeddingResult
from document_ai.embedding.store_registry import get_embedding_store_instance
from document_ai.parsers import config
from document_ai.embedding import executor as embedding_processing


def test_chunk_token_budget_is_embedding_budget_minus_headroom(settings):
    settings.EMBEDDING_MAX_TOKENS = 1216
    settings.EMBEDDING_TOKEN_HEADROOM = 192

    assert config.get_chunk_max_tokens() == 1024
    assert config.get_embedding_token_headroom() == 192
    assert config.get_embedding_max_tokens() == 1216


def test_embedding_max_tokens_is_fixed_independently_of_chunk_budget(settings):
    settings.EMBEDDING_TOKEN_HEADROOM = 256
    settings.EMBEDDING_MAX_TOKENS = 1536

    assert config.get_embedding_max_tokens() == 1536
    assert config.get_chunk_max_tokens() == 1280


def test_chunk_token_budget_rejects_headroom_at_or_above_embedding_limit(settings):
    settings.EMBEDDING_MAX_TOKENS = 256
    settings.EMBEDDING_TOKEN_HEADROOM = 256

    with pytest.raises(ValueError, match="must be greater"):
        config.get_chunk_max_tokens()


def test_embedding_input_preparation_truncates_overflow(monkeypatch, settings):
    settings.EMBEDDING_MAX_TOKENS = 5

    class FakeTokenizer:
        def count_tokens(self, text):
            return len(text.split())

    class FakeRawTokenizer:
        def encode(self, text, add_special_tokens=False):
            return text.split()

        def decode(self, token_ids, skip_special_tokens=True, clean_up_tokenization_spaces=True):
            return " ".join(token_ids)

    monkeypatch.setattr(embedding_processing, "get_hf_tokenizer", lambda: FakeTokenizer())
    monkeypatch.setattr(embedding_processing, "get_raw_tokenizer", lambda: FakeRawTokenizer())

    text, token_count, truncated = embedding_processing.prepare_embedding_input_text(
        "one two three four five six"
    )

    assert text == "one two three four five"
    assert token_count == 5
    assert truncated is True


def test_embedding_input_preparation_accepts_limit(monkeypatch, settings):
    settings.EMBEDDING_MAX_TOKENS = 5

    class FakeTokenizer:
        def count_tokens(self, text):
            return 5

    monkeypatch.setattr(embedding_processing, "get_hf_tokenizer", lambda: FakeTokenizer())

    text, token_count, truncated = embedding_processing.prepare_embedding_input_text("fits")

    assert text == "fits"
    assert token_count == 5
    assert truncated is False


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


def test_embed_document_routes_through_bge_m3_embedder(monkeypatch):
    calls = []

    def fake_embedder(**kwargs):
        calls.append(kwargs)
        return EmbeddingResult(dense_vector=[1.0], sparse_vector={})

    monkeypatch.setattr(embeding_models, "bge_m3_embedder", fake_embedder)

    embeding_models.embed_document(
        text="document chunk",
        model_name="dummy-model",
        backend="bgem3_hybrid",
        max_length=128,
    )

    assert calls[0]["text"] == "document chunk"
    assert calls[0]["max_length"] == 128


def test_embed_query_calls_provider_embed_query_directly(monkeypatch):
    # embed_query() must call provider.embed_query(), not provider.embed_document()
    # (via bge_m3_embedder) -- the two can carry different query/document prefixes.
    calls = []

    class FakeProvider:
        def embed_query(self, text, *, max_length=None):
            calls.append({"text": text, "max_length": max_length})
            return EmbeddingResult(dense_vector=[1.0], sparse_vector={})

        def embed_document(self, text, *, max_length=None):
            raise AssertionError("embed_query() must not call embed_document()")

    monkeypatch.setattr(embeding_models, "get_embedding_provider", lambda **kwargs: FakeProvider())
    settings.SEARCH_QUERY_EMBEDDING_MAX_TOKENS = 64

    embeding_models.embed_query(
        query="query",
        model_name="dummy-model",
        backend="bgem3_hybrid",
    )

    assert calls[0]["text"] == "query"
    assert calls[0]["max_length"] == 64


def test_sparse_vector_is_l2_normalized():
    normalized = embeding_models._normalize_sparse_vector(
        {"10": 3.0, "20": 4.0}
    )

    assert normalized["10"] == 0.6
    assert normalized["20"] == 0.8


def test_embedding_store_rejects_dimension_mismatch(settings):
    settings.EMBEDDING_MODEL = "dummy-model"
    settings.EMBEDDING_BACKEND = "bgem3_hybrid"
    settings.EMBEDDING_DIMENSION = 1024
    settings.EMBEDDING_STORE = "pgvector_chunk_1024"
    settings.EMBEDDING_SPARSE_ENABLED = True

    store = get_embedding_store_instance(
        store_name="pgvector_chunk_1024",
        model_name="dummy-model",
        backend="bgem3_hybrid",
        dimension=1024,
        supports_sparse=True,
    )
    embedding = EmbeddingResult(
        dense_vector=[0.1, 0.2],
        sparse_vector={"101": 1.0},
        model_name="dummy-model",
        backend="bgem3_hybrid",
        dimension=2,
    )

    with pytest.raises(ValueError, match="dimension"):
        store.validate_embedding(embedding)


def test_embedding_store_accepts_active_model_backend_and_dimension(settings):
    store = get_embedding_store_instance(
        store_name="pgvector_chunk_1024",
        model_name="BAAI/bge-m3",
        backend="bgem3_hybrid",
        dimension=1024,
        supports_sparse=True,
    )
    embedding = EmbeddingResult(
        dense_vector=[0.1] * 1024,
        sparse_vector={"101": 1.0},
        model_name="BAAI/bge-m3",
        backend="bgem3_hybrid",
        dimension=1024,
    )

    store.validate_embedding(embedding)


def _fake_active_runtime():
    from types import SimpleNamespace

    return SimpleNamespace(
        provider="bgem3_hybrid",
        model_id="BAAI/bge-m3",
        model_revision="",
        dimension=1024,
        normalize_embeddings=True,
        query_prefix="",
        document_prefix="",
        runtime_fingerprint="fp-test",
        supports_sparse=True,
        distance_strategy="cosine",
        languages=["en", "ko", "zh"],
    )


@pytest.fixture
def reset_internal_readiness_cache():
    from document_ai.embedding import internal_views

    internal_views._reset_readiness_cache()
    yield
    internal_views._reset_readiness_cache()


def test_registry_returns_real_provider_for_embedding_model_process(monkeypatch):
    from document_ai.embedding import registry
    from document_ai.embedding.providers.bgem3 import BGEM3HybridProvider

    monkeypatch.setenv("DOTORI_EMBEDDING_MODEL_PROCESS", "1")
    monkeypatch.setattr(registry, "get_active_embedding_runtime", _fake_active_runtime)

    provider = registry.get_embedding_provider(backend="bgem3_hybrid", model_name="BAAI/bge-m3", dimension=1024)

    assert type(provider) is BGEM3HybridProvider


def test_registry_returns_remote_provider_outside_model_process(monkeypatch):
    from document_ai.embedding import registry
    from document_ai.embedding.providers.remote import RemoteBGEM3Provider

    monkeypatch.delenv("DOTORI_EMBEDDING_MODEL_PROCESS", raising=False)
    monkeypatch.setattr(registry, "get_active_embedding_runtime", _fake_active_runtime)

    provider = registry.get_embedding_provider(backend="bgem3_hybrid", model_name="BAAI/bge-m3", dimension=1024)

    assert isinstance(provider, RemoteBGEM3Provider)
    assert provider.spec.model_name == "BAAI/bge-m3"


def test_registry_treats_embedding_consumer_as_remote(monkeypatch):
    # embedding-executor-consumer does NOT set
    # DOTORI_EMBEDDING_MODEL_PROCESS (only its gunicorn process does), so it
    # must also proxy through /embed/ rather than loading its own copy.
    from document_ai.embedding import registry
    from document_ai.embedding.providers.remote import RemoteBGEM3Provider

    monkeypatch.setenv("SERVICE_NAME", "embedding-executor-consumer")
    monkeypatch.delenv("DOTORI_EMBEDDING_MODEL_PROCESS", raising=False)
    monkeypatch.setattr(registry, "get_active_embedding_runtime", _fake_active_runtime)

    provider = registry.get_embedding_provider(backend="bgem3_hybrid", model_name="BAAI/bge-m3", dimension=1024)

    assert isinstance(provider, RemoteBGEM3Provider)


def test_remote_provider_embed_query_calls_embedding_executor(monkeypatch, settings):
    from document_ai.embedding.providers.remote import RemoteBGEM3Provider

    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    provider = RemoteBGEM3Provider(model_name="BAAI/bge-m3", dimension=1024)

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "dense_vector": [0.1, 0.2],
                "sparse_vector": {"5": 1.0},
                "model_name": "BAAI/bge-m3",
                "backend": "bgem3_hybrid",
                "dimension": 2,
            }

    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("document_ai.embedding.providers.remote.requests.post", fake_post)

    result = provider.embed_query("텍스트 검색", max_length=32)

    assert result.dense_vector == [0.1, 0.2]
    assert result.sparse_vector == {"5": 1.0}
    assert captured["json"] == {"input_type": "query", "text": "텍스트 검색", "max_length": 32}
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["url"].endswith("/embed/")
    assert captured["timeout"] == 60.0


def test_remote_provider_embed_document_calls_embedding_executor(monkeypatch, settings):
    from document_ai.embedding.providers.remote import RemoteBGEM3Provider

    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    provider = RemoteBGEM3Provider(model_name="BAAI/bge-m3", dimension=1024)

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"dense_vector": [0.1], "sparse_vector": {}, "dimension": 1}

    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr("document_ai.embedding.providers.remote.requests.post", fake_post)

    provider.embed_document("document chunk text")

    assert captured["json"]["input_type"] == "document"
    assert captured["json"]["text"] == "document chunk text"
    assert "model_name" not in captured["json"]
    assert "backend" not in captured["json"]


def test_remote_provider_embed_query_wraps_http_errors(monkeypatch, settings):
    from document_ai.embedding.providers.remote import RemoteBGEM3Provider

    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    provider = RemoteBGEM3Provider(model_name="BAAI/bge-m3", dimension=1024)

    class FakeResponse:
        status_code = 500
        text = "boom"

        def raise_for_status(self):
            import requests

            raise requests.HTTPError("500 error")

    monkeypatch.setattr(
        "document_ai.embedding.providers.remote.requests.post",
        lambda *a, **k: FakeResponse(),
    )

    with pytest.raises(RuntimeError, match="query embedding failed"):
        provider.embed_query("q")


def test_remote_provider_embed_query_raises_on_busy(monkeypatch, settings):
    from document_ai.embedding.providers.remote import RemoteBGEM3Provider

    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    provider = RemoteBGEM3Provider(model_name="BAAI/bge-m3", dimension=1024)

    class FakeResponse:
        status_code = 503
        headers = {"Retry-After": "5"}

    monkeypatch.setattr(
        "document_ai.embedding.providers.remote.requests.post",
        lambda *a, **k: FakeResponse(),
    )

    with pytest.raises(RuntimeError, match="EMBEDDING_BUSY"):
        provider.embed_query("q")


@pytest.mark.django_db
def test_internal_embed_view_returns_vector_for_query(client, monkeypatch, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    from document_ai.embedding import internal_views

    def fake_embed_query(text, *, max_length=None):
        return EmbeddingResult(
            dense_vector=[0.3, 0.4] * 512,
            sparse_vector={"9": 1.0},
            model_name="BAAI/bge-m3",
            backend="bgem3_hybrid",
            dimension=1024,
        )

    monkeypatch.setattr(embeding_models, "embed_query", fake_embed_query)
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", _fake_active_runtime)

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "query", "text": "hello"}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dense_vector"] == [0.3, 0.4] * 512
    assert body["sparse_vector"] == {"9": 1.0}
    assert body["runtime_fingerprint"] == "fp-test"


@pytest.mark.django_db
def test_internal_embed_view_routes_document_input_type(client, monkeypatch, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    from document_ai.embedding import internal_views

    calls = []

    def fake_embed_document(text, *, max_length=None):
        calls.append(text)
        return EmbeddingResult(dense_vector=[0.1] * 1024, sparse_vector={}, dimension=1024)

    monkeypatch.setattr(embeding_models, "embed_document", fake_embed_document)
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", _fake_active_runtime)

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "document", "text": "chunk text"}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token",
    )

    assert response.status_code == 200
    assert calls == ["chunk text"]


@pytest.mark.django_db
def test_internal_embed_view_rejects_missing_auth(client, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "query", "text": "hello"}),
        content_type="application/json",
    )

    assert response.status_code == 401


@pytest.mark.django_db
def test_internal_embed_view_rejects_wrong_token(client, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "query", "text": "hello"}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer wrong-token",
    )

    assert response.status_code == 401


@pytest.mark.django_db
def test_internal_embed_view_fails_closed_when_token_unset(client, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = ""

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "query", "text": "hello"}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer anything",
    )

    assert response.status_code == 401


@pytest.mark.django_db
def test_internal_embed_view_rejects_invalid_input_type(client, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "summary", "text": "hello"}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token",
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_internal_embed_view_rejects_missing_text(client, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "query"}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token",
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_internal_embed_view_surfaces_failure_generically(client, monkeypatch, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    from document_ai.embedding import internal_views

    def failing_embed_query(text, **kwargs):
        raise RuntimeError("GPU OOM at address 0x1234, model=/secret/path")

    monkeypatch.setattr(embeding_models, "embed_query", failing_embed_query)

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "query", "text": "hello"}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token",
    )

    assert response.status_code == 500
    assert "0x1234" not in response.json()["error"]
    assert "/secret/path" not in response.json()["error"]


@pytest.mark.django_db
def test_internal_embed_view_returns_busy_when_queue_is_unavailable(
    client, monkeypatch, settings
):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    from document_ai.embedding.admission import EmbeddingPriorityAdmission
    from document_ai.embedding import internal_views

    admission = EmbeddingPriorityAdmission(
        concurrency=1, queue_capacity=0, query_reserve=0
    )
    active_lease = admission.try_acquire()
    assert active_lease is not None
    monkeypatch.setattr(internal_views, "_EMBED_ADMISSION", admission)
    try:
        response = client.post(
            "/embed/",
            data=json.dumps({"input_type": "query", "text": "hello"}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer test-token",
        )
    finally:
        active_lease.release()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "EMBEDDING_BUSY"
    assert response.json()["error"]["details"]["reason"] == "queue_full"
    assert "Retry-After" in response


@pytest.mark.django_db
def test_internal_embed_view_allows_configured_concurrency(client, monkeypatch, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    from document_ai.embedding.admission import EmbeddingPriorityAdmission
    from document_ai.embedding import internal_views

    def fake_embed_query(text, *, max_length=None):
        return EmbeddingResult(dense_vector=[0.1] * 1024, sparse_vector={}, dimension=1024)

    monkeypatch.setattr(embeding_models, "embed_query", fake_embed_query)
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", _fake_active_runtime)

    admission = EmbeddingPriorityAdmission(
        concurrency=2, queue_capacity=2, query_reserve=1
    )
    active_lease = admission.try_acquire()
    assert active_lease is not None
    monkeypatch.setattr(internal_views, "_EMBED_ADMISSION", admission)
    try:
        response = client.post(
            "/embed/",
            data=json.dumps({"input_type": "query", "text": "hello"}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer test-token",
        )
    finally:
        active_lease.release()

    assert response.status_code == 200


@pytest.mark.django_db
def test_internal_embed_view_rejects_dimension_mismatch(client, monkeypatch, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    from document_ai.embedding import internal_views

    def fake_embed_query(text, *, max_length=None):
        return EmbeddingResult(dense_vector=[0.1, 0.2], sparse_vector={}, dimension=2)

    monkeypatch.setattr(embeding_models, "embed_query", fake_embed_query)
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", _fake_active_runtime)

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "query", "text": "hello"}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token",
    )

    assert response.status_code == 500
    assert "mismatch" in response.json()["error"]


def test_bgem3_embed_documents_batch_calls_encode_once_with_batch_size_n(monkeypatch):
    from document_ai.embedding.providers import bgem3
    from document_ai.embedding.providers.bgem3 import BGEM3HybridProvider

    encode_calls = []

    # Already L2-normalized so coerce_dense_vector's check_normalized() is a
    # no-op -- lets the assertion compare exact values per batch row.
    class FakeModel:
        def encode(self, texts, **kwargs):
            encode_calls.append({"texts": list(texts), "batch_size": kwargs["batch_size"]})
            return {
                "dense_vecs": [[1.0, 0.0], [0.0, 1.0], [0.6, 0.8]],
                "lexical_weights": [{"1": 0.5}, {"2": 0.6}, {"3": 0.7}],
            }

    monkeypatch.setattr(bgem3, "get_bgem3_model", lambda *a, **k: FakeModel())

    provider = BGEM3HybridProvider(model_name="BAAI/bge-m3", dimension=2)
    results = provider.embed_documents(["one", "two", "three"], max_length=32)

    assert len(encode_calls) == 1
    assert encode_calls[0]["texts"] == ["one", "two", "three"]
    assert encode_calls[0]["batch_size"] == 3
    assert [r.dense_vector for r in results] == [[1.0, 0.0], [0.0, 1.0], [0.6, 0.8]]
    # coerce_sparse_vector L2-normalizes -- a single positive weight always
    # normalizes to 1.0, so this also verifies each row maps to its own dict
    # (not a shared/overwritten one).
    assert [r.sparse_vector for r in results] == [{"1": 1.0}, {"2": 1.0}, {"3": 1.0}]


def test_bgem3_embed_documents_batch_applies_document_prefix(monkeypatch):
    from document_ai.embedding.providers import bgem3
    from document_ai.embedding.providers.bgem3 import BGEM3HybridProvider

    seen_texts = []

    class FakeModel:
        def encode(self, texts, **kwargs):
            seen_texts.extend(texts)
            return {
                "dense_vecs": [[0.1] for _ in texts],
                "lexical_weights": [{} for _ in texts],
            }

    monkeypatch.setattr(bgem3, "get_bgem3_model", lambda *a, **k: FakeModel())

    provider = BGEM3HybridProvider(model_name="BAAI/bge-m3", dimension=1, document_prefix="passage: ")
    provider.embed_documents(["a", "b"])

    assert seen_texts == ["passage: a", "passage: b"]


def test_remote_provider_embed_documents_calls_embedding_executor_once(monkeypatch, settings):
    from document_ai.embedding.providers.remote import RemoteBGEM3Provider

    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    provider = RemoteBGEM3Provider(model_name="BAAI/bge-m3", dimension=1024)

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {"dense_vector": [0.1], "sparse_vector": {"1": 0.5}, "model_name": "BAAI/bge-m3", "backend": "bgem3_hybrid", "dimension": 1},
                    {"dense_vector": [0.2], "sparse_vector": {"2": 0.6}, "model_name": "BAAI/bge-m3", "backend": "bgem3_hybrid", "dimension": 1},
                ]
            }

    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("document_ai.embedding.providers.remote.requests.post", fake_post)

    results = provider.embed_documents(["chunk one", "chunk two"], max_length=64)

    assert captured["json"] == {"input_type": "document", "texts": ["chunk one", "chunk two"], "max_length": 64}
    assert captured["timeout"] == 180.0
    assert [r.dense_vector for r in results] == [[0.1], [0.2]]
    assert [r.sparse_vector for r in results] == [{"1": 0.5}, {"2": 0.6}]


def test_embed_documents_wrapper_resolves_provider_and_calls_batch_method(monkeypatch):
    calls = []

    class FakeProvider:
        def embed_documents(self, texts, *, max_length=None):
            calls.append({"texts": texts, "max_length": max_length})
            return [EmbeddingResult(dense_vector=[1.0], sparse_vector={}) for _ in texts]

    monkeypatch.setattr(embeding_models, "get_embedding_provider", lambda **kwargs: FakeProvider())

    results = embeding_models.embed_documents(
        texts=["a", "b"],
        model_name="dummy-model",
        backend="bgem3_hybrid",
        max_length=128,
    )

    assert len(results) == 2
    assert calls[0]["texts"] == ["a", "b"]
    assert calls[0]["max_length"] == 128


@pytest.mark.django_db
def test_internal_embed_view_batch_returns_results_for_each_text(client, monkeypatch, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    from document_ai.embedding import internal_views

    def fake_embed_documents(texts, *, max_length=None):
        return [
            EmbeddingResult(dense_vector=[0.1] * 1024, sparse_vector={"1": 0.5}, dimension=1024)
            for _ in texts
        ]

    monkeypatch.setattr(embeding_models, "embed_documents", fake_embed_documents)
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", _fake_active_runtime)

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "document", "texts": ["chunk one", "chunk two"]}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token",
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 2
    assert body["results"][0]["sparse_vector"] == {"1": 0.5}
    assert body["runtime_fingerprint"] == "fp-test"


@pytest.mark.django_db
def test_internal_embed_view_batch_rejects_query_input_type(client, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "query", "texts": ["a", "b"]}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token",
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_internal_embed_view_batch_rejects_oversized_batch(client, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    from document_ai.embedding import internal_views

    oversized = ["text"] * (internal_views._MAX_BATCH_SIZE + 1)

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "document", "texts": oversized}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token",
    )

    assert response.status_code == 422


@pytest.mark.django_db
def test_internal_embed_view_batch_acquires_admission_once(client, monkeypatch, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"
    settings.EMBEDDING_INTERNAL_TOKEN = "test-token"
    from document_ai.embedding import internal_views

    acquire_calls = []
    original_acquire = internal_views._EMBED_ADMISSION.acquire

    def counting_acquire(request_type, *, timeout):
        acquire_calls.append(request_type)
        return original_acquire(request_type, timeout=timeout)

    monkeypatch.setattr(internal_views._EMBED_ADMISSION, "acquire", counting_acquire)

    def fake_embed_documents(texts, *, max_length=None):
        return [EmbeddingResult(dense_vector=[0.1] * 1024, sparse_vector={}, dimension=1024) for _ in texts]

    monkeypatch.setattr(embeding_models, "embed_documents", fake_embed_documents)
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", _fake_active_runtime)

    response = client.post(
        "/embed/",
        data=json.dumps({"input_type": "document", "texts": ["a", "b", "c"]}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token",
    )

    assert response.status_code == 200
    assert acquire_calls == ["document"]


def test_group_chunks_into_batches_respects_count_and_token_budget(settings):
    settings.EMBEDDING_DOCUMENT_BATCH_MAX_CHUNKS = 2
    settings.EMBEDDING_DOCUMENT_BATCH_MAX_TOKENS = 100

    chunk_rows = [(1, 60), (2, 60), (3, 10), (4, None), (5, None), (6, None)]

    batches = embedding_processing._group_chunks_into_batches(chunk_rows)

    # (1,60) starts a batch; (2,60) would push it to 120 > 100 -> new batch.
    # (2,60)+(3,10)=70 <= 100 and count 2 -> stays together.
    # (4,None)+(5,None) hits the count cap of 2 -> (6,None) starts a new batch.
    assert batches == [[1], [2, 3], [4, 5], [6]]


@pytest.mark.django_db
def test_internal_livez(client, settings):
    settings.ROOT_URLCONF = "config.embedding_urls"

    response = client.get("/livez")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_internal_readyz_reports_ready_and_reuses_probe(
    client, monkeypatch, settings, reset_internal_readiness_cache
):
    settings.ROOT_URLCONF = "config.embedding_urls"
    from document_ai.embedding import internal_views

    healthcheck_calls = []

    class FakeProvider:
        def healthcheck(self):
            healthcheck_calls.append("called")
            return {
                "backend": "bgem3_hybrid",
                "model_name": "BAAI/bge-m3",
                "dimension": 1024,
                "supports_sparse": True,
            }

    monkeypatch.setattr(internal_views, "get_embedding_provider", lambda **kwargs: FakeProvider())
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", _fake_active_runtime)

    response = client.get("/readyz")
    cached_response = client.get("/readyz")

    assert response.status_code == 200
    assert cached_response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["dimension"] == 1024
    assert body["runtime_fingerprint"] == "fp-test"
    assert healthcheck_calls == ["called"]


@pytest.mark.django_db
def test_internal_readyz_reports_not_ready_on_dimension_mismatch(
    client, monkeypatch, settings, reset_internal_readiness_cache
):
    settings.ROOT_URLCONF = "config.embedding_urls"
    from document_ai.embedding import internal_views

    class FakeProvider:
        def healthcheck(self):
            return {"backend": "bgem3_hybrid", "model_name": "BAAI/bge-m3", "dimension": 512, "supports_sparse": True}

    monkeypatch.setattr(internal_views, "get_embedding_provider", lambda **kwargs: FakeProvider())
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", _fake_active_runtime)

    response = client.get("/readyz")

    assert response.status_code == 503


@pytest.mark.django_db
def test_internal_readyz_reports_not_ready_on_load_failure(
    client, monkeypatch, settings, reset_internal_readiness_cache
):
    settings.ROOT_URLCONF = "config.embedding_urls"
    from document_ai.embedding import internal_views

    def failing_provider(**kwargs):
        raise RuntimeError("model failed to load")

    monkeypatch.setattr(internal_views, "get_embedding_provider", failing_provider)
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", _fake_active_runtime)

    response = client.get("/readyz")

    assert response.status_code == 503


@pytest.mark.django_db
def test_internal_readyz_uses_cached_result_when_admission_exhausted(
    client, monkeypatch, settings, reset_internal_readiness_cache
):
    # Once the active runtime has passed its deep probe, frequent readiness
    # polls must not compete with real /embed/ work for model slots.
    settings.ROOT_URLCONF = "config.embedding_urls"
    from document_ai.embedding.admission import EmbeddingPriorityAdmission
    from document_ai.embedding import internal_views

    healthcheck_calls = []

    class FakeProvider:
        def healthcheck(self):
            healthcheck_calls.append("called")
            return {
                "backend": "bgem3_hybrid",
                "model_name": "BAAI/bge-m3",
                "dimension": 1024,
                "supports_sparse": True,
            }

    monkeypatch.setattr(internal_views, "get_embedding_provider", lambda **kwargs: FakeProvider())
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", _fake_active_runtime)

    assert client.get("/readyz").status_code == 200

    admission = EmbeddingPriorityAdmission(
        concurrency=1, queue_capacity=0, query_reserve=0
    )
    active_lease = admission.try_acquire()
    assert active_lease is not None
    monkeypatch.setattr(internal_views, "_EMBED_ADMISSION", admission)
    try:
        response = client.get("/readyz")
    finally:
        active_lease.release()

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert healthcheck_calls == ["called"]


@pytest.mark.django_db
def test_internal_readyz_does_not_start_probe_without_embedding_slot(
    client, monkeypatch, settings, reset_internal_readiness_cache
):
    settings.ROOT_URLCONF = "config.embedding_urls"
    from document_ai.embedding.admission import EmbeddingPriorityAdmission
    from document_ai.embedding import internal_views

    provider_calls = []
    monkeypatch.setattr(
        internal_views,
        "get_embedding_provider",
        lambda **kwargs: provider_calls.append("called"),
    )
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", _fake_active_runtime)

    admission = EmbeddingPriorityAdmission(
        concurrency=1, queue_capacity=0, query_reserve=0
    )
    active_lease = admission.try_acquire()
    assert active_lease is not None
    monkeypatch.setattr(internal_views, "_EMBED_ADMISSION", admission)
    try:
        response = client.get("/readyz")
    finally:
        active_lease.release()

    assert response.status_code == 503
    assert response.json()["reason"] == "embedding_busy"
    assert provider_calls == []
