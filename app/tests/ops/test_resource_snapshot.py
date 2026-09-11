import pytest
from django.core.management import call_command
from django.test import TestCase

from document_ai.models import ResourceSnapshot

pytestmark = pytest.mark.integration


class CollectResourceSnapshotTests(TestCase):
    def test_records_db_connection_count_and_disk_free_space(self):
        call_command("collect_resource_snapshot")

        db_row = ResourceSnapshot.objects.get(service="db")
        self.assertIsNotNone(db_row.db_connections)
        self.assertGreaterEqual(db_row.db_connections, 1)
        self.assertIsNone(db_row.disk_free_mb)
        self.assertIsNone(db_row.cpu_percent)

        disk_services = set(
            ResourceSnapshot.objects.filter(service__startswith="disk:").values_list(
                "service", flat=True
            )
        )
        self.assertEqual(disk_services, {"disk:uploads", "disk:logs", "disk:config"})
        for row in ResourceSnapshot.objects.filter(service__startswith="disk:"):
            self.assertIsNotNone(row.disk_free_mb)
            self.assertGreater(row.disk_free_mb, 0)
            self.assertIsNone(row.db_connections)

    def test_missing_disk_path_is_skipped_not_fatal(self):
        from django.test import override_settings

        with override_settings(MEDIA_ROOT="/nonexistent/path/for/test"):
            call_command("collect_resource_snapshot")

        self.assertFalse(ResourceSnapshot.objects.filter(service="disk:uploads").exists())
        self.assertTrue(ResourceSnapshot.objects.filter(service="disk:logs").exists())
