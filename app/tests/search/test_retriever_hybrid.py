import os
import sys
import types
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

pytestmark = pytest.mark.unit

from config.enums import AIStatus
from document_ai.models import DocumentChunk, DocumentParseResult
from document_ai.search import retriever as retriever_module
from document_ai.search.retriever import VectorRetriever, _sparse_dot_product
from files.models import Node


def _make_embedding_row(
    *,
    parse_result_id: int,
    uid: str,
    owner,
    chunk_id: int,
    chunk_index: int,
    node_name: str,
    dense_vector: list[float],
    sparse_vector: dict[str, float],
    ext: str = ".txt",
    model_version: str = "bgem3_hybrid",
    status: str = AIStatus.COMPLETED,
    trashed: bool = False,
):
    node = SimpleNamespace(uid=uid, name=node_name, owner=owner, trashed=trashed, ext=ext)
    parse_result = SimpleNamespace(
        id=parse_result_id,
        node=node,
        metadata={"file_ext": ext},
        embedding_generation_id="legacy-bge-m3",
    )
    chunk = SimpleNamespace(
        id=chunk_id,
        parse_result_id=parse_result_id,
        chunk_index=chunk_index,
        text=f"text-{chunk_id}",
        section_title="section",
        page_from=1,
        page_to=1,
        parse_result=parse_result,
        status=status,
    )
    return SimpleNamespace(
        chunk=chunk,
        generation_id="legacy-bge-m3",
        model_name="BAAI/bge-m3",
        model_version=model_version,
        status=status,
        vector=dense_vector,
        sparse_vector=sparse_vector,
        distance=None,
    )


class FakeQuerySet:
    def __init__(self, rows):
        self.rows = list(rows)

    def select_related(self, *args, **kwargs):
        return self

    def filter(self, **kwargs):
        filtered = self.rows

        for key, value in kwargs.items():
            if key == "model_name":
                filtered = [row for row in filtered if row.model_name == value]
            elif key == "generation_id":
                filtered = [
                    row for row in filtered if row.generation_id == value
                ]
            elif key == "status":
                filtered = [row for row in filtered if row.status == value]
            elif key == "chunk__status":
                filtered = [row for row in filtered if row.chunk.status == value]
            elif key in {
                "chunk__parse_result__embedding_generation_id",
                "chunk__parse_result__embedding_runtime_fingerprint",
            }:
                # Fake rows represent the active in-place contract.
                filtered = list(filtered)
            elif key == "model_version":
                filtered = [row for row in filtered if row.model_version == value]
            elif key == "chunk__parse_result__node__owner":
                filtered = [row for row in filtered if row.chunk.parse_result.node.owner == value]
            elif key == "chunk__parse_result__node__uid__in":
                allowed = {str(item) for item in value}
                filtered = [row for row in filtered if str(row.chunk.parse_result.node.uid) in allowed]
            elif key == "chunk__parse_result__node__trashed":
                filtered = [row for row in filtered if row.chunk.parse_result.node.trashed == value]
            elif key == "chunk__parse_result__node__ext":
                filtered = [row for row in filtered if row.chunk.parse_result.node.ext == value]
            elif key == "distance__lte":
                filtered = [row for row in filtered if row.distance <= value]
            else:
                raise AssertionError(f"Unexpected filter: {key}")

        return FakeQuerySet(filtered)

    def exclude(self, **kwargs):
        filtered = self.rows
        for key, value in kwargs.items():
            if key == "chunk__parse_result__node__ext":
                filtered = [row for row in filtered if row.chunk.parse_result.node.ext != value]
            else:
                raise AssertionError(f"Unexpected exclude: {key}")
        return FakeQuerySet(filtered)

    def exists(self):
        return bool(self.rows)

    def annotate(self, **kwargs):
        dense_query = kwargs["distance"]
        annotated = []
        for row in self.rows:
            dot = sum(a * b for a, b in zip(dense_query, row.vector))
            annotated_row = SimpleNamespace(**row.__dict__)
            annotated_row.distance = -dot
            annotated.append(annotated_row)
        return FakeQuerySet(annotated)

    def order_by(self, field_name):
        reverse = field_name.startswith("-")
        key = field_name.lstrip("-")
        return FakeQuerySet(sorted(self.rows, key=lambda row: getattr(row, key), reverse=reverse))

    def __getitem__(self, item):
        if isinstance(item, slice):
            return self.rows[item]
        return self.rows[item]

    def __iter__(self):
        return iter(self.rows)


