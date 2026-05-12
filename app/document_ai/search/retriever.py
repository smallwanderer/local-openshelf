import logging
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional

from django.conf import settings
from pgvector.django import CosineDistance, L2Distance, MaxInnerProduct

from config.enums import AIStatus
from document_ai.models import ChunkEmbedding, DocumentChunk
from document_ai.parsers.config import get_embedding_backend
from document_ai.parsers.text_utils import normalize_extracted_text

logger = logging.getLogger(__name__)


def _sparse_dot_product(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0

    if len(left) > len(right):
        left, right = right, left

    score = 0.0
    for key, value in left.items():
        score += value * right.get(key, 0.0)
    return score

def _prune_sparse_vector(vec: dict[str, float], top_n: int) -> dict[str, float]:
    if not vec or top_n <=0 or len(vec) <= top_n:
        return vec
    sorted_items = sorted(vec.items(), key=lambda x: x[1], reverse=True)
    top_items = sorted_items[:top_n]
    return dict(top_items)

def _softmax_pool(scores: list[float], tau: float = 5.0) -> float:
    """
    Stable log-sum-exp pooling입니다.
    tau가 클 수록 maximum_pooling에 가까워집니다.
    """
    positive_scores = [max(s, 0.0) for s in scores]
    if not positive_scores:
        return 0.0

    scaled = [tau * s for s in positive_scores]
    m = max(scaled)

    return (m + math.log(sum(math.exp(v - m) for v in scaled))) / tau
    

class VectorRetriever:
    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        distance_strategy: str = "cosine",
        embedding_backend: str | None = None,
    ):
        self.model_name = model_name
        self.distance_strategy = distance_strategy
        self.embedding_backend = embedding_backend or get_embedding_backend()

        self.hybrid_dense_weight = float(
            getattr(settings, "EMBEDDING_HYBRID_DENSE_WEIGHT", 0.5)
        )
        self.hybrid_sparse_weight = float(
            getattr(settings, "EMBEDDING_HYBRID_SPARSE_WEIGHT", 0.5)
        )

        # dense 1차 후보 개수 = top_k * candidate_multiplier
        self.candidate_multiplier = int(
            getattr(settings, "EMBEDDING_HYBRID_CANDIDATE_MULTIPLIER", 12)
        )

        # 문서당 dense 후보 최대 허용 개수
        self.per_node_candidate_cap = int(
            getattr(settings, "EMBEDDING_PER_NODE_CANDIDATE_CAP", 4)
        )

        # query sparse pruning 상위 개수
        self.query_sparse_top_n = int(
            getattr(settings, "EMBEDDING_QUERY_SPARSE_TOP_N", 32)
        )

        # evidence로 보여줄 청크 수
        self.evidence_top_k = int(
            getattr(settings, "EMBEDDING_EVIDENCE_TOP_K", 3)
        )

        # softmax pooling에 사용할 청크 수
        self.pool_top_k = int(
            getattr(settings, "EMBEDDING_DOC_POOL_TOP_K", 5)
        )

        # softmax pooling temperature
        self.pool_tau = float(
            getattr(settings, "EMBEDDING_DOC_POOL_TAU", 5.0)
        )

        # 문서 길이 패널티 강도
        self.doc_length_penalty_alpha = float(
            getattr(settings, "EMBEDDING_DOC_LENGTH_PENALTY_ALPHA", 0.10)
        )
        self.evidence_context_window = int(
            getattr(settings, "EMBEDDING_EVIDENCE_CONTEXT_WINDOW", 1)
        )

    def _get_distance_func(self, vector):
        if self.distance_strategy == "cosine":
            return CosineDistance("vector", vector)
        if self.distance_strategy == "l2":
            return L2Distance("vector", vector)
        if self.distance_strategy == "inner_product":
            return MaxInnerProduct("vector", vector)
        raise ValueError(f"Unknown distance strategy: {self.distance_strategy}")

    def _distance_to_dense_score(self, distance: float | None) -> float:
        if distance is None or math.isnan(distance):
            return 0.0
        if self.distance_strategy == "cosine":
            return max(0.0, 1.0 - distance)
        if self.distance_strategy == "l2":
            return 1.0 / (1.0 + max(distance, 0.0))
        if self.distance_strategy == "inner_product":
            return max(0.0, -distance)
        raise ValueError(f"Unknown distance strategy: {self.distance_strategy}")

    def _build_context_text(self, chunk) -> str:
        if self.evidence_context_window <= 0:
            return normalize_extracted_text(chunk.text or "")

        context_chunks = (
            DocumentChunk.objects.filter(
                parse_result_id=chunk.parse_result_id,
                chunk_index__gte=max(chunk.chunk_index - self.evidence_context_window, 0),
                chunk_index__lte=chunk.chunk_index + self.evidence_context_window,
            )
            .order_by("chunk_index")
        )

        context_parts = [
            normalize_extracted_text(context_chunk.text or "")
            for context_chunk in context_chunks
            if normalize_extracted_text(context_chunk.text or "")
        ]

        if not context_parts:
            return normalize_extracted_text(chunk.text or "")

        return "\n\n".join(context_parts)

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        threshold: Optional[float] = None,
        node_ids: Optional[List[int]] = None,
        user=None,
    ) -> List[Dict[str, Any]]:
        qs = ChunkEmbedding.objects.select_related(
            "chunk",
            "chunk__parse_result",
            "chunk__parse_result__node",
        ).filter(
            model_name=self.model_name,
            status=AIStatus.COMPLETED,
            chunk__parse_result__node__trashed=False,
        )

        if user is not None:
            qs = qs.filter(chunk__parse_result__node__owner=user)

        if node_ids is not None:
            qs = qs.filter(chunk__parse_result__node__uid__in=node_ids)

        query_backend = self.embedding_backend
        if not qs.filter(model_version=query_backend).exists():
            logger.warning("No embeddings found for backend=%s.", query_backend)
            return []

        try:
            from document_ai.embedding.embeding_models import bge_m3_embedder

            query_embedding = bge_m3_embedder(
                query,
                model_name=self.model_name,
                backend=query_backend,
            )
        except Exception as exc:
            logger.error("Failed to embed query: %s", exc)
            raise

        pruned_query_sparse = _prune_sparse_vector(
            query_embedding.sparse_vector or {},
            self.query_sparse_top_n,
        )

        distance_func = self._get_distance_func(query_embedding.dense_vector)

        qs = qs.filter(model_version=query_backend).annotate(distance=distance_func)

        if threshold is not None:
            qs = qs.filter(distance__lte=threshold)

        candidate_limit = max(top_k * self.candidate_multiplier, top_k)
        dense_candidates = list(qs.order_by("distance")[:candidate_limit])

        if not dense_candidates:
            return []

        node_groups = defaultdict(list)
        node_candidate_counts = defaultdict(int)

        for emb in dense_candidates:
            uid = emb.chunk.parse_result.node.uid

            # 문서당 dense 후보 과다 유입 방지
            if node_candidate_counts[uid] >= self.per_node_candidate_cap:
                continue

            dense_score = self._distance_to_dense_score(emb.distance)
            sparse_score = _sparse_dot_product(
                pruned_query_sparse,
                emb.sparse_vector or {},
            )
            hybrid_score = (
                self.hybrid_dense_weight * dense_score
                + self.hybrid_sparse_weight * sparse_score
            )

            node_groups[uid].append(
                {
                    "embedding": emb,
                    "dense_score": dense_score,
                    "sparse_score": sparse_score,
                    "hybrid_score": hybrid_score,
                    "distance": emb.distance,
                }
            )
            node_candidate_counts[uid] += 1

        if not node_groups:
            return []

        retrieved_data = []
        for uid, hits in node_groups.items():
            if not hits:
                continue

            hits.sort(key=lambda item: item["hybrid_score"], reverse=True)

            evidence_hits = hits[: self.evidence_top_k]
            pool_hits = hits[: self.pool_top_k]

            softmax_score = _softmax_pool(
                [item["hybrid_score"] for item in pool_hits],
                tau=self.pool_tau,
            )

            # hits 전체 길이 기준으로 약한 길이 패널티
            length_penalty = 1.0 + self.doc_length_penalty_alpha * math.log1p(len(hits))
            doc_score = softmax_score / length_penalty

            evidence_hits.sort(key=lambda item: item["embedding"].chunk.id)
            evidences = []

            for item in evidence_hits:
                chunk = item["embedding"].chunk
                evidence = {
                    "chunk_id": chunk.id,
                    "text": normalize_extracted_text(chunk.text or ""),
                    "context_text": self._build_context_text(chunk),
                    "section": chunk.section_title or "",
                    "pages": (
                        f"{chunk.page_from}-{chunk.page_to}"
                        if chunk.page_to
                        else str(chunk.page_from)
                    ),
                    "distance": item["distance"],
                    "dense_score": item["dense_score"],
                    "sparse_score": item["sparse_score"],
                    "hybrid_score": item["hybrid_score"],
                }
                evidences.append(evidence)

            meta_chunk = evidence_hits[0]["embedding"].chunk.parse_result

            retrieved_data.append(
                {
                    "node_id": str(uid),
                    "node_name": meta_chunk.node.name,
                    "file_ext": meta_chunk.metadata.get("file_ext", ""),
                    "doc_score": doc_score,
                    "evidences": evidences,
                }
            )

        retrieved_data.sort(key=lambda x: x["doc_score"], reverse=True)
        return retrieved_data[:top_k]
