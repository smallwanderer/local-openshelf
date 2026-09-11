from django.core.management.base import BaseCommand, CommandError
class Command(BaseCommand):
    help = "Explain the maintenance re-embedding requirement for rollback."

    def add_arguments(self, parser):
        parser.add_argument(
            "--scope",
            choices=("production",),
            default="production",
        )
        parser.add_argument("--generation-id")

    def handle(self, *args, **options):
        raise CommandError(
            "Chunk vectors now keep only the active embedding contract. "
            "A previous runtime cannot be activated by pointer rollback; "
            "restore a database backup or run a maintenance re-embedding "
            "for the desired model."
        )
