from __future__ import annotations

import math
import pytest

from document_ai.embedding.sparse.lexical import (
    LexicalSparseEncoder,
    get_lexical_sparse_encoder,
)
from document_ai.search.retriever import _sparse_dot_product


@pytest.fixture
def encoder():
    return LexicalSparseEncoder()


def test_tokenize_alphanumeric_and_identifiers(encoder):
    text = "Deploying harrier-270m version v2.4.1 on port 8080 with python3.12"
    tokens = encoder.tokenize(text)

    # Identifiers, versions, codes preserved
    assert "harrier-270m" in tokens
    assert "v2.4.1" in tokens
    assert "8080" in tokens
    assert "python3.12" in tokens
    assert "deploying" in tokens
    assert "version" in tokens
    assert "port" in tokens

    # Stopwords like 'on', 'with' excluded
    assert "on" not in tokens
    assert "with" not in tokens


def test_tokenize_korean_words_and_bigrams(encoder):
    text = "도토리 검색엔진에서 임베딩 모델을 실행합니다."
    tokens = encoder.tokenize(text)

    # Full Hangul words
    assert "도토리" in tokens
    assert "검색엔진에서" in tokens
    assert "임베딩" in tokens
    assert "모델을" in tokens
    assert "실행합니다" in tokens

    # Bigrams for morphological resilience
    assert "도토" in tokens
    assert "토리" in tokens
    assert "임베" in tokens
    assert "베딩" in tokens


def test_encode_empty_text(encoder):
    assert encoder.encode("") == {}
    assert encoder.encode("   \n\t  ") == {}
    assert encoder.encode("... --- ???") == {}


def test_encode_l2_normalized(encoder):
    text = "Harrier is a high-speed 640d embedding model designed for fast vector search."
    vec = encoder.encode(text)

    assert isinstance(vec, dict)
    assert len(vec) > 0
    assert "harrier" in vec
    assert "640d" in vec

    # Verify L2 norm is approximately 1.0
    norm_sq = sum(w * w for w in vec.values())
    assert abs(math.sqrt(norm_sq) - 1.0) < 0.05


def test_exact_keyword_matching_dot_product(encoder):
    doc1 = "The release candidate includes bugfix v2.4.1 for memory leaks."
    doc2 = "Unrelated document discussing ancient history and philosophy."
    query = "bugfix v2.4.1"

    doc1_vec = encoder.encode(doc1, is_query=False)
    doc2_vec = encoder.encode(doc2, is_query=False)
    query_vec = encoder.encode(query, is_query=True)

    score1 = _sparse_dot_product(query_vec, doc1_vec)
    score2 = _sparse_dot_product(query_vec, doc2_vec)

    assert score1 > 0.3, f"Expected strong match for exact keywords, got {score1}"
    assert score2 == 0.0, f"Expected 0 match for unrelated document, got {score2}"


def test_korean_inflected_keyword_matching(encoder):
    # Query is root noun, document has noun with postposition (조사)
    doc = "우리는 도토리에서 검색엔진을 활용합니다."
    query = "도토리 검색엔진"

    doc_vec = encoder.encode(doc, is_query=False)
    query_vec = encoder.encode(query, is_query=True)

    score = _sparse_dot_product(query_vec, doc_vec)
    assert score > 0.0, f"Expected partial match via bigrams, got {score}"


def test_embed_models_enriches_dense_provider(monkeypatch):
    from document_ai.embedding import embeding_models
    from document_ai.embedding.providers.base import EmbeddingProvider, EmbeddingProviderSpec, EmbeddingResult

    class FakeDenseProvider(EmbeddingProvider):
        backend = "fake_dense"

        def __init__(self, **kwargs):
            self.spec = EmbeddingProviderSpec(
                backend="fake_dense",
                model_name="fake-dense-model",
                model_revision="",
                dimension=640,
                supports_sparse=False,
            )

        def embed_query(self, text: str, max_length=None):
            return EmbeddingResult(
                dense_vector=[0.1] * 640,
                sparse_vector={},  # provider produces no sparse vector
                model_name="fake-dense-model",
                backend="fake_dense",
                dimension=640,
            )

        def embed_document(self, text: str, max_length=None):
            return EmbeddingResult(
                dense_vector=[0.2] * 640,
                sparse_vector={},  # provider produces no sparse vector
                model_name="fake-dense-model",
                backend="fake_dense",
                dimension=640,
            )

        def embed_documents(self, texts: list[str], max_length=None):
            return [self.embed_document(t, max_length=max_length) for t in texts]

        def healthcheck(self):
            return {"status": "ok"}

    fake = FakeDenseProvider()
    monkeypatch.setattr(embeding_models, "get_embedding_provider", lambda **kw: fake)

    doc_res = embeding_models.embed_document("Notice for harrier-270m v1.0", backend="fake_dense")
    assert doc_res.sparse_vector, "Expected sparse_vector to be supplemented by LexicalSparseEncoder"
    assert "harrier-270m" in doc_res.sparse_vector

    query_res = embeding_models.embed_query("harrier-270m", backend="fake_dense")
    assert query_res.sparse_vector, "Expected query sparse_vector to be supplemented"
    assert "harrier-270m" in query_res.sparse_vector

    batch_res = embeding_models.embed_documents(["doc one with token123", "doc two with token456"], backend="fake_dense")
    assert len(batch_res) == 2
    assert "token123" in batch_res[0].sparse_vector
    assert "token456" in batch_res[1].sparse_vector


def test_josa_stripping_fallback(encoder):
    tokens = encoder.tokenize("도토리에서 검색엔진을 활용하여 임베딩으로 문서를 찾습니다.")
    # Full words with josa stripped should produce root nouns
    assert "도토리" in tokens
    assert "검색엔진" in tokens
    assert "임베딩" in tokens
    assert "문서" in tokens


def test_kiwi_integration_when_available():
    class FakeKiwiToken:
        def __init__(self, form, tag):
            self.form = form
            self.tag = tag

    class FakeKiwi:
        def tokenize(self, text):
            # Simulate Kiwi analyzing "도토리에서 검색을"
            return [
                FakeKiwiToken("도토리", "NNP"),
                FakeKiwiToken("에서", "JKB"),
                FakeKiwiToken("검색", "NNG"),
                FakeKiwiToken("을", "JKO"),
            ]

    enc = LexicalSparseEncoder()
    enc._kiwi = FakeKiwi()

    tokens = enc.tokenize("도토리에서 검색을")
    assert "도토리" in tokens
    assert "검색" in tokens
    # Particles should be completely excluded by Kiwi
    assert "에서" not in tokens
    assert "을" not in tokens