class FakeManager:
    def __init__(self, rows):
        self.rows = rows

    def select_related(self, *args, **kwargs):
        return FakeQuerySet(self.rows)


class FakeDocumentChunkQuerySet:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *args, **kwargs):
        filtered = self.rows
        # Bulk context loading uses a positional Q expression. The fake rows
        # are deliberately small; the production code's document/index key
        # lookup still verifies that unrelated rows cannot enter a context.
        for key, value in kwargs.items():
            if key == "parse_result_id":
                filtered = [row for row in filtered if row.parse_result_id == value]
            elif key == "chunk_index__gte":
                filtered = [row for row in filtered if row.chunk_index >= value]
            elif key == "chunk_index__lte":
                filtered = [row for row in filtered if row.chunk_index <= value]
            else:
                raise AssertionError(f"Unexpected filter: {key}")
        return FakeDocumentChunkQuerySet(filtered)

    def order_by(self, *field_names):
        rows = list(self.rows)
        for field_name in reversed(field_names):
            reverse = field_name.startswith("-")
            key = field_name.lstrip("-")
            rows.sort(key=lambda row: getattr(row, key), reverse=reverse)
        return FakeDocumentChunkQuerySet(rows)

    def __iter__(self):
        return iter(self.rows)


class FakeDocumentChunkManager:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args, **kwargs):
        return FakeDocumentChunkQuerySet(self.rows).filter(*args, **kwargs)


def test_sparse_dot_product():
    score = _sparse_dot_product({"10": 0.6, "20": 0.8}, {"20": 0.5, "30": 1.0})
    assert score == 0.4


def test_retriever_defaults_to_inner_product():
    retriever = VectorRetriever()

    assert retriever.distance_strategy == "inner_product"
    assert retriever._distance_to_dense_score(-0.91) == 0.91
    assert retriever._distance_to_dense_score(0.91) == 0.91
    assert retriever._threshold_to_distance_filter(0.8) == -0.8


