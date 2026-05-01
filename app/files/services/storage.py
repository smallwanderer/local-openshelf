from django.core.files.storage import default_storage
from django.core.files.base import File
from django.contrib.auth.models import AbstractBaseUser

import dataclasses
import os

from ..models import Node, FileBlob, NodeType
from django.db import transaction
from .utils import *

ALLOWED_EXTENSIONS = {
    # 문서
    ".pdf", ".txt", ".docx", ".xlsx", ".pptx", ".md",
    # 한글 문서
    ".hwp", ".hwpx",
    # 이미지
    ".png", ".jpg", ".jpeg",
    # 웹
    ".html",
}
MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

@dataclasses.dataclass
class UploadValidationResult:
    ok: bool
    warnings: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    duplicate: bool = False

def validate_upload(owner: AbstractBaseUser, uploaded_file: File) -> UploadValidationResult:
    """
    파일 업로드 전 유효성을 검사합니다.

    Args:
        owner (AbstractBaseUser): 파일을 업로드하는 사용자 객체
        uploaded_file (File): 업로드된 파일 객체 (예: UploadedFile)
    
    기능:
        - 파일 크기 검사
        - 파일 형식/MIME 타입 검사
        - 파일 중복 검사

    Returns:
        UploadValidationResult: 유효성 검사 결과
    """
    warnings = []
    errors = []

    if uploaded_file is None:
        return UploadValidationResult(
            ok=False,
            errors=["파일이 없습니다."]
        )
    
    if uploaded_file.size > MAX_UPLOAD_SIZE:
        return UploadValidationResult(
            ok=False,
            errors=["파일 크기가 제한을 초과했습니다."]
        )

    # 저장 공간 할당량 체크
    storage, _ = UserStorage.objects.get_or_create(user=owner)
    if storage.used_size + uploaded_file.size > storage.total_size:
        remaining_mb = round(storage.remaining_size / 1024 / 1024, 2)
        return UploadValidationResult(
            ok=False,
            errors=[f"저장 공간이 부족합니다. (현재 잔여: {remaining_mb} MB)"]
        )

    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return UploadValidationResult(
            ok=False,
            errors=["지원하지 않는 파일 형식입니다."]
        )

    content_type = getattr(uploaded_file, "content_type", None)
    if not content_type:
        warnings.append("파일의 MIME을 확인할 수 없습니다.")
    
    sha256 = calculate_sha256(uploaded_file)
    duplicate = FileBlob.objects.filter(node__owner=owner, sha256=sha256).exists()
    if duplicate:
        warnings.append("이미 존재하는 파일입니다.")
    
    ok = len(errors) == 0

    return UploadValidationResult(
        ok=ok,
        warnings=warnings,
        errors=errors,
        duplicate=duplicate,
    )


def save_file(owner: AbstractBaseUser, file: File, description: str, parent=None) -> Node:
    """
    업로드된 파일을 스토리지에 저장하고, 데이터베이스에 파일 정보를 기록(Node, FileBlob 객체 생성)합니다.

    Args:
        owner (AbstractBaseUser): 파일을 업로드하는 사용자 객체
        file (File): 업로드된 파일 객체 (예: UploadedFile)
        description (str): 사용자가 입력한 파일 설명
        parent (Node, optional): 부모 폴더 노드

    Returns:
        Node: 데이터베이스에 생성된 메타데이터 Node 모델 인스턴스
    """

    sha256 = calculate_sha256(file)
    file.seek(0)

    with transaction.atomic():
        # 메타데이터 Node 생성
        node = Node.objects.create(
            owner=owner,
            parent=parent,
            name=file.name,
            ext=extract_ext(file.name),
            node_type=NodeType.FILE,
            description=description,
        )

        # 실제 파일(FileBlob) 저장
        FileBlob.objects.create(
            node=node,
            file=file,
            original_name=file.name,
            size=file.size,
            mime_type=getattr(file, "content_type", ""),
            sha256=sha256,
        )
        
    return node


def delete_file(file):
    pass

def open_file(file):
    pass

def get_download_response(file):
    pass

def get_file(file):
    pass

def get_files(user):
    pass