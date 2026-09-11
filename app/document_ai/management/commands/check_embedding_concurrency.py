from __future__ import annotations

import random
import resource
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.management.base import BaseCommand, CommandError

# Real chunk-sized text, not just short sentences -- thread-safety issues in
# encode() can depend on sequence length (attention buffer sizes, etc).
_LONG_EN = (
    "Dotori is a self-hosted document search and retrieval-augmented "
    "generation platform. " * 15
)
_LONG_KO = (
    "도토리는 셀프호스팅 문서 검색 및 RAG 플랫폼입니다. 다양한 문서 형식을 지원합니다. " * 15
)
_DEFAULT_TEXTS = [
    "The quick brown fox jumps over the lazy dog.",
    "오늘 날씨가 정말 좋습니다.",
    "Concurrent requests may cause race conditions.",
    "임베딩 벡터의 정확성을 검증해야 합니다.",
    _LONG_EN,
    _LONG_KO,
    "This exact text is requested many times concurrently to test caching correctness.",
]


class Command(BaseCommand):
    help = (
        "Stress-test BGE-M3's encode() for thread-safety under concurrent calls "
        "on a single shared model instance. Loads its own real model instance "
        "directly (bypassing the RemoteBGEM3Provider routing that normal request "
        "traffic goes through), so it must run somewhere that actually has "
        "torch/FlagEmbedding installed -- run via "
        "`docker compose exec embedding-executor python manage.py check_embedding_concurrency` "
        "(not app, which no longer has those dependencies installed at all). "
        "Compares each concurrent result against a sequential ground truth computed "
        "up front; any mismatch, exception, or crash means encode() is not safe to "
        "call concurrently without the _EMBED_LOCK serialization currently in "
        "internal_views.py. Defaults are chosen to finish in a few minutes; "
        "raise --concurrency/--rounds for a stronger run once you have time to "
        "let it run longer, and lower --torch-threads first if CPU usage looks "
        "like it is thrashing (all cores pegged, no progress for minutes) -- "
        "that means torch/FlagEmbedding's own internal threading is "
        "oversubscribing your cores, not that the test is stuck."
    )

    def add_arguments(self, parser):
        parser.add_argument("--rounds", type=int, default=6, help="Number of concurrent batches to run.")
        parser.add_argument("--concurrency", type=int, default=4, help="Threads per round.")
        parser.add_argument("--jobs-per-round", type=int, default=None, help="Calls per round (default: concurrency * 2).")
        parser.add_argument("--max-length", type=int, default=512, help="max_length passed to encode().")
        parser.add_argument(
            "--torch-threads",
            type=int,
            default=1,
            help="torch.set_num_threads() -- keep at 1 unless you know your "
            "CPU has cores to spare per concurrent call. Higher values combined "
            "with --concurrency > 1 commonly cause severe slowdowns from thread "
            "oversubscription, not a hang.",
        )

    def handle(self, *args, **options):
        try:
            import numpy as np
        except ImportError as exc:
            raise CommandError("numpy is required for this check.") from exc

        import torch

        torch.set_num_threads(max(1, options["torch_threads"]))

        from document_ai.embedding.providers.bgem3 import BGEM3HybridProvider
        from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

        runtime = get_active_embedding_runtime()
        if runtime.provider != "bgem3_hybrid":
            raise CommandError(f"This command only knows how to stress-test bgem3_hybrid; active provider is {runtime.provider}.")

        # Deliberately bypass get_embedding_provider()'s process gating
        # (DOTORI_EMBEDDING_MODEL_PROCESS) and construct the real provider
        # directly -- `docker compose exec` sessions don't inherit that env var
        # (only gunicorn's own inline-prefixed command line has it), so going
        # through the registry here would silently hit RemoteBGEM3Provider and
        # just re-test the HTTP layer's _EMBED_LOCK/503 behavior instead of the
        # model's actual thread-safety, which is what this command is for.
        provider = BGEM3HybridProvider(
            model_name=runtime.model_id,
            model_revision=runtime.model_revision,
            dimension=runtime.dimension,
            normalize_embeddings=runtime.normalize_embeddings,
            query_prefix=runtime.query_prefix,
            document_prefix=runtime.document_prefix,
        )

        def encode_one(text: str):
            result = provider.embed_query(text, max_length=options["max_length"])
            return np.array(result.dense_vector), dict(result.sparse_vector)

        def cosine(a, b) -> float:
            return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

        def sparse_match(a: dict, b: dict, tol: float = 1e-4) -> tuple[bool, str]:
            if set(a) != set(b):
                return False, "key mismatch"
            for key in a:
                if abs(a[key] - b[key]) > tol:
                    return False, f"value mismatch at {key}"
            return True, ""

        self.stdout.write("Computing sequential ground truth...")
        ground_truth = {text: encode_one(text) for text in _DEFAULT_TEXTS}
        self.stdout.write(self.style.SUCCESS(f"Ground truth ready for {len(_DEFAULT_TEXTS)} texts."))

        concurrency = options["concurrency"]
        rounds = options["rounds"]
        jobs_per_round = options["jobs_per_round"] or concurrency * 2

        before_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        started = time.perf_counter()
        failures: list[tuple[int, str, str]] = []
        total_calls = 0

        for round_num in range(rounds):
            jobs = [random.choice(_DEFAULT_TEXTS) for _ in range(jobs_per_round)]
            total_calls += len(jobs)
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(encode_one, text): text for text in jobs}
                for future in as_completed(futures):
                    text = futures[future]
                    label = text[:30].replace("\n", " ")
                    try:
                        dense, sparse = future.result()
                    except Exception as exc:  # noqa: BLE001 - want to record any failure
                        failures.append((round_num, label, f"EXCEPTION: {exc}"))
                        continue
                    gt_dense, gt_sparse = ground_truth[text]
                    similarity = cosine(dense, gt_dense)
                    if similarity < 0.999:
                        failures.append((round_num, label, f"dense mismatch cosine_sim={similarity:.6f}"))
                    ok, reason = sparse_match(sparse, gt_sparse)
                    if not ok:
                        failures.append((round_num, label, f"sparse mismatch: {reason}"))
            self.stdout.write(
                f"round {round_num + 1}/{rounds} done ({total_calls} calls so far), "
                f"failures so far: {len(failures)}"
            )

        elapsed_s = time.perf_counter() - started
        after_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

        self.stdout.write("")
        self.stdout.write(
            f"elapsed: {elapsed_s:.1f}s, RSS before: {before_rss_mb:.1f} MB, "
            f"after: {after_rss_mb:.1f} MB, growth: {after_rss_mb - before_rss_mb:.1f} MB"
        )

        if failures:
            self.stdout.write(self.style.ERROR(f"FAILURES: {len(failures)}/{total_calls}"))
            for round_num, label, reason in failures[:20]:
                self.stdout.write(f"  round={round_num} text={label!r} {reason}")
            raise CommandError(
                f"{len(failures)}/{total_calls} concurrent encode() calls failed -- "
                "do not remove _EMBED_LOCK's serialization without further investigation."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"ALL PASSED: {total_calls} concurrent calls across {rounds} rounds "
                f"(concurrency={concurrency}), dense+sparse both verified, no race conditions."
            )
        )
