from django.core.management.base import BaseCommand

from document_ai.processing.recovery import (
    get_embedding_recovery_chunk_ids,
    get_parse_recovery_node_ids,
    get_recovery_embedding_batch_size,
    get_recovery_parse_batch_size,
    recover_document_pipeline_backlog_sync,
)


class Command(BaseCommand):
    help = "Inspect or repair stale document parsing and embedding work once."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--dry-run", action="store_true", help="Show repair candidates without queueing work.")
        mode.add_argument("--apply", action="store_true", help="Queue repair work for current candidates.")

    def handle(self, *args, **options):
        if options["dry_run"]:
            parse_ids = get_parse_recovery_node_ids(get_recovery_parse_batch_size())
            chunk_ids = get_embedding_recovery_chunk_ids(
                get_recovery_embedding_batch_size()
            )
            self.stdout.write(
                self.style.WARNING(
                    f"repair candidates: parse_nodes={len(parse_ids)} embedding_chunks={len(chunk_ids)}"
                )
            )
            return

        result = recover_document_pipeline_backlog_sync()
        self.stdout.write(self.style.SUCCESS(f"repair queued: {result}"))
