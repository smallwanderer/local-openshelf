from django.core.management.base import BaseCommand, CommandError

from document_ai.embedding.indexes import replace_active_hnsw_index
from document_ai.models import EmbeddingGeneration
from document_ai.services.embedding_runtime_config import load_embedding_runtime

from llm_installation.embedding_catalog import (
    get_embedding_catalog_entry_for_preset,
)
from llm_installation.embedding_config_store import (
    commit_active_embedding_runtime,
    get_embedding_runtime_config_path,
    write_embedding_runtime_generation,
)


class Command(BaseCommand):
    help = "Resolve a verified embedding catalog entry for this server."

    def add_arguments(self, parser):
        parser.add_argument(
            "--preset",
            choices=("speed", "balanced", "quality"),
            default="balanced",
        )
        parser.add_argument(
            "--scope",
            choices=("production",),
            default="production",
        )
        parser.add_argument(
            "--write",
            action="store_true",
            help="Persist and activate the resolved initial runtime.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Replace an existing runtime pointer. Intended for recovery.",
        )

    def handle(self, *args, **options):
        entry = get_embedding_catalog_entry_for_preset(options["preset"])
        self.stdout.write(
            f"resolved={entry.id} model={entry.repo_id}@{entry.revision} "
            f"provider={entry.provider} store={entry.store}"
        )
        if not options["write"]:
            return

        active_path = get_embedding_runtime_config_path(options["scope"])
        if active_path.exists() and not options["force"]:
            raise CommandError(
                f"Embedding runtime already exists: {active_path}. "
                "Use change_embedding_runtime for a model transition or "
                "--force for explicit recovery."
            )

        generation_id = (
            f"{options['scope']}-embedding-{entry.id}-{entry.revision[:12]}"
        )
        write_embedding_runtime_generation(
            scope=options["scope"],
            generation_id=generation_id,
            entry=entry,
        )
        commit_active_embedding_runtime(options["scope"], generation_id)
        runtime = load_embedding_runtime(scope=options["scope"])
        EmbeddingGeneration.objects.update_or_create(
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
                "status": "ACTIVE",
            },
        )
        replace_active_hnsw_index(dimension=runtime.dimension)
        self.stdout.write(
            self.style.SUCCESS(
                f"Activated embedding runtime generation: {generation_id}"
            )
        )