def test_retriever_hybrid_reranks_by_sparse_score(monkeypatch):
    owner = SimpleNamespace(email="owner@example.com")
    other_owner = SimpleNamespace(email="other@example.com")
    target_uid = str(uuid4())

    rows = [
        _make_embedding_row(
            parse_result_id=1,
            uid=target_uid,
            owner=owner,
            chunk_id=1,
            chunk_index=0,
            node_name="dense-wins-but-sparse-loses",
            dense_vector=[0.95, 0.05],
            sparse_vector={"100": 0.1},
        ),
        _make_embedding_row(
            parse_result_id=2,
            uid=str(uuid4()),
            owner=owner,
            chunk_id=2,
            chunk_index=0,
            node_name="hybrid-winner",
            dense_vector=[0.85, 0.15],
            sparse_vector={"999": 1.0},
        ),
        _make_embedding_row(
            parse_result_id=3,
            uid=str(uuid4()),
            owner=other_owner,
            chunk_id=3,
            chunk_index=0,
            node_name="other-user-doc",
            dense_vector=[0.99, 0.01],
            sparse_vector={"999": 1.0},
        ),
    ]

    monkeypatch.setattr(
        retriever_module.ChunkEmbedding,
        "objects",
        FakeManager(rows),
    )
    monkeypatch.setattr(
        retriever_module.DocumentChunk,
        "objects",
        FakeDocumentChunkManager([row.chunk for row in rows]),
    )
    monkeypatch.setattr(retriever_module.settings, "EMBEDDING_HYBRID_DENSE_WEIGHT", 0.2, raising=False)
    monkeypatch.setattr(retriever_module.settings, "EMBEDDING_HYBRID_SPARSE_WEIGHT", 0.8, raising=False)
    monkeypatch.setattr(retriever_module.settings, "EMBEDDING_HYBRID_CANDIDATE_MULTIPLIER", 10, raising=False)
    monkeypatch.setattr(
        VectorRetriever,
        "_get_distance_func",
        lambda self, vector: vector,
    )

    compressor_calls = []

    class FakeCompressor:
        def compress_evidences(self, *, evidences, query_embedding, query_sparse):
            compressor_calls.append(
                {
                    "evidence_count": len(evidences),
                    "query_sparse": query_sparse,
                }
            )
            for evidence in evidences:
                evidence["compressed_text"] = f"compressed:{evidence['text']}"
                evidence["compression"] = {"enabled": True, "method": "test"}
            return evidences

        def combine_evidence_texts(self, evidences):
            return "\n\n".join(evidence["compressed_text"] for evidence in evidences), {
                "enabled": True,
                "method": "embedding_lazy_segment_document",
            }

    monkeypatch.setattr(retriever_module, "EmbeddingContextualCompressor", FakeCompressor)

    def fake_embedder(*args, **kwargs):
        return SimpleNamespace(
            dense_vector=[1.0, 0.0],
            sparse_vector={"999": 1.0},
        )

    fake_embedding_module = types.SimpleNamespace(embed_query=fake_embedder)
    monkeypatch.setitem(
        sys.modules,
        "document_ai.embedding.embeding_models",
        fake_embedding_module,
    )

    retriever = VectorRetriever()
    performance_metrics = {}
    results = retriever.retrieve(
        query="hybrid query",
        top_k=2,
        user=owner,
        performance_metrics=performance_metrics,
    )

    assert len(results) == 2
    assert results[0]["node_name"] == "hybrid-winner"
    assert results[1]["node_name"] == "dense-wins-but-sparse-loses"
    assert all(item["node_name"] != "other-user-doc" for item in results)
    assert results[0]["score_details"]["distance_strategy"] == "inner_product"
    assert results[0]["score_details"]["pooling_method"] == "normalized_logsumexp"
    assert results[0]["evidences"][0]["dense_score"] > 0
    assert results[0]["evidences"][0]["hybrid_score"] > 0
    assert compressor_calls
    assert results[0]["compressed_text"].startswith("compressed:")
    assert results[0]["compression"]["method"] == "embedding_lazy_segment_document"
    assert results[0]["evidences"][0]["compressed_text"].startswith("compressed:")
    assert results[0]["evidences"][0]["compression"]["method"] == "test"
    assert performance_metrics["candidate_count"] == 2
    assert performance_metrics["result_count"] == 2
    assert performance_metrics["query_embedding_ms"] >= 0
    assert performance_metrics["vector_query_ms"] >= 0
    assert performance_metrics["contextual_compression_ms"] >= 0
    assert performance_metrics["retrieval_total_ms"] >= 0


def test_retriever_top_k_zero_returns_all_candidates(monkeypatch):
    owner = SimpleNamespace(email="owner@example.com")
    rows = [
        _make_embedding_row(
            parse_result_id=1,
            uid=str(uuid4()),
            owner=owner,
            chunk_id=1,
            chunk_index=0,
            node_name="doc-a",
            dense_vector=[0.95, 0.05],
            sparse_vector={"999": 1.0},
        ),
        _make_embedding_row(
            parse_result_id=2,
            uid=str(uuid4()),
            owner=owner,
            chunk_id=2,
            chunk_index=0,
            node_name="doc-b",
            dense_vector=[0.90, 0.10],
            sparse_vector={"999": 0.8},
        ),
        _make_embedding_row(
            parse_result_id=3,
            uid=str(uuid4()),
            owner=owner,
            chunk_id=3,
            chunk_index=0,
            node_name="doc-c",
            dense_vector=[0.85, 0.15],
            sparse_vector={"999": 0.6},
        ),
    ]

    monkeypatch.setattr(retriever_module.ChunkEmbedding, "objects", FakeManager(rows))
    monkeypatch.setattr(
        retriever_module.DocumentChunk,
        "objects",
        FakeDocumentChunkManager([row.chunk for row in rows]),
    )
    monkeypatch.setattr(VectorRetriever, "_get_distance_func", lambda self, vector: vector)

    fake_embedding_module = types.SimpleNamespace(
        embed_query=lambda *args, **kwargs: SimpleNamespace(
            dense_vector=[1.0, 0.0],
            sparse_vector={"999": 1.0},
        )
    )
    monkeypatch.setitem(
        sys.modules,
        "document_ai.embedding.embeding_models",
        fake_embedding_module,
    )

    retriever = VectorRetriever()
    results = retriever.retrieve(query="all candidates", top_k=0, user=owner)

    assert [result["node_name"] for result in results] == ["doc-a", "doc-b", "doc-c"]


