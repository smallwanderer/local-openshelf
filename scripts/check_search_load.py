#!/usr/bin/env python3
"""Small authenticated JSON smoke/load check for the vector search endpoint.

Mirrors check_rag_stream.py's pattern (requests + ThreadPoolExecutor, no extra
deps). Not a throughput benchmark - meant for a handful of concurrent requests
(2-5), mainly to check whether search stays responsive while dotori-document
is busy (see performance-and-reliability.md P0-5 manual verification procedure).
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


def run_once(args, index: int) -> dict:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": args.cookie,
    }
    payload = {"query": args.query, "top_k": args.top_k}
    if args.threshold is not None:
        payload["threshold"] = args.threshold
    if args.node_ids:
        payload["node_ids"] = args.node_ids

    started = time.perf_counter()
    response = requests.post(
        args.url,
        headers=headers,
        json=payload,
        timeout=(5, args.timeout),
    )
    completed = time.perf_counter()

    ok = response.status_code == 200
    metrics = {}
    result_count = None
    if ok:
        body = response.json()
        metrics = body.get("performance_metrics") or {}
        result_count = len(body.get("results") or [])

    return {
        "request": index,
        "ok": ok,
        "status_code": response.status_code,
        "total_ms": round((completed - started) * 1000, 1),
        "query_embedding_ms": metrics.get("query_embedding_ms"),
        "vector_query_ms": metrics.get("vector_query_ms"),
        "result_count": result_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000/api/document-ai/v1/search/")
    parser.add_argument("--cookie", required=True, help="Browser Cookie header value (session auth only, no CSRF token needed - this endpoint is csrf_exempt)")
    parser.add_argument("--query", default="선택한 문서의 핵심 내용을 요약해줘")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--node-id", dest="node_ids", action="append", default=[], help="Repeat to scope to multiple node UIDs")
    parser.add_argument("--requests", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(run_once, args, index + 1) for index in range(args.requests)]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False))

    successful = [result for result in results if result.get("ok")]
    summary = {
        "requests": len(results),
        "successful": len(successful),
        "failed": len(results) - len(successful),
        "total_ms_p50": statistics.median(result["total_ms"] for result in successful) if successful else None,
        "query_embedding_ms_p50": statistics.median(
            result["query_embedding_ms"] for result in successful if result.get("query_embedding_ms") is not None
        ) if any(result.get("query_embedding_ms") is not None for result in successful) else None,
    }
    print(json.dumps({"summary": summary}, ensure_ascii=False))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
