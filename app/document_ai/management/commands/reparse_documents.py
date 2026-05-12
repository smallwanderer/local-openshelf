from django.core.management.base import BaseCommand

from config.enums import NodeType
from document_ai.task import parse_document_with_docling
from files.models import Node


class Command(BaseCommand):
    help = "Reparse existing file documents and regenerate chunks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user-email",
            help="Only reparse documents owned by the given user email.",
        )
        parser.add_argument(
            "--node-id",
            type=int,
            action="append",
            dest="node_ids",
            help="Only reparse the specified node id. Repeat to pass multiple ids.",
        )
        parser.add_argument(
            "--uid",
            action="append",
            dest="uids",
            help="Only reparse the specified node uid. Repeat to pass multiple uids.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Maximum number of documents to process.",
        )
        parser.add_argument(
            "--queue",
            action="store_true",
            help="Queue parse tasks instead of running them inline.",
        )
        parser.add_argument(
            "--include-trashed",
            action="store_true",
            help="Include trashed file nodes.",
        )

    def handle(self, *args, **options):
        qs = (
            Node.objects.select_related("owner", "blob")
            .filter(node_type=NodeType.FILE, blob__isnull=False)
            .order_by("id")
        )

        if not options["include_trashed"]:
            qs = qs.filter(trashed=False)

        user_email = options.get("user_email")
        if user_email:
            qs = qs.filter(owner__email=user_email)

        node_ids = options.get("node_ids") or []
        if node_ids:
            qs = qs.filter(id__in=node_ids)

        uids = options.get("uids") or []
        if uids:
            qs = qs.filter(uid__in=uids)

        limit = options.get("limit")
        if limit:
            qs = qs[:limit]

        nodes = list(qs)
        total = len(nodes)
        if total == 0:
            self.stdout.write(self.style.WARNING("No documents found to reparse."))
            return

        mode = "queued" if options["queue"] else "inline"
        self.stdout.write(f"Reparsing {total} document(s) in {mode} mode.")

        success_count = 0
        failed_count = 0

        for index, node in enumerate(nodes, start=1):
            label = f"[{index}/{total}] node_id={node.id} name={node.name}"
            try:
                if options["queue"]:
                    parse_document_with_docling.delay(node.id)
                    self.stdout.write(f"{label} queued")
                else:
                    result = parse_document_with_docling(node.id)
                    if result.get("status") == "success":
                        self.stdout.write(
                            f"{label} reparsed (chunk_count={result.get('chunk_count', 0)})"
                        )
                    else:
                        raise RuntimeError(result.get("error") or "parse task failed")

                success_count += 1
            except Exception as exc:
                failed_count += 1
                self.stdout.write(
                    self.style.ERROR(f"{label} failed: {exc}")
                )

        summary = f"Completed reparse. success={success_count} failed={failed_count}"
        if failed_count:
            self.stdout.write(self.style.WARNING(summary))
            return

        self.stdout.write(self.style.SUCCESS(summary))
