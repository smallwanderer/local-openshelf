import os
import uuid
from datetime import timedelta
import math

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from config.enums import AIStatus, FileLanguage, FileStatus, NodeType


class Node(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="nodes",
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
    )

    uid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    ext = models.CharField(max_length=32)

    node_type = models.CharField(
        max_length=20,
        choices=NodeType.choices,
        default=NodeType.FILE,
    )
    description = models.TextField(blank=True)
    path = models.CharField(max_length=1024, db_index=True, default="/")

    starred = models.BooleanField(default=False)
    trashed = models.BooleanField(default=False)
    ai_processing_enabled = models.BooleanField(default=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "parent"]),
            models.Index(fields=["owner", "trashed"]),
            models.Index(fields=["owner", "node_type"]),
            models.Index(fields=["owner", "-created_at"]),
            models.Index(fields=["owner", "path"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "path"],
                name="uniq_node_path_per_owner",
            )
        ]

    @property
    def is_file(self):
        return self.node_type == NodeType.FILE

    @property
    def is_directory(self):
        return self.node_type == NodeType.FOLDER

    @property
    def mime_type(self):
        if self.is_file and hasattr(self, "blob"):
            return self.blob.mime_type
        return None

    @property
    def size_display(self):
        if self.is_file and hasattr(self, "blob") and self.blob.size is not None:
            bytes_size = self.blob.size
            if bytes_size == 0:
                return "0 B"
            import math

            k = 1024
            sizes = ["B", "KB", "MB", "GB", "TB"]
            i = int(math.floor(math.log(bytes_size) / math.log(k)))
            return f"{round(bytes_size / math.pow(k, i), 2)} {sizes[i]}"
        return "-"

    @property
    def get_status_display(self):
        if not self.is_file or not hasattr(self, "blob"):
            return "Pending upload"

        blob_status = self.blob.get_status_display()

        ai_status = self.get_ai_status()
        if ai_status:
            parse_status = ai_status["parse_status"]
            embedding_status = ai_status["embedding_status"]

            if parse_status == AIStatus.PENDING:
                return f"{blob_status} (Parsing queued)"
            if parse_status == AIStatus.PROCESSING:
                return f"{blob_status} (Parsing in progress)"
            if parse_status == AIStatus.FAILED:
                return f"{blob_status} (Parsing failed)"

            if embedding_status == AIStatus.PENDING:
                return f"{blob_status} (Embedding queued)"
            if embedding_status == AIStatus.PROCESSING:
                return f"{blob_status} (Embedding in progress)"
            if embedding_status == AIStatus.FAILED:
                return f"{blob_status} (Embedding failed)"
            if embedding_status == AIStatus.COMPLETED:
                return f"{blob_status} (Embedding completed)"

        return blob_status

    def get_ai_status(self):
        if not self.is_file:
            return None

        parse_status = AIStatus.PENDING
        parse_label = "Parsing queued"
        embedding_status = AIStatus.PENDING
        embedding_label = "Waiting for parsing"
        chunk_count = 0
        completed_chunks = 0
        processing_chunks = 0
        pending_chunks = 0
        failed_chunks = 0
        embedding_backend = None

        if not hasattr(self, "parse_result"):
            return {
                "parse_status": parse_status,
                "parse_label": parse_label,
                "embedding_status": embedding_status,
                "embedding_label": embedding_label,
                "embedding_completed": False,
                "chunk_count": chunk_count,
                "completed_chunks": completed_chunks,
                "processing_chunks": processing_chunks,
                "pending_chunks": pending_chunks,
                "failed_chunks": failed_chunks,
                "embedding_backend": embedding_backend,
            }

        parse_result = self.parse_result
        parse_status = parse_result.status
        embedding_backend = (parse_result.metadata or {}).get("embedding_backend")
        chunk_count = parse_result.chunk_count or 0

        parse_label_map = {
            AIStatus.PENDING: "Parsing queued",
            AIStatus.PROCESSING: "Parsing in progress",
            AIStatus.COMPLETED: "Parsing completed",
            AIStatus.FAILED: "Parsing failed",
        }
        parse_label = parse_label_map.get(parse_status, "Parsing queued")

        if parse_status == AIStatus.FAILED:
            embedding_status = AIStatus.FAILED
            embedding_label = "Embedding unavailable because parsing failed"
        elif parse_status in {AIStatus.PENDING, AIStatus.PROCESSING}:
            embedding_status = AIStatus.PENDING
            embedding_label = "Waiting for parsing"
        else:
            prefetched_chunks = getattr(parse_result, "_prefetched_objects_cache", {}).get("chunks")
            if prefetched_chunks is not None:
                chunk_count = len(prefetched_chunks) or chunk_count
                completed_chunks = sum(1 for chunk in prefetched_chunks if chunk.status == AIStatus.COMPLETED)
                processing_chunks = sum(1 for chunk in prefetched_chunks if chunk.status == AIStatus.PROCESSING)
                pending_chunks = sum(1 for chunk in prefetched_chunks if chunk.status == AIStatus.PENDING)
                failed_chunks = sum(1 for chunk in prefetched_chunks if chunk.status == AIStatus.FAILED)
            else:
                chunk_counts = parse_result.chunks.aggregate(
                    total=models.Count("id"),
                    completed=models.Count("id", filter=models.Q(status=AIStatus.COMPLETED)),
                    processing=models.Count("id", filter=models.Q(status=AIStatus.PROCESSING)),
                    pending=models.Count("id", filter=models.Q(status=AIStatus.PENDING)),
                    failed=models.Count("id", filter=models.Q(status=AIStatus.FAILED)),
                )

                chunk_count = chunk_counts["total"] or chunk_count
                completed_chunks = chunk_counts["completed"] or 0
                processing_chunks = chunk_counts["processing"] or 0
                pending_chunks = chunk_counts["pending"] or 0
                failed_chunks = chunk_counts["failed"] or 0

            if chunk_count == 0:
                embedding_status = AIStatus.PENDING
                embedding_label = "No chunks available for embedding"
            elif completed_chunks == chunk_count:
                embedding_status = AIStatus.COMPLETED
                embedding_label = f"Embedding completed ({completed_chunks}/{chunk_count} chunks)"
            elif failed_chunks > 0 and completed_chunks + failed_chunks == chunk_count:
                embedding_status = AIStatus.FAILED
                embedding_label = (
                    f"Embedding finished with failures ({completed_chunks}/{chunk_count} chunks completed)"
                )
            elif processing_chunks > 0 or completed_chunks > 0 or failed_chunks > 0:
                embedding_status = AIStatus.PROCESSING
                embedding_label = f"Embedding in progress ({completed_chunks}/{chunk_count} chunks completed)"
            else:
                embedding_status = AIStatus.PENDING
                embedding_label = f"Embedding queued ({pending_chunks}/{chunk_count} chunks pending)"

        return {
            "parse_status": parse_status,
            "parse_label": parse_label,
            "embedding_status": embedding_status,
            "embedding_label": embedding_label,
            "embedding_completed": embedding_status == AIStatus.COMPLETED,
            "chunk_count": chunk_count,
            "completed_chunks": completed_chunks,
            "processing_chunks": processing_chunks,
            "pending_chunks": pending_chunks,
            "failed_chunks": failed_chunks,
            "embedding_backend": embedding_backend,
        }

    @property
    def trash_expires_at(self):
        if not self.deleted_at:
            return None
        retention_days = getattr(settings, "TRASH_RETENTION_DAYS", 7)
        return self.deleted_at + timedelta(days=retention_days)

    @property
    def days_until_purge(self):
        expires_at = self.trash_expires_at
        if not expires_at:
            return None
        seconds_remaining = (expires_at - timezone.now()).total_seconds()
        return max(0, math.ceil(seconds_remaining / 86400))

    def build_path(self):
        if self.parent:
            base = self.parent.path.rstrip("/")
            return f"{base}/{self.name}"
        return f"/{self.name}"

    def save(self, *args, **kwargs):
        if self.parent and self.parent.owner_id != self.owner_id:
            raise ValueError("You cannot move an item into another user's folder.")
        self.path = self.build_path()
        super().save(*args, **kwargs)

    def move(self, new_name=None, new_parent=None, to_root=False):
        target_name = new_name if new_name is not None else self.name
        if to_root:
            target_parent = None
        else:
            target_parent = new_parent if new_parent is not None else self.parent

        if target_parent and target_parent.owner_id != self.owner_id:
            raise ValueError("You cannot move an item into another user's folder.")

        if target_parent and target_parent.id == self.id:
            raise ValueError("A folder cannot be its own parent.")

        if target_parent and target_parent.path.startswith(self.path + "/"):
            raise ValueError("A folder cannot be moved into one of its descendants.")

        old_path = self.path
        old_name = self.name
        old_parent = self.parent

        self.name = target_name
        self.parent = target_parent
        new_path = self.build_path()

        try:
            with transaction.atomic():
                if self.node_type == NodeType.FOLDER and old_path != new_path:
                    Node.objects.filter(
                        owner=self.owner,
                        path__startswith=old_path + "/",
                    ).update(
                        path=models.functions.Replace(
                            "path",
                            models.Value(old_path + "/"),
                            models.Value(new_path + "/"),
                        )
                    )

                self.path = new_path
                self.save()
        except Exception as e:
            self.name = old_name
            self.parent = old_parent
            self.path = old_path
            raise ValueError(f"Failed to move the item: {e}")

    def to_dict(self):
        data = {
            "id": self.id,
            "uid": str(self.uid) if hasattr(self, "uid") else None,
            "name": self.name,
            "ext": self.ext,
            "node_type": self.node_type,
            "description": self.description,
            "path": self.path,
            "status": None,
            "starred": self.starred,
            "trashed": self.trashed,
            "ai_processing_enabled": self.ai_processing_enabled,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "restore_until": self.trash_expires_at.isoformat() if self.trash_expires_at else None,
            "days_until_purge": self.days_until_purge,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "parent_id": self.parent_id,
        }
        if self.is_file and hasattr(self, "blob"):
            data["status"] = self.blob.status
            data["size"] = self.blob.size
            data["size_mb"] = self.blob.size_mb()
            data["mime_type"] = self.blob.mime_type
            data["language"] = self.blob.language
            data["ai_status"] = self.get_ai_status()
        return data

    def __str__(self):
        return f"{self.name} ({self.owner})"


