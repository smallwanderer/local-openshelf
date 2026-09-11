from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from document_ai.models import ExecutorJobReceipt
from document_ai.services.executor_jobs import fail_execution


class Command(BaseCommand):
    help = "Release a stranded receipt only after its executor has been stopped."

    def add_arguments(self, parser):
        parser.add_argument("job_id")
        parser.add_argument("--executor-stopped", action="store_true")

    def handle(self, *args, **options):
        if not options["executor_stopped"]:
            raise CommandError("Stop the owning executor first, then pass --executor-stopped.")
        with transaction.atomic():
            receipt = ExecutorJobReceipt.objects.select_for_update().get(job_id=options["job_id"])
            if receipt.status != "running":
                raise CommandError("Only a running receipt may be released.")
            fail_execution(receipt, code="MANUALLY_RELEASED", message="Operator confirmed executor stopped", retryable=True)
        self.stdout.write("Claim released. Restart executor and replay the same job ID or run state recovery.")
