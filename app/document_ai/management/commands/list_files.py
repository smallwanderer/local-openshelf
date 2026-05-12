from django.core.management.base import BaseCommand

from files.models import Node

class Command(BaseCommand):
    help = "List stored files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user-id",
            help="Filter files by owner id.",
        )
        parser.add_argument(
            "--name",
            help="Filter files by name containing this value.",
        )
        parser.add_argument(
            "--uid",
            help="Filter by exact node uid.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Maximum number of files to display.",
        )
        parser.add_argument(
            "--only-files",
            action="store_true",
            help="Show only file nodes.",
        )

    def handle(self, *args, **options):
        qs = Node.objects.select_related("owner").order_by("id")

        user_id = options.get("user_id")
        if user_id:
            qs = qs.filter(owner__id=user_id)

        name = options.get("name")
        if name:
            qs = qs.filter(name__icontains=name)

        uid = options.get("uid")
        if uid:
            qs = qs.filter(uid=uid)

        if options["only_files"]:
            # 프로젝트에서 파일/폴더 구분 필드명이 다르면 이 부분 수정 필요
            # 예: kind="file" / node_type="file" / is_dir=False 등
            qs = qs.filter(kind="file")

        limit = options.get("limit")
        if limit:
            qs = qs[:limit]

        nodes = list(qs)

        if not nodes:
            self.stdout.write(self.style.WARNING("No files found."))
            return

        header = (
            f"{'ID':<6} {'UID':<38} {'NAME':<50} "
            f"{'OWNER':<6} {'KIND':<10}"    
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for node in nodes:
            node_id = str(node.id)
            node_uid = str(getattr(node, "uid", "-"))
            node_name = getattr(node, "name", "") or "-"
            owner_id = getattr(getattr(node, "owner", None), "id", "-") or "-"
            kind = getattr(node, "kind", "-") or "-"

            self.stdout.write(
                f"{node_id:<6} {node_uid:<38} {node_name:<50} "
                f"{owner_id:<6} {kind:<10}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Total files: {len(nodes)}"))