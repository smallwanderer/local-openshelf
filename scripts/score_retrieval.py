#!/usr/bin/env python3
"""Score the operational search endpoint against a graded relevance dataset.

Computes Recall@K and nDCG@K (graded 0-3 relevance), unlike `evaluate_retrieval`
which only reports HitRate/MRR from a separate, non-operational ranking
implementation (see dev-docs/evaluation/evaluation-status.md). This script
calls the real `/api/document-ai/v1/search/` endpoint instead, so results
reflect the actual VectorRetriever path.

Matches check_search_load.py's pattern (requests, no extra deps, session
Cookie auth - the search endpoint is csrf_exempt).

Dataset format: dev-docs/evaluation/datasets/dotori-docs-relevance-v1.json
Each query has a "relevance" map of {node_name: 0-3 grade} covering every
document in the corpus (exhaustive judgment - safe for a small corpus only).
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

import requests


def dcg(grades: list[int]) -> float:
    return sum((2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(grades))


def ndcg_at_k(ranked_grades: list[int], all_grades: list[int], k: int) -> float | None:
    ideal = sorted(all_grades, reverse=True)[:k]
    ideal_dcg = dcg(ideal)
    if ideal_dcg == 0:
        return None
    return dcg(ranked_grades[:k]) / ideal_dcg


def recall_at_k(ranked_grades: list[int], total_relevant: int, k: int, threshold: int) -> float | None:
    if total_relevant == 0:
        return None
    hit = sum(1 for grade in ranked_grades[:k] if grade >= threshold)
    return hit / total_relevant


def run_query(args, item: dict) -> dict:
    payload = {
        "mode": args.mode,
        "query": item["query"],
        "top_k": args.top_k,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": args.cookie,
    }
    response = requests.post(args.url, headers=headers, json=payload, timeout=(5, args.timeout))
    response.raise_for_status()
    body = response.json()
    results = body.get("results") or []
    ranked_names = [result["node_name"] for result in results]

    relevance = item["relevance"]
    threshold = args.relevant_threshold
    ranked_grades = [relevance.get(name, 0) for name in ranked_names]
    all_grades = list(relevance.values())
    total_relevant = sum(1 for grade in all_grades if grade >= threshold)

    metrics = {}
    for k in args.k:
        metrics[f"recall@{k}"] = recall_at_k(ranked_grades, total_relevant, k, threshold)
        metrics[f"ndcg@{k}"] = ndcg_at_k(ranked_grades, all_grades, k)

    return {
        "id": item["id"],
        "query": item["query"],
        "total_relevant": total_relevant,
        "ranked_node_names": ranked_names[: max(args.k)],
        "metrics": metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True, help="Path to a graded relevance dataset JSON file.")
    parser.add_argument("--url", default="http://localhost:8000/api/document-ai/v1/search/")
    parser.add_argument("--cookie", required=True, help="Browser Cookie header value (session auth only).")
    parser.add_argument("--mode", default="basic", choices=["basic", "advanced"])
    parser.add_argument("--top-k", type=int, default=10, help="top_k sent to the search API.")
    parser.add_argument("--k", type=int, action="append", default=None, help="K values to score. Repeat. Default: 1 3 5.")
    parser.add_argument("--relevant-threshold", type=int, default=None, help="Grade at/above which a doc counts as relevant for Recall. Defaults to dataset's relevant_threshold_for_recall or 2.")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    if args.k is None:
        args.k = [1, 3, 5]
    if args.relevant_threshold is None:
        args.relevant_threshold = dataset.get("relevant_threshold_for_recall", 2)

    per_query = []
    for item in dataset["queries"]:
        try:
            result = run_query(args, item)
        except Exception as exc:
            result = {"id": item["id"], "query": item["query"], "error": str(exc)}
        per_query.append(result)
        print(json.dumps(result, ensure_ascii=False))

    scored = [r for r in per_query if "error" not in r]
    summary = {"queries": len(per_query), "errors": len(per_query) - len(scored)}
    for k in args.k:
        recalls = [r["metrics"][f"recall@{k}"] for r in scored if r["metrics"][f"recall@{k}"] is not None]
        ndcgs = [r["metrics"][f"ndcg@{k}"] for r in scored if r["metrics"][f"ndcg@{k}"] is not None]
        summary[f"recall@{k}_mean"] = round(statistics.mean(recalls), 4) if recalls else None
        summary[f"recall@{k}_n"] = len(recalls)
        summary[f"ndcg@{k}_mean"] = round(statistics.mean(ndcgs), 4) if ndcgs else None
        summary[f"ndcg@{k}_n"] = len(ndcgs)

    no_relevant = [r["id"] for r in scored if r.get("total_relevant") == 0]
    if no_relevant:
        summary["queries_with_no_relevant_docs"] = no_relevant

    print(json.dumps({"summary": summary}, ensure_ascii=False, indent=2))
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
