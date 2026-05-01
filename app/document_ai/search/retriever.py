import logging
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional

from pgvector.django import CosineDistance, L2Distance, MaxInnerProduct

from config.enums import AIStatus
from document_ai.models import ChunkEmbedding
from document_ai.parsers.config import get_embedding_backend

logger = logging.getLogger(__name__)

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

    def _get_distance_func(self, vector):
        if self.distance_strategy == "cosine":
            return CosineDistance("vector", vector)
        if self.distance_strategy == "l2":
            return L2Distance("vector", vector)
        if self.distance_strategy == "inner_product":
            return MaxInnerProduct("vector", vector)
        raise ValueError(f"Unknown distance strategy: {self.distance_strategy}")

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        threshold: Optional[float] = None,
        node_ids: Optional[List[int]] = None,
        user=None,
    ) -> List[Dict[str, Any]]:
        from document_ai.embedding.embeding_models import bge_m3_embedder
        
        qs = ChunkEmbedding.objects.select_related(
            "chunk",
            "chunk__parse_result",
            "chunk__parse_result__node",
        ).filter(model_name=self.model_name, status=AIStatus.COMPLETED)

        if user is not None:
            qs = qs.filter(chunk__parse_result__node__owner=user)

        if node_ids is not None:
            qs = qs.filter(chunk__parse_result__node__uid__in=node_ids)

        query_backend = self.embedding_backend
        if not qs.filter(model_version=self.embedding_backend).exists():
            if self.embedding_backend != "hf_cls_legacy":
                logger.warning(
                    "No embeddings found for backend=%s. Falling back to legacy embeddings.",
                    self.embedding_backend,
                )
            query_backend = "hf_cls_legacy"

        try:
            query_vector = bge_m3_embedder(
                query,
                model_name=self.model_name,
                backend=query_backend,
            )
        except Exception as exc:
            logger.error("Failed to embed query: %s", exc)
            raise

        distance_func = self._get_distance_func(query_vector)

        qs = qs.filter(model_version="" if query_backend == "hf_cls_legacy" else query_backend)
        qs = qs.annotate(distance=distance_func)

        if threshold is not None:
            qs = qs.filter(distance__lte=threshold)

        results = qs.order_by("distance")[:top_k]

        # Group by node ID
        node_groups = defaultdict(list)
        for emb in results:
            node_groups[emb.chunk.parse_result.node.uid].append(emb)

        retrieved_data = []
        for uid, embs in node_groups.items():
            # 문서 내 중복 제거를 위해 가장 가까운 top 3 청크만 선택 (이미 results에 distance 순으로 정렬되어 있을 수 있지만, 필터에서 여러 문서가 섞이므로 한 문서 내 정렬 보장 필요)
            embs.sort(key=lambda x: x.distance)
            top_3 = embs[:3]

            # PGVector may return NaN if comparing against zero vectors
            for e in top_3:
                if e.distance is None or math.isnan(e.distance):
                    e.distance = 2.0  # Max cosine distance equivalent

            # `doc_score` 계산 (로그 기반 정규화)
            # 유사도가 높음 = distance가 작음. 1 / (1 + distance)을 score 값으로 사용하여 합산
            combined_score = sum(1.0 / (1.0 + e.distance) for e in top_3)
            doc_score = math.log10(1.0 + combined_score)

            # 증거물(Chunk) 목록 생성 (원문 흐름을 위해 chunk_id로 정렬)
            top_3.sort(key=lambda x: x.chunk.id)
            evidences = []
            
            for e in top_3:
                chunk = e.chunk
                evidence = {
                    "chunk_id": chunk.id,
                    "text": chunk.text,
                    "section": chunk.section_title or "",
                    "pages": f"{chunk.page_from}-{chunk.page_to}" if chunk.page_to else str(chunk.page_from),
                    "distance": e.distance,
                }
                evidences.append(evidence)

            # 해당 문서 메타 추출 (첫 번째 청크 기준)
            meta_chunk = top_3[0].chunk.parse_result

            retrieved_data.append({
                "node_id": str(uid),
                "node_name": meta_chunk.node.name,
                "file_ext": meta_chunk.metadata.get("file_ext", ""),
                "doc_score": doc_score,
                "evidences": evidences,
            })

        # 최종 반환 시, 문서 전체의 doc_score가 높은 순(내림차순)으로 정렬
        retrieved_data.sort(key=lambda x: x["doc_score"], reverse=True)

        return retrieved_data
