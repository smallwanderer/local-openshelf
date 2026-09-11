import os
import uuid
from datetime import timedelta
import math

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from config.enums import AIStatus, FileLanguage, FileOperation, FileStatus, NodeType


class Node(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="owned_nodes",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
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
            models.Index(fields=["workspace", "parent"], name="node_workspace_parent_idx"),
            models.Index(fields=["workspace", "trashed"], name="node_workspace_trashed_idx"),
            models.Index(fields=["workspace", "node_type"], name="node_workspace_type_idx"),
            models.Index(fields=["workspace", "-created_at"], name="node_workspace_created_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "path"],
                name="uniq_node_path_per_workspace",
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

        from document_ai.services.embedding_runtime_config import (
            EmbeddingRuntimeConfigError,
            get_active_embedding_runtime,
        )

        try:
            active_runtime = get_active_embedding_runtime()
            active_generation_id = active_runtime.generation_id
            active_runtime_fingerprint = active_runtime.runtime_fingerprint
            runtime_configured = True
        except EmbeddingRuntimeConfigError:
            active_generation_id = ""
            active_runtime_fingerprint = ""
            runtime_configured = False

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
        embedding_contract_status = "missing"
        embedded_generation_ids = []

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
                "embedding_contract_status": embedding_contract_status,
                "active_embedding_generation_id": active_generation_id,
                "active_embedding_runtime_fingerprint": active_runtime_fingerprint,
                "embedded_generation_ids": embedded_generation_ids,
                "reembedding_required": False,
                "searchable": False,
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
                processing_chunks = sum(1 for chunk in prefetched_chunks if chunk.status == AIStatus.PROCESSING)
                pending_chunks = sum(1 for chunk in prefetched_chunks if chunk.status == AIStatus.PENDING)
                chunk_failed_count = sum(1 for chunk in prefetched_chunks if chunk.status == AIStatus.FAILED)
                chunk_completed_count = sum(1 for chunk in prefetched_chunks if chunk.status == AIStatus.COMPLETED)
            else:
                chunk_counts = parse_result.chunks.aggregate(
                    total=models.Count("id"),
                    completed=models.Count("id", filter=models.Q(status=AIStatus.COMPLETED)),
                    processing=models.Count("id", filter=models.Q(status=AIStatus.PROCESSING)),
                    pending=models.Count("id", filter=models.Q(status=AIStatus.PENDING)),
                    failed=models.Count("id", filter=models.Q(status=AIStatus.FAILED)),
                )

                chunk_count = chunk_counts["total"] or chunk_count
                processing_chunks = chunk_counts["processing"] or 0
                pending_chunks = chunk_counts["pending"] or 0
                chunk_failed_count = chunk_counts["failed"] or 0
                chunk_completed_count = chunk_counts["completed"] or 0

            stored_generation_id = parse_result.embedding_generation_id
            stored_fingerprint = parse_result.embedding_runtime_fingerprint
            embedded_generation_ids = [stored_generation_id] if stored_generation_id else []
            contract_matches = (
                stored_generation_id == active_generation_id
                and (
                    not stored_fingerprint
                    or stored_fingerprint == active_runtime_fingerprint
                )
            )
            completed_chunks = chunk_completed_count if contract_matches else 0
            failed_chunks = chunk_failed_count if contract_matches else 0

            if chunk_count == 0:
                embedding_status = AIStatus.PENDING
                embedding_label = "No chunks available for embedding"
                embedding_contract_status = "missing"
            elif not runtime_configured:
                embedding_status = AIStatus.FAILED
                embedding_label = "Active embedding contract is invalid"
                embedding_contract_status = "invalid_config"
            elif contract_matches and completed_chunks == chunk_count:
                embedding_status = AIStatus.COMPLETED
                embedding_label = f"Embedding completed ({completed_chunks}/{chunk_count} chunks)"
                embedding_contract_status = "current"
            elif contract_matches and failed_chunks > 0 and completed_chunks + failed_chunks == chunk_count:
                embedding_status = AIStatus.FAILED
                embedding_label = (
                    f"Embedding finished with failures ({completed_chunks}/{chunk_count} chunks completed)"
                )
                embedding_contract_status = "failed"
            elif processing_chunks > 0 or pending_chunks > 0:
                embedding_status = AIStatus.PROCESSING
                embedding_label = f"Embedding in progress ({completed_chunks}/{chunk_count} chunks completed)"
                embedding_contract_status = "reembedding"
            elif stored_generation_id and not contract_matches:
                embedding_status = AIStatus.FAILED
                embedding_label = "Embedding contract is outdated; re-embedding is required"
                embedding_contract_status = "stale_contract"
            elif chunk_failed_count > 0:
                embedding_status = AIStatus.FAILED
                embedding_label = "Embedding failed"
                embedding_contract_status = "failed"
            else:
                embedding_status = AIStatus.PENDING
                embedding_label = f"Embedding queued ({pending_chunks}/{chunk_count} chunks pending)"
                embedding_contract_status = "missing"

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
            "embedding_contract_status": embedding_contract_status,
            "active_embedding_generation_id": active_generation_id,
            "active_embedding_runtime_fingerprint": active_runtime_fingerprint,
            "embedded_generation_ids": embedded_generation_ids,
            "reembedding_required": embedding_contract_status
            in {"stale_contract", "failed", "missing"}
            and parse_status == AIStatus.COMPLETED
            and chunk_count > 0,
            "searchable": embedding_contract_status == "current",
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
        if not self.workspace_id:
            if self.parent_id:
                self.workspace_id = self.parent.workspace_id
            elif self.owner_id:
                from workspaces.models import WorkspaceMembership

                self.workspace_id = WorkspaceMembership.objects.filter(
                    user_id=self.owner_id,
                    status=WorkspaceMembership.STATUS_ACTIVE,
                    workspace__kind="personal",
                ).values_list("workspace_id", flat=True).first()
        if not self.workspace_id:
            raise ValueError("Node requires a workspace.")
        if self.parent and self.parent.workspace_id != self.workspace_id:
            raise ValueError("You cannot move an item into another workspace's folder.")
        self.path = self.build_path()
        super().save(*args, **kwargs)

    def move(self, new_name=None, new_parent=None, to_root=False):
        target_name = new_name if new_name is not None else self.name
        if to_root:
            target_parent = None
        else:
            target_parent = new_parent if new_parent is not None else self.parent

        if target_parent and target_parent.workspace_id != self.workspace_id:
            raise ValueError("You cannot move an item into another workspace's folder.")

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
                        workspace=self.workspace,
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

    def to_dict(self, *, include_ai_meta=False):
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
            "parent_uid": str(self.parent.uid) if self.parent_id and self.parent else None,
        }
        if self.is_file and hasattr(self, "blob"):
            data["status"] = self.blob.status
            data["size"] = self.blob.size
            data["size_mb"] = self.blob.size_mb()
            data["mime_type"] = self.blob.mime_type
            data["language"] = self.blob.language
            data["ai_status"] = self.get_ai_status()
        if self.is_file and include_ai_meta:
            parse_result = getattr(self, "parse_result", None)
            data["summary"] = parse_result.summary if parse_result else ""
            data["auto_tags"] = list(parse_result.auto_tags or []) if parse_result else []
        return data

    def __str__(self):
        return f"{self.name} ({self.owner or 'deleted user'})"


def blob_upload_path(instance, filename):
    ext = os.path.splitext(filename)[1].lower()
    workspace_id = instance.node.workspace_id or "orphan"
    return f"blobs/workspace_{workspace_id}/{instance.uuid}{ext}"


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


class FileOperationLog(models.Model):
    """One row per file-management API request (upload, rename, move, delete,
    restore, ...), mirroring document_ai's SearchJob/RAGJob pattern so
    response time can be queried the same way across operation types (see
    dev-docs/evaluation/performance-and-reliability.md, 파일 입출력 계측).

    `node` is null for operations that don't resolve to a single surviving
    node (bulk calls, permanent deletion, empty trash, or requests that
    failed before a node was found)."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="file_operation_logs",
    )
    node = models.ForeignKey(
        "files.Node",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operation_logs",
    )

    operation = models.CharField(max_length=32, choices=FileOperation.choices, db_index=True)
    # Operation-specific context that doesn't warrant its own column, e.g.
    # {"old_name", "new_name"} for rename, {"count"} for bulk operations.
    detail = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=32,
        choices=AIStatus.choices,
        default=AIStatus.COMPLETED,
        db_index=True,
    )
    error_message = models.TextField(blank=True)
    performance_metrics = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["owner", "-created_at"]),
            models.Index(fields=["operation", "-created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"FileOperationLog({self.id}) {self.operation}/{self.status}"


class UserStorage(models.Model):
    # Kept only for the legacy admin display during this migration phase.
    # Quota ownership and all accounting use `workspace` exclusively.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="legacy_storage_records",
    )
    workspace = models.OneToOneField(
        "workspaces.Workspace",
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
        return f"{self.workspace.name} - {self.used_size_mb()}MB / {self.total_size_gb()}GB"
