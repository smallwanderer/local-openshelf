from django.core.management.base import BaseCommand

from document_ai.services.resource_snapshot import collect_resource_snapshots


class Command(BaseCommand):
    help = (
        "Take a one-off snapshot of DB connection count and disk free space "
        "(data/uploads, data/logs, data/config). No cron/scheduling - run "
        "manually, typically before/during/after a load test to check for "
        "resource exhaustion. Per-container CPU/RAM/GPU is out of scope until "
        "docker.sock is mounted into this container."
    )

    def handle(self, *args, **options):
        result = collect_resource_snapshots()
        for snapshot in result.rows:
            self.stdout.write(self.style.SUCCESS(f"{snapshot.service}: collected"))
        for skipped in result.skipped:
            self.stdout.write(
                self.style.WARNING(
                    f"{skipped['service']}: skipped ({skipped['reason']})"
                )
            )