def test_retriever_excludes_trashed_nodes(monkeypatch):
    owner = SimpleNamespace(email="owner@example.com")
    rows = [
        _make_embedding_row(
            parse_result_id=1,
            uid=str(uuid4()),
            owner=owner,
            chunk_id=1,
            chunk_index=0,
            node_name="visible-doc",
            dense_vector=[0.9, 0.1],
            sparse_vector={"999": 0.8},
            trashed=False,
        ),
        _make_embedding_row(
            parse_result_id=2,
            uid=str(uuid4()),
            owner=owner,
            chunk_id=2,
            chunk_index=0,
            node_name="trashed-doc",
            dense_vector=[0.99, 0.01],
            sparse_vector={"999": 1.0},
            trashed=True,
        ),
    ]

    monkeypatch.setattr(retriever_module.ChunkEmbedding, "objects", FakeManager(rows))
    monkeypatch.setattr(
        retriever_module.DocumentChunk,
        "objects",
        FakeDocumentChunkManager([row.chunk for row in rows]),
    )
    monkeypatch.setattr(VectorRetriever, "_get_distance_func", lambda self, vector: vector)

    def fake_embedder(*args, **kwargs):
        return SimpleNamespace(
            dense_vector=[1.0, 0.0],
            sparse_vector={"999": 1.0},
        )

    fake_embedding_module = types.SimpleNamespace(embed_query=fake_embedder)
    monkeypatch.setitem(sys.modules, "document_ai.embedding.embeding_models", fake_embedding_module)

    retriever = VectorRetriever()
    results = retriever.retrieve(query="query", top_k=5, user=owner)

    assert [item["node_name"] for item in results] == ["visible-doc"]


def test_retriever_applies_querydsl_orm_filters(monkeypatch):
    owner = SimpleNamespace(email="owner@example.com")
    rows = [
        _make_embedding_row(
            parse_result_id=1,
            uid=str(uuid4()),
            owner=owner,
            chunk_id=1,
            chunk_index=0,
            node_name="pdf-doc",
            ext="pdf",
            dense_vector=[0.9, 0.1],
            sparse_vector={"999": 1.0},
        ),
        _make_embedding_row(
            parse_result_id=2,
            uid=str(uuid4()),
            owner=owner,
            chunk_id=2,
            chunk_index=0,
            node_name="txt-doc",
            ext="txt",
            dense_vector=[0.99, 0.01],
            sparse_vector={"999": 1.0},
        ),
    ]

    monkeypatch.setattr(retriever_module.ChunkEmbedding, "objects", FakeManager(rows))
    monkeypatch.setattr(
        retriever_module.DocumentChunk,
        "objects",
        FakeDocumentChunkManager([row.chunk for row in rows]),
    )
    monkeypatch.setattr(VectorRetriever, "_get_distance_func", lambda self, vector: vector)

    fake_embedding_module = types.SimpleNamespace(
        embed_query=lambda *args, **kwargs: SimpleNamespace(
            dense_vector=[1.0, 0.0],
            sparse_vector={"999": 1.0},
        )
    )
    monkeypatch.setitem(sys.modules, "document_ai.embedding.embeding_models", fake_embedding_module)

    retriever = VectorRetriever()
    results = retriever.retrieve(
        query="contract",
        top_k=5,
        user=owner,
        filter_kwargs={"ext": "pdf"},
    )

    assert [item["node_name"] for item in results] == ["pdf-doc"]


def test_retriever_returns_empty_when_backend_missing(monkeypatch):
    monkeypatch.setattr(
        retriever_module.ChunkEmbedding,
        "objects",
        FakeManager([]),
    )

    retriever = VectorRetriever()
    results = retriever.retrieve(query="anything")

    assert results == []


