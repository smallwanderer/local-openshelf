from django.core.management.base import BaseCommand

from document_ai.models import ChunkSegmentEmbedding


class Command(BaseCommand):
    help = "Delete lazy contextual compression segment embeddings."

    def add_arguments(self, parser):
        parser.add_argument(
            "--window-size",
            type=int,
            default=None,
            help="Delete only segment embeddings for this window size.",
        )

    def handle(self, *args, **options):
        qs = ChunkSegmentEmbedding.objects.all()
        window_size = options.get("window_size")
        if window_size is not None:
            qs = qs.filter(window_size=window_size)

        count, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {count} segment embedding rows."))
