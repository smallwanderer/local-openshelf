import json

from django.core.management.base import BaseCommand

from document_ai.models import EmbeddingGeneration
from document_ai.services.embedding_runtime_config import load_embedding_runtime


class Command(BaseCommand):
    help = "Inspect the persisted server embedding runtime and generation state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--scope",
            choices=("production",),
            default=None,
        )
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **options):
        runtime = load_embedding_runtime(scope=options["scope"])
        generation = EmbeddingGeneration.objects.filter(
            generation_id=runtime.generation_id
        ).first()
        payload = runtime.model_dump(mode="json")
        payload["database_generation"] = (
            {
                "status": generation.status,
                "expected_chunks": generation.expected_chunks,
                "completed_chunks": generation.completed_chunks,
                "failed_chunks": generation.failed_chunks,
                "activated_at": (
                    generation.activated_at.isoformat()
                    if generation.activated_at
                    else None
                ),
            }
            if generation
            else None
        )
        if options["as_json"]:
            self.stdout.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            )
            return
        for key, value in payload.items():
            self.stdout.write(f"{key}: {value}")
