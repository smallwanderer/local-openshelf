from django.contrib.auth.models import AbstractBaseUser
from django.core.files.base import File
from django.core.files.storage import default_storage
from django.http import FileResponse
from django.db import transaction

import dataclasses
import os

from ..models import FileBlob, Node, NodeType, UserStorage
from .utils import calculate_sha256, extract_ext

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".docx",
    ".xlsx",
    ".pptx",
    ".md",
    ".hwp",
    ".hwpx",
    ".png",
    ".jpg",
    ".jpeg",
    ".html",
}
MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB


@dataclasses.dataclass
class UploadValidationResult:
    ok: bool
    warnings: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    duplicate: bool = False


def validate_upload(owner: AbstractBaseUser, uploaded_file: File) -> UploadValidationResult:
    warnings = []
    errors = []

    if uploaded_file is None:
        return UploadValidationResult(
            ok=False,
            errors=["No file was provided."],
        )

    if uploaded_file.size > MAX_UPLOAD_SIZE:
        return UploadValidationResult(
            ok=False,
            errors=["The file exceeds the 2 GB upload limit."],
        )

    storage, _ = UserStorage.objects.get_or_create(user=owner)
    if storage.used_size + uploaded_file.size > storage.total_size:
        remaining_mb = round(storage.remaining_size / 1024 / 1024, 2)
        return UploadValidationResult(
            ok=False,
            errors=[f"Not enough storage space is available. Remaining: {remaining_mb} MB."],
        )

    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return UploadValidationResult(
            ok=False,
            errors=["This file type is not supported."],
        )

    content_type = getattr(uploaded_file, "content_type", None)
    if not content_type:
        warnings.append("The file MIME type could not be verified.")

    sha256 = calculate_sha256(uploaded_file)
    duplicate = FileBlob.objects.filter(node__owner=owner, sha256=sha256).exists()
    if duplicate:
        warnings.append("A file with the same content already exists.")

    return UploadValidationResult(
        ok=True,
        warnings=warnings,
        errors=errors,
        duplicate=duplicate,
    )


def save_file(owner: AbstractBaseUser, file: File, description: str, parent=None, ai_processing_enabled: bool = True) -> Node:
    sha256 = calculate_sha256(file)
    file.seek(0)

    with transaction.atomic():
        node = Node.objects.create(
            owner=owner,
            parent=parent,
            name=file.name,
            ext=extract_ext(file.name),
            node_type=NodeType.FILE,
            description=description,
            ai_processing_enabled=ai_processing_enabled,
        )

        FileBlob.objects.create(
            node=node,
            file=file,
            original_name=file.name,
            size=file.size,
            mime_type=getattr(file, "content_type", ""),
            sha256=sha256,
        )

    return node


def _coerce_node(file_or_node) -> Node:
    if isinstance(file_or_node, Node):
        node = file_or_node
    elif isinstance(file_or_node, FileBlob):
        node = file_or_node.node
    else:
        raise TypeError("Expected a Node or FileBlob instance.")

    if node.node_type != NodeType.FILE:
        raise ValueError("The target node is not a file.")
    if not hasattr(node, "blob"):
        raise ValueError("The file node does not have a blob attached.")

    return node


def delete_file(file_or_node):
    node = _coerce_node(file_or_node)
    blob = node.blob
    file_name = blob.file.name if blob.file else ""

    with transaction.atomic():
        if file_name and default_storage.exists(file_name):
            default_storage.delete(file_name)
        node.delete()


def open_file(file_or_node, mode: str = "rb"):
    node = _coerce_node(file_or_node)
    if not node.blob.file:
        raise FileNotFoundError("The file blob is missing from storage.")
    return node.blob.file.open(mode)


def get_download_response(file_or_node):
    node = _coerce_node(file_or_node)
    return FileResponse(
        open_file(node),
        as_attachment=True,
        filename=node.blob.original_name,
    )


def get_file(file_or_node):
    return _coerce_node(file_or_node)


def get_files(user):
    return Node.objects.filter(owner=user, node_type=NodeType.FILE, trashed=False).order_by("-created_at")
