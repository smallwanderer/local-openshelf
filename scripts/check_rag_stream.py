#!/usr/bin/env python3
"""Small authenticated NDJSON smoke/load check for the RAG endpoint."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


def run_once(args, index: int) -> dict:
    headers = {
        "Accept": "application/x-ndjson",
        "Content-Type": "application/json",
        "Cookie": args.cookie,
        "X-CSRFToken": args.csrf_token,
    }
    started = time.perf_counter()
    first_token_at = None
    event_types = []
    with requests.post(
        args.url,
        headers=headers,
        json={"question": args.question, "top_k": args.top_k, "language": args.language},
        stream=True,
        timeout=(5, args.timeout),
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            event = json.loads(line)
            event_types.append(event.get("type"))
            if event.get("type") == "token" and first_token_at is None:
                first_token_at = time.perf_counter()

    completed = time.perf_counter()
    valid = event_types[:1] == ["started"] and event_types[-1:] == ["completed"]
    return {
        "request": index,
        "ok": valid,
        "ttft_ms": round((first_token_at - started) * 1000, 1) if first_token_at else None,
        "total_ms": round((completed - started) * 1000, 1),
        "events": event_types,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8000/api/document-ai/v1/rag/stream/")
    parser.add_argument("--cookie", required=True, help="Browser Cookie header value")
    parser.add_argument("--csrf-token", required=True)
    parser.add_argument("--question", default="선택한 문서의 핵심 내용을 요약해줘")
    parser.add_argument("--language", choices=("ko", "en"), default="ko")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--requests", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=360)
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
        "ttft_ms_p50": statistics.median(
            result["ttft_ms"] for result in successful if result.get("ttft_ms") is not None
        ) if any(result.get("ttft_ms") is not None for result in successful) else None,
    }
    print(json.dumps({"summary": summary}, ensure_ascii=False))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
