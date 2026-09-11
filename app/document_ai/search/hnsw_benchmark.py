from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from time import perf_counter
from typing import Any, Iterable

from django.db import connection, transaction

from config.enums import AIStatus
from document_ai.models import (
    ChunkEmbedding,
    DocumentChunk,
    DocumentParseResult,
    EmbeddingGeneration,
)
from files.models import Node


DEFAULT_K_VALUES = (1, 3, 5, 10)
DEFAULT_EF_SEARCH_VALUES = (10, 20, 40, 80)


@dataclass(frozen=True)
class BenchmarkScope:
    workspace_id: int
    workspace_uid: str
    workspace_name: str
    generation_id: str
    model_id: str
    model_revision: str
    provider: str
    corpus_size: int


def parse_positive_int_list(value: str, *, option_name: str) -> list[int]:
    try:
        values = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    except ValueError as exc:
        raise ValueError(f"{option_name} must be a comma-separated list of integers.") from exc
    if not values or any(item <= 0 for item in values):
        raise ValueError(f"{option_name} values must all be positive integers.")
    return values


def percentile(values: Iterable[float], percentile_value: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    rank = max(0, math.ceil((percentile_value / 100.0) * len(ordered)) - 1)
    return ordered[min(rank, len(ordered) - 1)]


def latency_summary(values: Iterable[float]) -> dict[str, float | int]:
    samples = [float(value) for value in values]
    if not samples:
        return {"samples": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0}
    return {
        "samples": len(samples),
        "mean": round(mean(samples), 4),
        "p50": round(median(samples), 4),
        "p95": round(percentile(samples, 95), 4),
        "p99": round(percentile(samples, 99), 4),
    }


def recall_at_k(exact_ids: list[int], approximate_ids: list[int], k: int) -> float:
    exact = exact_ids[:k]
    if not exact:
        return 0.0
    return len(set(exact).intersection(approximate_ids[:k])) / len(exact)


def _quoted_table(model) -> str:
    return connection.ops.quote_name(model._meta.db_table)


def _nearest_neighbor_sql() -> str:
    embedding_table = _quoted_table(ChunkEmbedding)
    chunk_table = _quoted_table(DocumentChunk)
    parse_table = _quoted_table(DocumentParseResult)
    node_table = _quoted_table(Node)
    return f"""
        SELECT embedding.id
        FROM {embedding_table} embedding
        INNER JOIN {chunk_table} chunk ON chunk.id = embedding.chunk_id
        INNER JOIN {parse_table} parse_result ON parse_result.id = chunk.parse_result_id
        INNER JOIN {node_table} node ON node.id = parse_result.node_id
        WHERE parse_result.embedding_generation_id = %s
          AND embedding.status = %s
          AND embedding.vector IS NOT NULL
          AND node.workspace_id = %s
          AND node.trashed = FALSE
          AND embedding.id <> %s
        ORDER BY embedding.vector <#> %s::vector
        LIMIT %s
    """


def _set_planner_mode(cursor, *, mode: str, ef_search: int | None = None) -> None:
    settings = {
        "jit": "off",
        "enable_seqscan": "on",
        "enable_indexscan": "on",
        "enable_bitmapscan": "on",
        "enable_sort": "on",
        "hnsw.iterative_scan": "strict_order",
    }
    if mode == "exact":
        settings.update(enable_indexscan="off", enable_bitmapscan="off")
    elif mode == "ann":
        # With a tiny corpus PostgreSQL otherwise prefers a B-tree lookup plus
        # an explicit Sort even when sequential scans are disabled. Disabling
        # Sort here is benchmark-only: it forces the ordered vector index path
        # so ANN recall is never accidentally measured against an exact sort.
        settings.update(enable_seqscan="off", enable_bitmapscan="off", enable_sort="off")
    elif mode != "default":
        raise ValueError(f"Unknown planner mode: {mode}")

    if ef_search is not None:
        settings["hnsw.ef_search"] = str(ef_search)

    for name, value in settings.items():
        cursor.execute("SELECT set_config(%s, %s, true)", [name, value])


def _fetch_top_ids(
    *,
    scope: BenchmarkScope,
    query_embedding_id: int,
    query_vector: str,
    limit: int,
    mode: str,
    ef_search: int | None = None,
) -> tuple[list[int], float]:
    params = [
        scope.generation_id,
        AIStatus.COMPLETED,
        scope.workspace_id,
        query_embedding_id,
        query_vector,
        limit,
    ]
    with transaction.atomic(), connection.cursor() as cursor:
        _set_planner_mode(cursor, mode=mode, ef_search=ef_search)
        started = perf_counter()
        cursor.execute(_nearest_neighbor_sql(), params)
        rows = cursor.fetchall()
        elapsed = (perf_counter() - started) * 1000.0
    return [int(row[0]) for row in rows], elapsed


def _explain(
    *,
    scope: BenchmarkScope,
    query_embedding_id: int,
    query_vector: str,
    limit: int,
    mode: str,
    ef_search: int | None = None,
) -> dict[str, Any]:
    params = [
        scope.generation_id,
        AIStatus.COMPLETED,
        scope.workspace_id,
        query_embedding_id,
        query_vector,
        limit,
    ]
    explain_sql = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + _nearest_neighbor_sql()
    with transaction.atomic(), connection.cursor() as cursor:
        _set_planner_mode(cursor, mode=mode, ef_search=ef_search)
        cursor.execute(explain_sql, params)
        value = cursor.fetchone()[0]
    return value[0] if isinstance(value, list) else value


def _plan_facts(explain: dict[str, Any]) -> dict[str, Any]:
    node_types: list[str] = []
    index_names: list[str] = []

    def visit(node: dict[str, Any]) -> None:
        node_type = node.get("Node Type")
        if node_type:
            node_types.append(str(node_type))
        index_name = node.get("Index Name")
        if index_name:
            index_names.append(str(index_name))
        for child in node.get("Plans", []):
            visit(child)

    plan = explain.get("Plan", {})
    visit(plan)
    return {
        "node_types": node_types,
        "index_names": index_names,
        "planning_time_ms": explain.get("Planning Time"),
        "execution_time_ms": explain.get("Execution Time"),
        "actual_rows": plan.get("Actual Rows"),
    }


def resolve_scope(*, workspace, generation_id: str | None = None) -> tuple[BenchmarkScope, list[int]]:
    queryset = ChunkEmbedding.objects.filter(
        chunk__parse_result__node__workspace=workspace,
        chunk__parse_result__node__trashed=False,
        status=AIStatus.COMPLETED,
        vector__isnull=False,
    )
    available_generations = list(
        queryset.values_list(
            "chunk__parse_result__embedding_generation_id", flat=True
        ).exclude(
            chunk__parse_result__embedding_generation_id=""
        ).distinct().order_by("chunk__parse_result__embedding_generation_id")
    )
    if generation_id is None:
        if not available_generations:
            raise ValueError("The workspace has no completed dense embeddings.")
        if len(available_generations) > 1:
            joined = ", ".join(available_generations)
            raise ValueError(f"Multiple embedding generations are present; choose one: {joined}")
        generation_id = available_generations[0]
    elif generation_id not in available_generations:
        raise ValueError(f"Generation {generation_id!r} has no completed vectors in this workspace.")

    queryset = queryset.filter(
        chunk__parse_result__embedding_generation_id=generation_id
    ).order_by("id")
    embedding_ids = list(queryset.values_list("id", flat=True))
    generation = EmbeddingGeneration.objects.get(generation_id=generation_id)
    return (
        BenchmarkScope(
            workspace_id=workspace.pk,
            workspace_uid=str(workspace.uid),
            workspace_name=workspace.name,
            generation_id=generation.generation_id,
            model_id=generation.model_id,
            model_revision=generation.model_revision,
            provider=generation.provider,
            corpus_size=len(embedding_ids),
        ),
        embedding_ids,
    )


def run_hnsw_benchmark(
    *,
    workspace,
    generation_id: str | None,
    sample_size: int,
    k_values: list[int],
    ef_search_values: list[int],
    repeats: int,
    seed: int,
    min_recall: float,
) -> dict[str, Any]:
    scope, embedding_ids = resolve_scope(workspace=workspace, generation_id=generation_id)
    if scope.corpus_size < 2:
        raise ValueError("At least two completed vectors are required (the query vector is excluded).")
    if repeats <= 0 or sample_size <= 0:
        raise ValueError("sample_size and repeats must be positive.")
    if not 0.0 <= min_recall <= 1.0:
        raise ValueError("min_recall must be between 0 and 1.")

    max_neighbors = scope.corpus_size - 1
    invalid_k = [k for k in k_values if k > max_neighbors]
    if invalid_k:
        raise ValueError(
            f"K values {invalid_k} exceed the {max_neighbors} available neighbors after excluding self."
        )

    rng = random.Random(seed)
    query_ids = sorted(rng.sample(embedding_ids, min(sample_size, len(embedding_ids))))
    vector_rows = dict(
        ChunkEmbedding.objects.filter(id__in=query_ids).values_list("id", "vector")
    )
    query_vectors = {embedding_id: str(vector_rows[embedding_id]) for embedding_id in query_ids}
    max_k = max(k_values)

    # Prime PostgreSQL buffers and each planner path before collecting latency.
    first_id = query_ids[0]
    _fetch_top_ids(
        scope=scope,
        query_embedding_id=first_id,
        query_vector=query_vectors[first_id],
        limit=max_k,
        mode="exact",
    )
    for ef_search in ef_search_values:
        _fetch_top_ids(
            scope=scope,
            query_embedding_id=first_id,
            query_vector=query_vectors[first_id],
            limit=max_k,
            mode="ann",
            ef_search=ef_search,
        )

    exact_by_query: dict[int, list[int]] = {}
    exact_latencies: list[float] = []
    for query_id in query_ids:
        for repeat_index in range(repeats):
            ids, elapsed = _fetch_top_ids(
                scope=scope,
                query_embedding_id=query_id,
                query_vector=query_vectors[query_id],
                limit=max_k,
                mode="exact",
            )
            if repeat_index == 0:
                exact_by_query[query_id] = ids
            exact_latencies.append(elapsed)

    per_query: list[dict[str, Any]] = []
    ef_summaries: list[dict[str, Any]] = []
    ann_plans: dict[str, Any] = {}
    for ef_search in ef_search_values:
        recalls = {k: [] for k in k_values}
        ann_latencies: list[float] = []
        for query_id in query_ids:
            approximate_ids: list[int] | None = None
            for repeat_index in range(repeats):
                ids, elapsed = _fetch_top_ids(
                    scope=scope,
                    query_embedding_id=query_id,
                    query_vector=query_vectors[query_id],
                    limit=max_k,
                    mode="ann",
                    ef_search=ef_search,
                )
                if repeat_index == 0:
                    approximate_ids = ids
                ann_latencies.append(elapsed)
            approximate_ids = approximate_ids or []
            query_recalls = {
                str(k): recall_at_k(exact_by_query[query_id], approximate_ids, k)
                for k in k_values
            }
            for k in k_values:
                recalls[k].append(query_recalls[str(k)])
            per_query.append(
                {
                    "query_embedding_id": query_id,
                    "ef_search": ef_search,
                    "exact_ids": exact_by_query[query_id],
                    "ann_ids": approximate_ids,
                    "recall_at_k": query_recalls,
                }
            )

        recall_summary = {
            str(k): round(mean(recalls[k]), 6) if recalls[k] else 0.0 for k in k_values
        }
        ann_explain = _explain(
            scope=scope,
            query_embedding_id=first_id,
            query_vector=query_vectors[first_id],
            limit=max_k,
            mode="ann",
            ef_search=ef_search,
        )
        ann_facts = _plan_facts(ann_explain)
        ann_plans[str(ef_search)] = {"facts": ann_facts, "explain": ann_explain}
        ef_summaries.append(
            {
                "ef_search": ef_search,
                "recall_at_k": recall_summary,
                "latency_ms": latency_summary(ann_latencies),
                "hnsw_index_used": "chunk_embedding_vector_hnsw_idx" in ann_facts["index_names"],
                "meets_min_recall": all(value >= min_recall for value in recall_summary.values()),
            }
        )

    default_explain = _explain(
        scope=scope,
        query_embedding_id=first_id,
        query_vector=query_vectors[first_id],
        limit=max_k,
        mode="default",
    )
    exact_explain = _explain(
        scope=scope,
        query_embedding_id=first_id,
        query_vector=query_vectors[first_id],
        limit=max_k,
        mode="exact",
    )
    return {
        "schema_version": 1,
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"),
        "query_source": "stored_document_embeddings_excluding_self",
        "scope": {
            "workspace_id": scope.workspace_id,
            "workspace_uid": scope.workspace_uid,
            "workspace_name": scope.workspace_name,
            "generation_id": scope.generation_id,
            "model_id": scope.model_id,
            "model_revision": scope.model_revision,
            "provider": scope.provider,
            "corpus_size": scope.corpus_size,
        },
        "parameters": {
            "sample_size_requested": sample_size,
            "sample_size_actual": len(query_ids),
            "query_embedding_ids": query_ids,
            "k_values": k_values,
            "ef_search_values": ef_search_values,
            "repeats": repeats,
            "seed": seed,
            "min_recall": min_recall,
            "distance_operator": "<#>",
            "hnsw_iterative_scan": "strict_order",
        },
        "summary": {
            "exact_latency_ms": latency_summary(exact_latencies),
            "ef_search": ef_summaries,
            "all_forced_ann_plans_use_hnsw": all(item["hnsw_index_used"] for item in ef_summaries),
        },
        "plans": {
            "default": {"facts": _plan_facts(default_explain), "explain": default_explain},
            "exact": {"facts": _plan_facts(exact_explain), "explain": exact_explain},
            "ann": ann_plans,
        },
        "per_query": per_query,
    }


def write_benchmark_result(result: dict[str, Any], output_dir: str | Path) -> Path:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    output_path = destination / f"{result['run_id']}.json"
    temporary_path = output_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(output_path)
    return output_path
