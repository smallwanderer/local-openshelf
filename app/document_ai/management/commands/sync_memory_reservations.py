from __future__ import annotations

from django.core.management.base import BaseCommand

from document_ai.services.embedding_runtime_config import load_embedding_runtime
from document_ai.services.rag_runtime_config import load_llm_runtime_config

from llm_installation.embedding_footprint import claim_embedding_from_generation
from llm_installation.memory_reservations import (
    CLAIM_LLM,
    claim_llm_runtime,
    clear_claim,
    get_total_reservation,
    read_reservations,
)


class Command(BaseCommand):
    help = (
        "Rebuild the scope's memory reservation file from the currently active "
        "runtimes. Claims are normally written when a runtime is activated; this "
        "backfills installs that predate the reservation file and repairs a file "
        "that was deleted or hand-edited."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--scope",
            choices=("production", "development"),
            default="production",
        )

    def handle(self, *args, **options):
        scope = options["scope"]

        llm_payload = load_llm_runtime_config(scope=scope)
        target = llm_payload.get("target") if isinstance(llm_payload, dict) else None
        llm_generation_id = (
            str(target.get("generation_id") or "") if isinstance(target, dict) else ""
        )
        if llm_generation_id:
            claim = claim_llm_runtime(scope, llm_generation_id)
            if claim is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"llm: generation {llm_generation_id} has no recorded memory "
                        "placement; claim not written."
                    )
                )
            else:
                self.stdout.write(
                    f"llm: claimed ram={claim.ram_mb} MB "
                    f"vram={list(claim.vram_per_gpu_mb) or '-'} "
                    f"(generation {claim.generation_id})"
                )
        else:
            # No configured runtime means nothing is held, and a stale claim
            # left behind would keep shrinking the other workload's budget.
            if clear_claim(CLAIM_LLM, scope=scope) is not None:
                self.stdout.write("llm: no active runtime; released stale claim.")
            else:
                self.stdout.write("llm: no active runtime.")

        embedding_runtime = load_embedding_runtime(scope=scope)
        embedding_claim = claim_embedding_from_generation(
            scope, embedding_runtime.generation_id
        )
        if embedding_claim is None:
            self.stdout.write(
                self.style.WARNING(
                    "embedding: could not resolve a generation to claim from "
                    f"({embedding_runtime.generation_id})."
                )
            )
        else:
            self.stdout.write(
                f"embedding: claimed device={embedding_claim.device} "
                f"ram={embedding_claim.ram_mb} MB "
                f"vram={list(embedding_claim.vram_per_gpu_mb) or '-'} "
                f"(generation {embedding_claim.generation_id})"
            )

        total = get_total_reservation(scope)
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Total reserved: ram={total.ram_mb} MB "
                f"vram={list(total.vram_per_gpu_mb) or '-'} "
                f"across {', '.join(read_reservations(scope)) or 'no workloads'}."
            )
        )
