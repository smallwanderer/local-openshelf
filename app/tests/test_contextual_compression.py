from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from config.enums import AIStatus, NodeType
from document_ai.models import ChunkSegmentEmbedding, DocumentChunk, DocumentParseResult
from document_ai.search.compression import EmbeddingContextualCompressor
from document_ai.tasks import _build_rag_context
from files.models import Node

pytestmark = pytest.mark.integration

User = get_user_model()


def _vec(first: float, second: float = 0.0) -> list[float]:
    return [first, second] + [0.0] * 1022


class ContextualCompressionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="compression@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.node = Node.objects.create(
            owner=self.user,
            name="policy.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )
        self.parse_result = DocumentParseResult.objects.create(
            node=self.node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
        )
        self.chunk = DocumentChunk.objects.create(
            parse_result=self.parse_result,
            chunk_index=0,
            text=(
                "회의가 열렸다. "
                "정부는 공급 확대와 할인지원을 통해 가격 안정을 추진한다. "
                "양파와 배추는 출하 조절과 소비 촉진을 병행한다. "
                "향후 수급 상황을 점검한다."
            ),
            status=AIStatus.COMPLETED,
        )

    @override_settings(CONTEXTUAL_COMPRESSION_ENABLED=True)
    def test_lazy_segment_embeddings_are_created_and_reused(self):
        compressor = EmbeddingContextualCompressor(
            enabled=True,
            window_size=2,
            top_segments=1,
            max_segments_per_chunk=8,
            min_score=0.1,
            dense_weight=1.0,
            sparse_weight=0.0,
        )
        query_embedding = SimpleNamespace(dense_vector=_vec(1.0), sparse_vector={})

        def fake_embed(text, *args, **kwargs):
            if "공급 확대" in text:
                return SimpleNamespace(dense_vector=_vec(1.0), sparse_vector={})
            return SimpleNamespace(dense_vector=_vec(0.0, 1.0), sparse_vector={})

        evidence = {
            "chunk_id": self.chunk.id,
            "_chunk": self.chunk,
            "text": self.chunk.text,
            "context_text": self.chunk.text,
        }

        with patch("document_ai.embedding.embeding_models.embed_document", side_effect=fake_embed) as embed_mock:
            compressed = compressor.compress_evidence(
                evidence=evidence,
                query_embedding=query_embedding,
                query_sparse={},
            )

        assert "공급 확대" in compressed["compressed_text"]
        assert compressed["compression"]["method"] == "embedding_lazy_segment"
        assert ChunkSegmentEmbedding.objects.filter(chunk=self.chunk, window_size=2).count() > 0
        first_call_count = embed_mock.call_count

        with patch("document_ai.embedding.embeding_models.embed_document", side_effect=fake_embed) as second_embed_mock:
            compressor.compress_evidence(
                evidence={**evidence, "compressed_text": ""},
                query_embedding=query_embedding,
                query_sparse={},
            )

        assert first_call_count > 0
        assert second_embed_mock.call_count == 0

    @override_settings(CONTEXTUAL_COMPRESSION_ENABLED=True)
    def test_overlapping_segments_are_deduplicated(self):
        self.chunk.text = "서론 문장이다. 핵심 공급 확대 정책이다. 핵심 할인 지원 정책이다. 마무리 문장이다."
        self.chunk.save(update_fields=["text"])
        compressor = EmbeddingContextualCompressor(
            enabled=True,
            window_size=2,
            top_segments=3,
            max_segments_per_chunk=8,
            min_score=0.1,
            dense_weight=1.0,
            sparse_weight=0.0,
        )
        query_embedding = SimpleNamespace(dense_vector=_vec(1.0), sparse_vector={})

        def fake_embed(text, *args, **kwargs):
            if "핵심" in text:
                return SimpleNamespace(dense_vector=_vec(1.0), sparse_vector={})
            return SimpleNamespace(dense_vector=_vec(0.0, 1.0), sparse_vector={})

        with patch("document_ai.embedding.embeding_models.embed_document", side_effect=fake_embed):
            compressed = compressor.compress_evidence(
                evidence={
                    "chunk_id": self.chunk.id,
                    "_chunk": self.chunk,
                    "text": self.chunk.text,
                    "context_text": self.chunk.text,
                },
                query_embedding=query_embedding,
                query_sparse={},
            )

        assert compressed["compressed_text"].count("핵심 공급 확대 정책이다.") == 1
        assert compressed["compressed_text"].count("핵심 할인 지원 정책이다.") == 1
        assert compressed["compression"]["unique_sentence_count"] >= 2

    @override_settings(CONTEXTUAL_COMPRESSION_ENABLED=True)
    def test_segments_below_threshold_are_not_used(self):
        compressor = EmbeddingContextualCompressor(
            enabled=True,
            window_size=2,
            top_segments=3,
            max_segments_per_chunk=8,
            min_score=0.5,
            dense_weight=1.0,
            sparse_weight=0.0,
        )
        query_embedding = SimpleNamespace(dense_vector=_vec(1.0), sparse_vector={})

        def fake_embed(text, *args, **kwargs):
            return SimpleNamespace(dense_vector=_vec(0.0, 1.0), sparse_vector={})

        with patch("document_ai.embedding.embeding_models.embed_document", side_effect=fake_embed):
            compressed = compressor.compress_evidence(
                evidence={
                    "chunk_id": self.chunk.id,
                    "_chunk": self.chunk,
                    "text": self.chunk.text,
                    "context_text": self.chunk.text,
                },
                query_embedding=query_embedding,
                query_sparse={},
            )

        assert "compressed_text" not in compressed
        assert compressed["compression"]["selected_segment_count"] == 0
        assert compressed["compression"]["skipped_reason"] == "below_threshold"


def test_rag_context_prefers_compressed_text():
    context_text, citations = _build_rag_context(
        [
            {
                "node_name": "policy.txt",
                "node_id": "node-1",
                "doc_score": 0.9,
                "evidences": [
                    {
                        "chunk_id": 1,
                        "text": "raw text",
                        "context_text": "wide context",
                        "compressed_text": "direct evidence",
                    }
                ],
            }
        ],
        evidence_limit=1,
    )

    assert "direct evidence" in context_text
    assert citations[0]["text"] == "direct evidence"


def test_compressor_combines_multiple_evidence_compressed_texts():
    compressor = EmbeddingContextualCompressor(enabled=True, max_chars=120)

    text, metadata = compressor.combine_evidence_texts(
        [
            {"compressed_text": "첫 번째 chunk의 직접 근거"},
            {"compressed_text": "두 번째 chunk의 보완 근거"},
        ]
    )

    assert "첫 번째 chunk" in text
    assert "두 번째 chunk" in text
    assert metadata["compressed_evidence_count"] == 2