def blob_upload_path(instance, filename):
    ext = os.path.splitext(filename)[1].lower()
    return f"blobs/user_{instance.node.owner_id}/{instance.uuid}{ext}"


class FileBlob(models.Model):
    node = models.OneToOneField(
        "files.Node",
        on_delete=models.CASCADE,
        related_name="blob",
    )
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    file = models.FileField(upload_to=blob_upload_path)
    original_name = models.CharField(max_length=255)

    language = models.CharField(
        max_length=32,
        choices=FileLanguage.choices,
        default=FileLanguage.ENGLISH,
    )

    status = models.CharField(
        max_length=20,
        choices=FileStatus.choices,
        default=FileStatus.UPLOADED,
        db_index=True,
    )

    mime_type = models.CharField(max_length=100, blank=True)
    size = models.BigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, null=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["sha256"]),
            models.Index(fields=["status"]),
        ]

    def size_mb(self):
        if self.size is None:
            return None
        return round(self.size / 1024 / 1024, 2)

    def to_dict(self):
        return {
            "id": self.id,
            "uuid": str(self.uuid),
            "original_name": self.original_name,
            "language": self.language,
            "mime_type": self.mime_type,
            "size": self.size,
            "size_mb": self.size_mb(),
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }

    def __str__(self):
        return f"{self.original_name}"


class UserStorage(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="storage",
    )
    total_size = models.BigIntegerField(default=1073741824)
    used_size = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def remaining_size(self):
        return max(self.total_size - self.used_size, 0)

    @property
    def usage_percent(self):
        if self.total_size == 0:
            return 100
        return min(round((self.used_size / self.total_size) * 100, 1), 100)

    def used_size_mb(self):
        return round(self.used_size / 1024 / 1024, 2)

    def total_size_gb(self):
        return round(self.total_size / 1024 / 1024 / 1024, 2)

    def __str__(self):
        return f"{self.user.email} - {self.used_size_mb()}MB / {self.total_size_gb()}GB"
