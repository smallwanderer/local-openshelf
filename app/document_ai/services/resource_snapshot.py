from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import connection

from document_ai.models import ResourceSnapshot


@dataclass(frozen=True)
class ResourceSnapshotCollection:
    rows: list[ResourceSnapshot]
    skipped: list[dict]


def _disk_paths() -> dict[str, Path]:
    return {
        "disk:uploads": Path(settings.MEDIA_ROOT),
        "disk:logs": Path(settings.LOG_DIR),
        "disk:config": settings.BASE_DIR.parent / "data" / "config",
    }


def collect_resource_snapshots() -> ResourceSnapshotCollection:
    rows: list[ResourceSnapshot] = []
    skipped: list[dict] = []

    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            )
            count = cursor.fetchone()[0]
        rows.append(ResourceSnapshot.objects.create(service="db", db_connections=count))
    else:
        skipped.append({"service": "db", "reason": "not_postgresql"})

    for service, path in _disk_paths().items():
        if not path.exists():
            skipped.append({"service": service, "reason": "path_not_found"})
            continue
        usage = shutil.disk_usage(path)
        rows.append(
            ResourceSnapshot.objects.create(
                service=service,
                disk_free_mb=round(usage.free / (1024 * 1024), 1),
            )
        )

    return ResourceSnapshotCollection(rows=rows, skipped=skipped)


def latest_resource_snapshots() -> list[ResourceSnapshot]:
    latest: dict[str, ResourceSnapshot] = {}
    for snapshot in ResourceSnapshot.objects.order_by("-collected_at"):
        latest.setdefault(snapshot.service, snapshot)
    return list(latest.values())


def serialize_resource_snapshot(snapshot: ResourceSnapshot) -> dict:
    return {
        "service": snapshot.service,
        "cpu_percent": snapshot.cpu_percent,
        "mem_mb": snapshot.mem_mb,
        "gpu_mem_mb": snapshot.gpu_mem_mb,
        "db_connections": snapshot.db_connections,
        "disk_free_mb": snapshot.disk_free_mb,
        "collected_at": snapshot.collected_at.isoformat(),
    }
