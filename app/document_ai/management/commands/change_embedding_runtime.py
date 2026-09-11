from __future__ import annotations

import json
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from config.enums import AIStatus
from document_ai.embedding.registry import get_embedding_provider
from document_ai.embedding.indexes import replace_active_hnsw_index
from document_ai.embedding.store_registry import get_embedding_store_instance
from document_ai.models import (
    ChunkSegmentEmbedding,
    DocumentChunk,
    DocumentParseResult,
    EmbeddingGeneration,
)
from document_ai.parsers.text_utils import normalize_extracted_text
from document_ai.services.embedding_runtime_config import (
    clear_embedding_runtime_cache,
    load_embedding_runtime,
)
from llm_installation.embedding_catalog import (
    get_embedding_catalog_entry,
    get_embedding_catalog_entry_for_preset,
)
from llm_installation.embedding_config_store import (
    commit_active_embedding_runtime,
    get_embedding_runtime_config_path,
    write_embedding_runtime_generation,
)


LOCK_NAME = "dotori_embedding_runtime_change"


class Command(BaseCommand):
    help = (
        "Validate an embedding runtime or activate it during a maintenance "
        "re-embedding of the complete corpus."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--preset",
            choices=("speed", "balanced", "quality"),
            default=None,
        )
        parser.add_argument(
            "--catalog-id",
            default=None,
            help="Catalog model or profile ID to activate (e.g. bge-m3-hybrid, openai-text-embedding-3-small).",
        )
        parser.add_argument(
            "--candidate-generation-id",
            default=None,
            help="Use an already staged candidate generation ID from embedding_generations/.",
        )
        parser.add_argument(
            "--scope",
            choices=("production",),
            default="production",
        )
        parser.add_argument(
            "--activate",
            action="store_true",
            help="Re-embed in place and activate after complete corpus coverage.",
        )
        parser.add_argument(
            "--skip-reembed",
            action="store_true",
            help="Activate the candidate generation without re-embedding existing documents.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Validate the provider on a limited sample without persisting vectors.",
        )
        parser.add_argument(
            "--force-reembed",
            action="store_true",
            help="Rebuild even when the selected catalog revision is active.",
        )
        parser.add_argument(
            "--check-chunks",
            action="store_true",
            help="Return completed document and chunk count in DB as JSON and exit.",
        )

    def _acquire_lock(self) -> bool:
        if connection.vendor != "postgresql":
            return True
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))",
                [LOCK_NAME],
            )
            return bool(cursor.fetchone()[0])

    def _release_lock(self) -> None:
        if connection.vendor != "postgresql":
            return
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))",
                [LOCK_NAME],
            )

    def handle(self, *args, **options):
        if options.get("check_chunks"):
            chunk_qs = (
                DocumentChunk.objects.select_related("parse_result__node")
                .filter(
                    parse_result__status=AIStatus.COMPLETED,
                    parse_result__node__trashed=False,
                    parse_result__node__ai_processing_enabled=True,
                )
            )
            doc_qs = (
                DocumentParseResult.objects.filter(
                    status=AIStatus.COMPLETED,
                    node__trashed=False,
                    node__ai_processing_enabled=True,
                    chunks__isnull=False,
                ).distinct()
            )
            payload = {
                "documents": doc_qs.count(),
                "chunks": chunk_qs.count(),
            }
            self.stdout.write(json.dumps(payload))
            return

        if options["activate"] and options.get("limit"):
            raise CommandError("A limited candidate run cannot be activated.")
        if not self._acquire_lock():
            raise CommandError("Another embedding runtime change is in progress.")

        try:
            self._change_runtime(**options)
        finally:
            self._release_lock()

    def _change_runtime(self, **options):
        active_runtime = load_embedding_runtime(scope=options["scope"])
        active_path = get_embedding_runtime_config_path(options["scope"])

        # Preserve the previous active snapshot as a rollback artifact,
        # including legacy .env-derived installations.
        previous_generation_dir = (
            active_path.parent
            / "embedding_generations"
            / active_runtime.generation_id
        )
        previous_generation_path = previous_generation_dir / "runtime.json"
        if not previous_generation_path.exists():
            previous_generation_dir.mkdir(parents=True, exist_ok=True)
            previous_generation_path.write_text(
                json.dumps(
                    active_runtime.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

        candidate_gen_id = options.get("candidate_generation_id")
        catalog_id = options.get("catalog_id")
        preset = options.get("preset")

        if candidate_gen_id:
            generation_id = candidate_gen_id
            generation_dir = (
                active_path.parent
                / "embedding_generations"
                / generation_id
            )
            runtime_json = generation_dir / "runtime.json"
            if not runtime_json.exists():
                raise CommandError(
                    f"Candidate generation runtime.json not found: {runtime_json}"
                )
            runtime = load_embedding_runtime(
                path=runtime_json,
                scope=options["scope"],
            )
            if (
                not options.get("force_reembed")
                and active_runtime.model_id == runtime.model_id
                and active_runtime.model_revision == runtime.model_revision
                and active_runtime.provider == runtime.provider
                and active_runtime.dimension == runtime.dimension
            ):
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Embedding runtime is already active: {runtime.model_id}@{runtime.model_revision}"
                    )
                )
                return
        else:
            if catalog_id:
                entry = get_embedding_catalog_entry(catalog_id)
                if entry is None:
                    raise CommandError(f"Unknown embedding catalog ID: {catalog_id}")
            else:
                entry = get_embedding_catalog_entry_for_preset(preset or "balanced")

            if (
                not options.get("force_reembed")
                and active_runtime.catalog_id == entry.id
                and active_runtime.model_revision == entry.revision
            ):
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Embedding runtime is already active: {entry.id}@{entry.revision}"
                    )
                )
                return

            generation_id = (
                f"{options['scope']}-embedding-{entry.id}-"
                f"{entry.revision[:12]}-{int(time.time())}"
            )
            generation_dir = write_embedding_runtime_generation(
                scope=options["scope"],
                generation_id=generation_id,
                entry=entry,
            )
            runtime = load_embedding_runtime(
                path=generation_dir / "runtime.json",
                scope=options["scope"],
            )

        chunk_qs = (
            DocumentChunk.objects.select_related(
                "parse_result",
                "parse_result__node",
            )
            .filter(
                parse_result__status=AIStatus.COMPLETED,
                parse_result__node__trashed=False,
                parse_result__node__ai_processing_enabled=True,
            )
            .order_by("id")
        )
        expected_chunks = chunk_qs.count()
        if options.get("limit"):
            chunk_qs = chunk_qs[: options["limit"]]

        generation, _ = EmbeddingGeneration.objects.update_or_create(
            generation_id=generation_id,
            defaults={
                "scope": runtime.scope,
                "runtime_fingerprint": runtime.runtime_fingerprint,
                "catalog_id": runtime.catalog_id,
                "model_id": runtime.model_id,
                "model_revision": runtime.model_revision,
                "provider": runtime.provider,
                "store": runtime.store,
                "dimension": runtime.dimension,
                "supports_sparse": runtime.supports_sparse,
                "status": "PREPARING",
                "expected_chunks": expected_chunks,
                "completed_chunks": 0,
                "failed_chunks": 0,
            },
        )

        # provider proxies to embedding-executor's /embed when run outside it (see
        # RemoteBGEM3Provider) -- correct either way, but running this inside
        # embedding-executor avoids paying a per-chunk HTTP round trip for what
        # can be a large bulk re-embed.
        provider = get_embedding_provider(runtime=runtime)
        store = get_embedding_store_instance(runtime=runtime)
        try:
            probe = provider.embed_query(
                "dotori embedding runtime healthcheck",
                max_length=32,
            )
            store.validate_embedding(probe)
        except Exception as exc:
            generation.status = "FAILED"
            generation.failed_chunks = expected_chunks
            generation.save(
                update_fields=["status", "failed_chunks", "updated_at"]
            )
            raise CommandError(
                f"Candidate provider validation failed: {exc}"
            ) from exc

        if not options["activate"] and not options.get("skip_reembed"):
            sample_limit = options.get("limit") or 1
            checked = 0
            for chunk in chunk_qs[:sample_limit]:
                text = normalize_extracted_text(chunk.text or "")
                if not text:
                    continue
                store.validate_embedding(provider.embed_document(text))
                checked += 1
            generation.status = "VALIDATED"
            generation.save(update_fields=["status", "updated_at"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"Embedding runtime validated: {generation_id}; samples={checked}. "
                    "Run again with --activate for maintenance re-embedding."
                )
            )
            return

        if options.get("skip_reembed"):
            replace_active_hnsw_index(dimension=runtime.dimension)
            commit_active_embedding_runtime(options["scope"], generation_id)
            clear_embedding_runtime_cache()
            with transaction.atomic():
                EmbeddingGeneration.objects.filter(
                    scope=options["scope"],
                ).exclude(pk=generation.pk).update(status="RETIRED")
                target_parse_results = DocumentParseResult.objects.filter(
                    status=AIStatus.COMPLETED,
                    node__trashed=False,
                    node__ai_processing_enabled=True,
                    chunks__isnull=False,
                ).distinct()
                target_parse_results.update(
                    embedding_generation_id="",
                    embedding_runtime_fingerprint="",
                    updated_at=timezone.now(),
                )
                generation.status = "ACTIVE"
                generation.activated_at = timezone.now()
                generation.save(
                    update_fields=["status", "activated_at", "updated_at"]
                )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Activated embedding generation without re-embedding: {generation_id}. "
                    "Existing documents must be re-embedded before they can match search queries."
                )
            )
            return

        target_parse_results = DocumentParseResult.objects.filter(
            status=AIStatus.COMPLETED,
            node__trashed=False,
            node__ai_processing_enabled=True,
            chunks__isnull=False,
        ).distinct()
        target_parse_results.update(
            embedding_generation_id="",
            embedding_runtime_fingerprint="",
            updated_at=timezone.now(),
        )
        ChunkSegmentEmbedding.objects.all().delete()
        EmbeddingGeneration.objects.filter(
            generation_id=active_runtime.generation_id,
            status="ACTIVE",
        ).update(status="MAINTENANCE")

        generation.status = "EMBEDDING"
        generation.save(update_fields=["status", "updated_at"])
        completed = 0
        failed = 0
        for chunk in chunk_qs.iterator(chunk_size=100):
            text = normalize_extracted_text(chunk.text or "")
            if not text:
                failed += 1
                continue
            try:
                DocumentChunk.objects.filter(pk=chunk.pk).update(
                    status=AIStatus.PROCESSING,
                    error_message={},
                )
                embedding = provider.embed_document(text)
                store.save_chunk_embedding(
                    chunk=chunk,
                    embedding=embedding,
                    status=AIStatus.COMPLETED,
                )
                DocumentChunk.objects.filter(pk=chunk.pk).update(
                    status=AIStatus.COMPLETED,
                    error_message={},
                )
                completed += 1
            except Exception as exc:
                failed += 1
                store.mark_chunk_embedding_failed(
                    chunk=chunk,
                    error_message=str(exc)[:255],
                    status=AIStatus.FAILED,
                )
                DocumentChunk.objects.filter(pk=chunk.pk).update(
                    status=AIStatus.FAILED,
                    error_message={"message": str(exc)[:255]},
                )

            if (completed + failed) % 100 == 0:
                EmbeddingGeneration.objects.filter(pk=generation.pk).update(
                    completed_chunks=completed,
                    failed_chunks=failed,
                )
                self.stdout.write(
                    f"candidate={generation_id} completed={completed} "
                    f"failed={failed}"
                )

        generation.completed_chunks = completed
        generation.failed_chunks = failed
        full_coverage = failed == 0 and completed == expected_chunks
        generation.status = "READY" if full_coverage else "FAILED"
        generation.save(
            update_fields=[
                "completed_chunks",
                "failed_chunks",
                "status",
                "updated_at",
            ]
        )
        if not full_coverage:
            raise CommandError(
                "Maintenance re-embedding is incomplete; affected documents "
                "remain non-searchable until the command succeeds: "
                f"expected={expected_chunks} completed={completed} failed={failed}"
            )

        replace_active_hnsw_index(dimension=runtime.dimension)
        commit_active_embedding_runtime(options["scope"], generation_id)
        clear_embedding_runtime_cache()
        with transaction.atomic():
            previous_generation, _ = EmbeddingGeneration.objects.get_or_create(
                generation_id=active_runtime.generation_id,
                defaults={
                    "scope": active_runtime.scope,
                    "runtime_fingerprint": active_runtime.runtime_fingerprint,
                    "catalog_id": active_runtime.catalog_id,
                    "model_id": active_runtime.model_id,
                    "model_revision": active_runtime.model_revision,
                    "provider": active_runtime.provider,
                    "store": active_runtime.store,
                    "dimension": active_runtime.dimension,
                    "supports_sparse": active_runtime.supports_sparse,
                    "status": "ACTIVE",
                },
            )
            EmbeddingGeneration.objects.filter(
                scope=options["scope"],
            ).exclude(pk=generation.pk).update(status="RETIRED")
            target_parse_results.update(
                embedding_generation_id=generation_id,
                embedding_runtime_fingerprint=runtime.runtime_fingerprint,
                updated_at=timezone.now(),
            )
            generation.status = "ACTIVE"
            generation.activated_at = timezone.now()
            generation.save(
                update_fields=["status", "activated_at", "updated_at"]
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Activated embedding generation: {generation_id}. "
                "Restart app and embedding-executor before "
                "serving new requests."
            )
        )
        if runtime.max_tokens < getattr(active_runtime, "max_tokens", 8192):
            self.stdout.write(
                self.style.WARNING(
                    f"Notice: Active embedding max_tokens decreased from "
                    f"{getattr(active_runtime, 'max_tokens', 8192)} to {runtime.max_tokens}. "
                    "Run 'python manage.py reparse_documents' if existing chunks should "
                    "be re-segmented under the tighter limit."
                )
            )
