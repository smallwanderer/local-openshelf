from django.conf import settings
from django.db import models, transaction

from config.enums import NodeType, FileStatus, FileLanguage

import uuid
import os

# 파일 및 디렉토리의 가상 경로 객체
class Node(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="nodes"
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children"
    )

    uid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    ext = models.CharField(max_length=32)

    node_type = models.CharField(
        max_length=20,
        choices=NodeType.choices,
        default=NodeType.FILE
    )
    description = models.TextField(blank=True)

    # 파일의 가상 경로
    path = models.CharField(
        max_length=1024,
        db_index=True,
        default="/",
    )

    starred = models.BooleanField(default=False)
    trashed = models.BooleanField(default=False)
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
        if self.is_file and hasattr(self, 'blob'):
            return self.blob.mime_type
        return None

    @property
    def size_display(self):
        if self.is_file and hasattr(self, 'blob') and self.blob.size is not None:
            bytes_size = self.blob.size
            if bytes_size == 0:
                return '0 B'
            import math
            k = 1024
            sizes = ['B', 'KB', 'MB', 'GB', 'TB']
            i = int(math.floor(math.log(bytes_size) / math.log(k)))
            return f"{round(bytes_size / math.pow(k, i), 2)} {sizes[i]}"
        return "-"

    @property
    def get_status_display(self):
        if not self.is_file or not hasattr(self, 'blob'):
            return "업로드 준비"
            
        blob_status = self.blob.get_status_display()
        
        if hasattr(self, 'parse_result'):
            pr = self.parse_result
            if pr.status == "PENDING":
                return f"{blob_status} (AI 분석 대기중)"
            elif pr.status == "PROCESSING":
                return f"{blob_status} (AI 분석 중)"
            elif pr.status == "COMPLETED":
                return f"{blob_status} (AI 분석 완료)"
            elif pr.status == "FAILED":
                return f"{blob_status} (AI 분석 실패)"
        
        return blob_status

    def build_path(self):
        if self.parent:
            base = self.parent.path.rstrip("/")
            return f"{base}/{self.name}"
        return f"/{self.name}"
    
    def save(self, *args, **kwargs):
        if self.parent and self.parent.owner_id != self.owner_id:
            raise ValueError("다른 사용자의 폴더로 이동할 수 없습니다.")
        self.path = self.build_path()
        super().save(*args, **kwargs)
    
    def move(self, new_name=None, new_parent=None, to_root=False):
        target_name = new_name if new_name is not None else self.name
        if to_root:
            target_parent = None
        else:
            target_parent = new_parent if new_parent is not None else self.parent

        if target_parent and target_parent.owner_id != self.owner_id:
            raise ValueError("다른 사용자의 폴더로 이동할 수 없습니다.")

        if target_parent and target_parent.id == self.id:
            raise ValueError("자기 자신을 부모로 지정할 수 없습니다.")

        if target_parent and target_parent.path.startswith(self.path + "/"):
            raise ValueError("자기 자신의 하위 폴더로 이동할 수 없습니다.")

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
            raise ValueError(f"폴더 이동 중 오류 발생: {e}")

    def to_dict(self):
        data = {
            "id": self.id,
            "uid": str(self.uid) if hasattr(self, 'uid') else None,
            "name": self.name,
            "ext": self.ext,
            "node_type": self.node_type,
            "description": self.description,
            "path": self.path,
            "status": None,          # FileBlob이 있는 경우 아래에서 덮어씀
            "starred": self.starred,
            "trashed": self.trashed,
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
        return data

    def __str__(self):
        return f"{self.name} ({self.owner})"


# 실제 파일 저장 위치
def blob_upload_path(instance, filename):
    ext = os.path.splitext(filename)[1].lower()
    return f"blobs/user_{instance.node.owner_id}/{instance.uuid}{ext}"

class FileBlob(models.Model):
    node = models.OneToOneField(
        "files.Node", # Lazy Reference: 순환 참조 방지
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
        return round(self.size / 1024 / 1024, 2) # MB 단위 변환

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
    # Default: 1GB = 1024 * 1024 * 1024 bytes
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