def test_retriever_normalizes_evidence_text(monkeypatch):
    owner = SimpleNamespace(email="owner@example.com")
    rows = [
        _make_embedding_row(
            parse_result_id=1,
            uid=str(uuid4()),
            owner=owner,
            chunk_id=1,
            chunk_index=1,
            node_name="doc",
            dense_vector=[1.0, 0.0],
            sparse_vector={"999": 1.0},
        ),
    ]
    rows[0].chunk.text = "  first\t\tline  \n\n\n second   line \u00a0  "

    monkeypatch.setattr(
        retriever_module.ChunkEmbedding,
        "objects",
        FakeManager(rows),
    )
    context_rows = [
        SimpleNamespace(
            parse_result_id=1,
            chunk_index=0,
            text="  before   block ",
        ),
        SimpleNamespace(
            parse_result_id=1,
            chunk_index=1,
            text="  first\t\tline  \n\n\n second   line \u00a0  ",
        ),
        SimpleNamespace(
            parse_result_id=1,
            chunk_index=2,
            text=" after\t\tblock ",
        ),
    ]
    monkeypatch.setattr(
        retriever_module.DocumentChunk,
        "objects",
        FakeDocumentChunkManager(context_rows),
    )
    monkeypatch.setattr(
        VectorRetriever,
        "_get_distance_func",
        lambda self, vector: vector,
    )

    def fake_embedder(*args, **kwargs):
        return SimpleNamespace(
            dense_vector=[1.0, 0.0],
            sparse_vector={"999": 1.0},
        )

    fake_embedding_module = types.SimpleNamespace(embed_query=fake_embedder)
    monkeypatch.setitem(
        sys.modules,
        "document_ai.embedding.embeding_models",
        fake_embedding_module,
    )

    retriever = VectorRetriever()
    results = retriever.retrieve(query="query", top_k=1, user=owner)

    assert results[0]["evidences"][0]["text"] == "first line\n\nsecond line"
    assert (
        results[0]["evidences"][0]["context_text"]
        == "before block\n\nfirst line\n\nsecond line\n\nafter block"
    )


class BulkEvidenceContextQueryTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            email="bulk-context@example.com",
            password="password",
        )
        workspace = user.workspace_memberships.get(workspace__kind="personal").workspace
        self.parse_results = []
        for document_number in range(2):
            node = Node.objects.create(
                owner=user,
                workspace=workspace,
                name=f"context-{document_number}.txt",
                ext=".txt",
                node_type="file",
            )
            parse_result = DocumentParseResult.objects.create(
                node=node,
                status=AIStatus.COMPLETED,
                chunk_count=5,
            )
            DocumentChunk.objects.bulk_create(
                [
                    DocumentChunk(
                        parse_result=parse_result,
                        chunk_index=index,
                        text=f"doc-{document_number}-chunk-{index}",
                    )
                    for index in range(5)
                ]
            )
            self.parse_results.append(parse_result)

        self.retriever = object.__new__(VectorRetriever)
        self.retriever.evidence_context_window = 1

    def test_context_query_count_is_constant_as_evidence_count_grows(self):
        first_document_chunks = list(
            DocumentChunk.objects.filter(parse_result=self.parse_results[0]).order_by(
                "chunk_index"
            )
        )
        second_document_chunks = list(
            DocumentChunk.objects.filter(parse_result=self.parse_results[1]).order_by(
                "chunk_index"
            )
        )

        with self.assertNumQueries(1):
            one_evidence = self.retriever._load_context_chunks(
                [first_document_chunks[2]]
            )
        with self.assertNumQueries(1):
            many_evidences = self.retriever._load_context_chunks(
                [
                    first_document_chunks[1],
                    first_document_chunks[3],
                    second_document_chunks[1],
                    second_document_chunks[3],
                ]
            )

        assert self.retriever._build_context_text(
            first_document_chunks[2], one_evidence
        ) == "doc-0-chunk-1\n\ndoc-0-chunk-2\n\ndoc-0-chunk-3"
        assert self.retriever._build_context_text(
            first_document_chunks[1], many_evidences
        ) == "doc-0-chunk-0\n\ndoc-0-chunk-1\n\ndoc-0-chunk-2"
        assert self.retriever._build_context_text(
            second_document_chunks[1], many_evidences
        ) == "doc-1-chunk-0\n\ndoc-1-chunk-1\n\ndoc-1-chunk-2"
