import os
import sys
import types
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

pytestmark = pytest.mark.unit

from config.enums import AIStatus
from document_ai.search import retriever as retriever_module
from document_ai.search.retriever import VectorRetriever, _sparse_dot_product


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
    model_version: str = "bgem3_hybrid",
    status: str = AIStatus.COMPLETED,
    trashed: bool = False,
):
    node = SimpleNamespace(uid=uid, name=node_name, owner=owner, trashed=trashed)
    parse_result = SimpleNamespace(id=parse_result_id, node=node, metadata={"file_ext": ".txt"})
    chunk = SimpleNamespace(
        id=chunk_id,
        parse_result_id=parse_result_id,
        chunk_index=chunk_index,
        text=f"text-{chunk_id}",
        section_title="section",
        page_from=1,
        page_to=1,
        parse_result=parse_result,
    )
    return SimpleNamespace(
        chunk=chunk,
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
            elif key == "status":
                filtered = [row for row in filtered if row.status == value]
            elif key == "model_version":
                filtered = [row for row in filtered if row.model_version == value]
            elif key == "chunk__parse_result__node__owner":
                filtered = [row for row in filtered if row.chunk.parse_result.node.owner == value]
            elif key == "chunk__parse_result__node__uid__in":
                allowed = {str(item) for item in value}
                filtered = [row for row in filtered if str(row.chunk.parse_result.node.uid) in allowed]
            elif key == "chunk__parse_result__node__trashed":
                filtered = [row for row in filtered if row.chunk.parse_result.node.trashed == value]
            elif key == "distance__lte":
                filtered = [row for row in filtered if row.distance <= value]
            else:
                raise AssertionError(f"Unexpected filter: {key}")

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

    def filter(self, **kwargs):
        filtered = self.rows
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

    def order_by(self, field_name):
        reverse = field_name.startswith("-")
        key = field_name.lstrip("-")
        return FakeDocumentChunkQuerySet(
            sorted(self.rows, key=lambda row: getattr(row, key), reverse=reverse)
        )

    def __iter__(self):
        return iter(self.rows)


class FakeDocumentChunkManager:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, **kwargs):
        return FakeDocumentChunkQuerySet(self.rows).filter(**kwargs)


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
    monkeypatch.setattr(
        retriever_module,
        "settings",
        SimpleNamespace(
            EMBEDDING_HYBRID_DENSE_WEIGHT=0.2,
            EMBEDDING_HYBRID_SPARSE_WEIGHT=0.8,
            EMBEDDING_HYBRID_CANDIDATE_MULTIPLIER=10,
        ),
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

    fake_embedding_module = types.SimpleNamespace(bge_m3_embedder=fake_embedder)
    monkeypatch.setitem(
        sys.modules,
        "document_ai.embedding.embeding_models",
        fake_embedding_module,
    )

    retriever = VectorRetriever()
    results = retriever.retrieve(
        query="hybrid query",
        top_k=2,
        user=owner,
    )

    assert len(results) == 2
    assert results[0]["node_name"] == "hybrid-winner"
    assert results[1]["node_name"] == "dense-wins-but-sparse-loses"
    assert all(item["node_name"] != "other-user-doc" for item in results)
    assert results[0]["score_details"]["distance_strategy"] == "inner_product"
    assert results[0]["score_details"]["pooling_method"] == "normalized_logsumexp"
    assert results[0]["evidences"][0]["dense_score"] > 0
    assert results[0]["evidences"][0]["hybrid_score"] > 0


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
        bge_m3_embedder=lambda *args, **kwargs: SimpleNamespace(
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

    fake_embedding_module = types.SimpleNamespace(bge_m3_embedder=fake_embedder)
    monkeypatch.setitem(sys.modules, "document_ai.embedding.embeding_models", fake_embedding_module)

    retriever = VectorRetriever()
    results = retriever.retrieve(query="query", top_k=5, user=owner)

    assert [item["node_name"] for item in results] == ["visible-doc"]


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

    fake_embedding_module = types.SimpleNamespace(bge_m3_embedder=fake_embedder)
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
