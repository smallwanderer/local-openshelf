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


def _l2_norm(vector) -> float:
    if vector is None:
        return 0.0
    values = list(vector)
    if not values:
        return 0.0
    return math.sqrt(sum(float(value) * float(value) for value in values))


def _is_finite_number(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _prune_sparse_vector(vec: dict[str, float], top_n: int) -> dict[str, float]:
    """
    BGE-M3의 Sparse 벡터를 상위 n개만 남기는 함수
    """
    if not vec or top_n <=0 or len(vec) <= top_n:
        return vec
    sorted_items = sorted(vec.items(), key=lambda x: x[1], reverse=True)
    top_items = sorted_items[:top_n]
    return dict(top_items)

def _normalized_softmax_pool(scores: list[float], tau: float = 1.0) -> float:
    """
    정규화된 Softmax Pooling: 여러 청크의 최종 점수를 계산합니다.
    tau가 클 수록 maximum_pooling에 가까워집니다.
    """
    positive_scores = [max(s, 0.0) for s in scores]
    if not positive_scores:
        return 0.0

    scaled = [tau * s for s in positive_scores]
    m = max(scaled)
    logsumexp = m + math.log(sum(math.exp(v - m) for v in scaled))

    return (logsumexp - math.log(len(positive_scores))) / tau
    

class VectorRetriever:
    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        distance_strategy: str | None = None,
        embedding_backend: str | None = None,
    ):
        self.model_name = model_name
        self.distance_strategy = distance_strategy or getattr(
            settings,
            "EMBEDDING_DISTANCE_STRATEGY",
            "inner_product",
        )
        self.embedding_backend = embedding_backend or get_embedding_backend()
        self.pooling_method = getattr(
            settings,
            "EMBEDDING_DOC_POOLING_METHOD",
            "normalized_logsumexp",
        )

        self.hybrid_dense_weight = float(
            getattr(settings, "EMBEDDING_HYBRID_DENSE_WEIGHT", 0.3)
        )
        self.hybrid_sparse_weight = float(
            getattr(settings, "EMBEDDING_HYBRID_SPARSE_WEIGHT", 0.7)
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

    def _apply_tuning_params(self, params: Dict[str, Any]):
        """
        샌드박스 튜닝을 위해 인스턴스 파라미터를 동적으로 덮어씁니다.
        """
        if not params:
            return

        self.hybrid_dense_weight = params.get("dense_weight", self.hybrid_dense_weight)
        self.hybrid_sparse_weight = params.get("sparse_weight", self.hybrid_sparse_weight)
        self.candidate_multiplier = params.get("candidate_multiplier", self.candidate_multiplier)
        self.per_node_candidate_cap = params.get("per_node_candidate_cap", self.per_node_candidate_cap)
        self.query_sparse_top_n = params.get("query_sparse_top_n", self.query_sparse_top_n)
        self.evidence_top_k = params.get("evidence_top_k", self.evidence_top_k)
        self.pool_top_k = params.get("pool_top_k", self.pool_top_k)
        self.pool_tau = params.get("pool_tau", self.pool_tau)
        self.doc_length_penalty_alpha = params.get("doc_length_penalty_alpha", self.doc_length_penalty_alpha)
        self.evidence_context_window = params.get("evidence_context_window", self.evidence_context_window)

    def _pool_scores(self, scores: list[float]) -> float:
        if self.pooling_method in {"normalized_logsumexp", "normalized_softmax"}:
            return _normalized_softmax_pool(scores, tau=self.pool_tau)
        if self.pooling_method == "max":
            return max((max(score, 0.0) for score in scores), default=0.0)
        raise ValueError(f"Unknown document pooling method: {self.pooling_method}")

    def _get_distance_func(self, vector):
        """
        Distance Strategy에 따른 Vector distance 계산 (ex: cosine, l2, inner_product)
        """
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
            # pgvector MaxInnerProduct returns negative inner product so lower
            # distance still sorts better. Some tests/debug adapters provide
            # the raw positive dot product, so accept both shapes explicitly.
            return max(0.0, -distance if distance < 0 else distance)
        raise ValueError(f"Unknown distance strategy: {self.distance_strategy}")

    def _threshold_to_distance_filter(self, threshold: float) -> float:
        if self.distance_strategy == "inner_product" and threshold >= 0:
            return -threshold
        return threshold

    def _score_checks(
        self,
        *,
        query_dense_norm: float,
        candidate_dense_norm: float,
        distance: float | None,
        dense_score: float,
        sparse_score: float,
        hybrid_score: float,
    ) -> list[str]:
        checks = []
        if self.distance_strategy == "inner_product":
            if query_dense_norm and abs(query_dense_norm - 1.0) > 0.05:
                checks.append(f"query_dense_norm_not_unit:{query_dense_norm:.4f}")
            if candidate_dense_norm and abs(candidate_dense_norm - 1.0) > 0.05:
                checks.append(f"candidate_dense_norm_not_unit:{candidate_dense_norm:.4f}")
            if distance is not None and distance > 0:
                checks.append("inner_product_distance_is_positive")

        for name, value in (
            ("dense_score", dense_score),
            ("sparse_score", sparse_score),
            ("hybrid_score", hybrid_score),
        ):
            if not _is_finite_number(value):
                checks.append(f"{name}_not_finite")

        if self.hybrid_dense_weight < 0 or self.hybrid_sparse_weight < 0:
            checks.append("negative_hybrid_weight")

        return checks

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
        tuning_params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        # 튜닝 파라미터가 있으면 적용
        if tuning_params:
            self._apply_tuning_params(tuning_params)
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
        query_dense_norm = _l2_norm(query_embedding.dense_vector)
        query_sparse_norm = _l2_norm(pruned_query_sparse.values())

        distance_func = self._get_distance_func(query_embedding.dense_vector)

        qs = qs.filter(model_version=query_backend).annotate(distance=distance_func)

        distance_threshold = None
        if threshold is not None:
            distance_threshold = self._threshold_to_distance_filter(threshold)
            qs = qs.filter(distance__lte=distance_threshold)

        ordered_qs = qs.order_by("distance")
        # dense_candidates: dense retrieval을 통한 top-k 후보군
        if top_k == 0:
            dense_candidates = list(ordered_qs)
        else:
            # candidate_multiplier: dense 후보군 수 배수
            # per_node_candidate_cap: 문서당 dense 후보 최대 수
            candidate_limit = max(top_k * self.candidate_multiplier, top_k)
            dense_candidates = list(ordered_qs[:candidate_limit])

        if not dense_candidates:
            return []

        logger.info(
            "Vector retrieval candidates: backend=%s, strategy=%s, top_k=%s, "
            "candidate_count=%s, dense_weight=%s, sparse_weight=%s, "
            "query_dense_norm=%.4f, query_sparse_terms=%s, query_sparse_norm=%.4f",
            query_backend,
            self.distance_strategy,
            top_k,
            len(dense_candidates),
            self.hybrid_dense_weight,
            self.hybrid_sparse_weight,
            query_dense_norm,
            len(pruned_query_sparse),
            query_sparse_norm,
        )

        # node_groups: 문서 id별로 묶은 dense candidate 목록
        # node_candidate_counts: 문서 id별 dense 후보 수
        node_groups = defaultdict(list)
        node_candidate_counts = defaultdict(int)

        for emb in dense_candidates:
            uid = emb.chunk.parse_result.node.uid

            # 문서당 dense 후보 과다 유입 방지
            # per_node_candidate_cap이 0이면 문서당 후보 수 제한 없음
            if self.per_node_candidate_cap != 0 and node_candidate_counts[uid] >= self.per_node_candidate_cap:
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
            candidate_dense_norm = _l2_norm(getattr(emb, "vector", None))
            score_checks = self._score_checks(
                query_dense_norm=query_dense_norm,
                candidate_dense_norm=candidate_dense_norm,
                distance=emb.distance,
                dense_score=dense_score,
                sparse_score=sparse_score,
                hybrid_score=hybrid_score,
            )

            if score_checks:
                logger.warning(
                    "Vector retrieval score check: node_id=%s, chunk_id=%s, "
                    "checks=%s, distance=%s, dense_score=%s, sparse_score=%s, "
                    "hybrid_score=%s",
                    uid,
                    emb.chunk.id,
                    score_checks,
                    emb.distance,
                    dense_score,
                    sparse_score,
                    hybrid_score,
                )

            node_groups[uid].append(
                {
                    "embedding": emb,
                    "dense_score": dense_score,
                    "sparse_score": sparse_score,
                    "hybrid_score": hybrid_score,
                    "distance": emb.distance,
                    "candidate_dense_norm": candidate_dense_norm,
                    "score_checks": score_checks,
                }
            )
            node_candidate_counts[uid] += 1

        if not node_groups:
            return []

        retrieved_data = []
        
        # node별로 묶은 dense candidate들을 가지고 hybrid pooling을 수행합니다.
        for uid, hits in node_groups.items():
            if not hits:
                continue

            hits.sort(key=lambda item: item["hybrid_score"], reverse=True)

            evidence_hits = hits[: self.evidence_top_k]
            pool_hits = hits[: self.pool_top_k]

            pooled_score = self._pool_scores([item["hybrid_score"] for item in pool_hits])

            # hits 전체 길이 기준으로 약한 길이 패널티
            length_penalty = 1.0 + self.doc_length_penalty_alpha * math.log1p(len(hits))
            doc_score = pooled_score / length_penalty
            doc_checks = []
            if not _is_finite_number(pooled_score):
                doc_checks.append("pooled_score_not_finite")
            if length_penalty <= 0 or not _is_finite_number(length_penalty):
                doc_checks.append("length_penalty_invalid")
            if not _is_finite_number(doc_score):
                doc_checks.append("doc_score_not_finite")

            if doc_checks:
                logger.warning(
                    "Vector retrieval document score check: node_id=%s, "
                    "checks=%s, pooled_score=%s, length_penalty=%s, doc_score=%s",
                    uid,
                    doc_checks,
                    pooled_score,
                    length_penalty,
                    doc_score,
                )

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
                    "candidate_dense_norm": item["candidate_dense_norm"],
                    "score_checks": item["score_checks"],
                }
                evidences.append(evidence)

            meta_chunk = evidence_hits[0]["embedding"].chunk.parse_result
            score_details = {
                "distance_strategy": self.distance_strategy,
                "pooling_method": self.pooling_method,
                "dense_weight": self.hybrid_dense_weight,
                "sparse_weight": self.hybrid_sparse_weight,
                "query_dense_norm": query_dense_norm,
                "query_sparse_terms": len(pruned_query_sparse),
                "query_sparse_norm": query_sparse_norm,
                "input_threshold": threshold,
                "distance_threshold": distance_threshold,
                "hit_count": len(hits),
                "pool_hit_count": len(pool_hits),
                "evidence_hit_count": len(evidence_hits),
                "pool_tau": self.pool_tau,
                "pool_top_k": self.pool_top_k,
                "pooled_score": pooled_score,
                "softmax_score": pooled_score,
                "length_penalty": length_penalty,
                "doc_score": doc_score,
                "top_hybrid_scores": [item["hybrid_score"] for item in pool_hits],
                "checks": doc_checks,
            }

            retrieved_data.append(
                {
                    "node_id": str(uid),
                    "node_name": meta_chunk.node.name,
                    "file_ext": meta_chunk.metadata.get("file_ext", ""),
                    "doc_score": doc_score,
                    "score_details": score_details,
                    "evidences": evidences,
                }
            )

        retrieved_data.sort(key=lambda x: x["doc_score"], reverse=True)
        if top_k == 0:
            return retrieved_data
        return retrieved_data[:top_k]
