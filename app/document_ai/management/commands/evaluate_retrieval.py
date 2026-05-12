import json
import math
from collections import defaultdict
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from config.enums import AIStatus
from document_ai.embedding.embeding_models import bge_m3_embedder, EmbeddingResult
from document_ai.models import DocumentChunk
from document_ai.parsers.config import get_embedding_backend, get_embedding_model


def _dense_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _sparse_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0

    if len(left) > len(right):
        left, right = right, left

    return sum(value * right.get(key, 0.0) for key, value in left.items())


def _load_dataset(dataset_path: str) -> list[dict]:
    raw = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise CommandError("Dataset must be a JSON array.")

    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise CommandError(f"Dataset item #{index} must be an object.")
        if not item.get("query"):
            raise CommandError(f"Dataset item #{index} is missing 'query'.")
        expected_ids = item.get("expected_node_ids")
        if not isinstance(expected_ids, list) or not expected_ids:
            raise CommandError(f"Dataset item #{index} needs a non-empty 'expected_node_ids' list.")

    return raw


def _rank_documents(
    query_embedding: EmbeddingResult,
    chunk_records: list[dict],
    chunk_embeddings: list[EmbeddingResult],
    top_k: int,
    dense_weight: float,
    sparse_weight: float,
) -> list[dict]:
    chunk_hits = []
    for record, embedding in zip(chunk_records, chunk_embeddings):
        dense_score = _dense_similarity(query_embedding.dense_vector, embedding.dense_vector)
        sparse_score = _sparse_similarity(query_embedding.sparse_vector, embedding.sparse_vector)
        hybrid_score = (dense_weight * dense_score) + (sparse_weight * sparse_score)
        chunk_hits.append(
            {
                "node_id": record["node_id"],
                "node_name": record["node_name"],
                "hybrid_score": hybrid_score,
            }
        )

    chunk_hits.sort(key=lambda item: item["hybrid_score"], reverse=True)
    chunk_hits = chunk_hits[:top_k]

    grouped_hits = defaultdict(list)
    for hit in chunk_hits:
        grouped_hits[hit["node_id"]].append(hit)

    documents = []
    for node_id, hits in grouped_hits.items():
        hits.sort(key=lambda item: item["hybrid_score"], reverse=True)
        top_hits = hits[:3]
        combined_score = sum(max(item["hybrid_score"], 0.0) for item in top_hits)
        doc_score = math.log10(1.0 + combined_score)
        documents.append(
            {
                "node_id": node_id,
                "node_name": top_hits[0]["node_name"],
                "doc_score": doc_score,
            }
        )

    documents.sort(key=lambda item: item["doc_score"], reverse=True)
    return documents


class Command(BaseCommand):
    help = "Evaluate retrieval quality for one or more embedding backends."

    def add_arguments(self, parser):
        parser.add_argument("--dataset", required=True, help="Path to a JSON relevance dataset.")
        parser.add_argument(
            "--backend",
            action="append",
            dest="backends",
            help="Embedding backend to evaluate. May be supplied multiple times.",
        )
        parser.add_argument(
            "--model-name",
            default=get_embedding_model(),
            help="Embedding model name.",
        )
        parser.add_argument(
            "--user-email",
            help="Limit evaluation corpus to one user's documents.",
        )
        parser.add_argument(
            "--top-k",
            type=int,
            default=5,
            help="Top-k to use for ranking and metrics.",
        )

    def handle(self, *args, **options):
        dataset = _load_dataset(options["dataset"])
        backends = options.get("backends") or [get_embedding_backend()]
        model_name = options["model_name"]
        top_k = options["top_k"]
        dense_weight = float(getattr(settings, "EMBEDDING_HYBRID_DENSE_WEIGHT", 0.5))
        sparse_weight = float(getattr(settings, "EMBEDDING_HYBRID_SPARSE_WEIGHT", 0.5))

        chunk_qs = DocumentChunk.objects.select_related(
            "parse_result",
            "parse_result__node",
        ).filter(parse_result__status=AIStatus.COMPLETED)

        user_email = options.get("user_email")
        if user_email:
            chunk_qs = chunk_qs.filter(parse_result__node__owner__email=user_email)

        chunk_records = [
            {
                "chunk_id": chunk.id,
                "node_id": str(chunk.parse_result.node.uid),
                "node_name": chunk.parse_result.node.name,
                "text": chunk.text,
            }
            for chunk in chunk_qs
            if (chunk.text or "").strip()
        ]

        if not chunk_records:
            raise CommandError("No completed chunks were found for evaluation.")

        self.stdout.write(
            f"Loaded {len(chunk_records)} chunks and {len(dataset)} evaluation queries."
        )

        for backend in backends:
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(f"[backend={backend}]"))

            chunk_embeddings = [
                bge_m3_embedder(
                    text=record["text"],
                    model_name=model_name,
                    backend=backend,
                )
                for record in chunk_records
            ]

            hits_at_k = 0
            hits_at_1 = 0
            reciprocal_rank_sum = 0.0

            for item in dataset:
                expected_ids = {str(node_id) for node_id in item["expected_node_ids"]}
                query_embedding = bge_m3_embedder(
                    text=item["query"],
                    model_name=model_name,
                    backend=backend,
                )
                ranked_docs = _rank_documents(
                    query_embedding=query_embedding,
                    chunk_records=chunk_records,
                    chunk_embeddings=chunk_embeddings,
                    top_k=top_k,
                    dense_weight=dense_weight,
                    sparse_weight=sparse_weight,
                )

                ranked_node_ids = [doc["node_id"] for doc in ranked_docs]
                if ranked_node_ids and ranked_node_ids[0] in expected_ids:
                    hits_at_1 += 1

                matched_rank = None
                for rank, node_id in enumerate(ranked_node_ids, start=1):
                    if node_id in expected_ids:
                        matched_rank = rank
                        break

                if matched_rank is not None:
                    hits_at_k += 1
                    reciprocal_rank_sum += 1.0 / matched_rank

            total_queries = len(dataset)
            summary = {
                "backend": backend,
                "queries": total_queries,
                "dense_weight": dense_weight,
                "sparse_weight": sparse_weight,
                "hit_rate_at_1": round(hits_at_1 / total_queries, 4),
                "hit_rate_at_k": round(hits_at_k / total_queries, 4),
                "mrr_at_k": round(reciprocal_rank_sum / total_queries, 4),
            }
            self.stdout.write(json.dumps(summary, ensure_ascii=True, indent=2))
