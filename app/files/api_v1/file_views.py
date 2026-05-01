from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods
from django.core.paginator import Paginator
import json
import logging

from files.models import Node, NodeType
from files.services.storage import save_file, validate_upload
from files.services import file_service
from accounts.decorators import email_verification_required

logger = logging.getLogger(__name__)

@login_required
@email_verification_required
@require_http_methods(["GET"])
def file_list(request):
    q = request.GET.get("q", "").strip()
    parent_id = request.GET.get("parent_id")
    page = request.GET.get("page", 1)
    limit = request.GET.get("limit", 50)

    qs = file_service.get_user_files(request.user, q, parent_id)
    
    try:
        limit = int(limit)
        page = int(page)
    except ValueError:
        limit = 50
        page = 1

    paginator = Paginator(qs, limit)
    try:
        page_obj = paginator.page(page)
    except Exception:
        return JsonResponse({"ok": True, "files": [], "page": page, "has_next": False})

    data = [node.to_dict() for node in page_obj]
    return JsonResponse({
        "ok": True, 
        "files": data,
        "page": page,
        "has_next": page_obj.has_next()
    })

@login_required
@email_verification_required
@require_http_methods(["POST"])
def upload_file(request):
    logger.info("API upload file view")
    if "file" not in request.FILES:
        return JsonResponse({"ok": False, "status": "error", "errors": ["No file provided."]}, status=400)

    result = validate_upload(request.user, request.FILES["file"])
    if not result.ok:
        return JsonResponse({"ok": False, "status": "error", "errors": result.errors}, status=400)

    try:
        parent_id = request.POST.get("parent_id")
        parent = None
        if parent_id:
            parent = get_object_or_404(Node, uid=parent_id, owner=request.user, node_type=NodeType.FOLDER)

        node = save_file(
            owner=request.user,
            file=request.FILES["file"],
            description=request.POST.get("description", ""),
            parent=parent
        )
        return JsonResponse({
            "ok": True,
            "status": "duplicate" if result.duplicate else "done",
            "file": node.to_dict(),
            "warnings": result.warnings
        })
    except Exception as e:
        logger.exception("uploading file failed")
        return JsonResponse({"ok": False, "status": "error", "errors": [str(e)]}, status=500)

@login_required
@email_verification_required
@require_http_methods(["POST"])
def create_folder(request):
    logger.info("API create folder view")
    
    try:
        parent_id = request.POST.get("parent_id")
        name = request.POST.get("name", "").strip()
        if not name:
            return JsonResponse({"ok": False, "status": "error", "errors": ["Folder name is required."]}, status=400)
        
        parent = None
        if parent_id:
            parent = get_object_or_404(Node, uid=parent_id, owner=request.user, node_type=NodeType.FOLDER)
            
        node = file_service.create_folder(
            user=request.user,
            name=name,
            parent=parent
        )
        return JsonResponse({"ok": True, "folder": node.to_dict()})
    except Exception as e:
        logger.exception("creating folder failed")
        return JsonResponse({"ok": False, "status": "error", "errors": [str(e)]}, status=500)


@login_required
@require_http_methods(["POST"])
def rename_file(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user)
    try:
        data = json.loads(request.body)
        new_name = data.get("name", "").strip()
    except json.JSONDecodeError:
        new_name = request.POST.get("name", "").strip()

    if not new_name:
        return JsonResponse({"ok": False, "status": "error", "errors": ["새 이름이 필요합니다."]}, status=400)

    try:
        node.move(new_name=new_name)
    except Exception as e:
        logger.exception("파일 이름 변경 실패")
        return JsonResponse({"ok": False, "status": "error", "errors": ["이름을 변경하는 중 오류가 발생했습니다."]}, status=500)

    return JsonResponse({"ok": True, "file": node.to_dict()})

@login_required
@require_http_methods(["POST"])
def move_file(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user)
    try:
        data = json.loads(request.body)
        parent_id = data.get("parent_id")
    except json.JSONDecodeError:
        parent_id = request.POST.get("parent_id")

    if parent_id is None:
        return JsonResponse({"ok": False, "status": "error", "errors": ["대상 폴더가 지정되지 않았습니다."]}, status=400)

    try:
        if parent_id == "" or parent_id == "root":
            node.move(to_root=True)
        else:
            target_parent = get_object_or_404(Node, uid=parent_id, owner=request.user, node_type=NodeType.FOLDER)
            node.move(new_parent=target_parent)
    except ValueError as e:
        return JsonResponse({"ok": False, "status": "error", "errors": [str(e)]}, status=400)
    except Exception as e:
        logger.exception("파일 이동 실패")
        return JsonResponse({"ok": False, "status": "error", "errors": ["이동하는 중 오류가 발생했습니다."]}, status=500)

    return JsonResponse({"ok": True, "file": node.to_dict()})

@login_required
@require_http_methods(["POST"])
def toggle_star(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user)
    is_starred = file_service.toggle_star_status(node)
    return JsonResponse({"ok": True, "starred": is_starred})

@login_required
@require_http_methods(["GET"])
def file_detail(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user)
    return JsonResponse({"ok": True, "file": node.to_dict()})

@login_required
@require_http_methods(["GET"])
def all_folders(request):
    qs = Node.objects.filter(owner=request.user, node_type=NodeType.FOLDER, trashed=False).order_by('path')
    data = [{"uid": str(node.uid), "name": node.name, "path": node.path} for node in qs]
    return JsonResponse({"ok": True, "folders": data})

@login_required
@require_http_methods(["GET"])
def get_storage_usage(request):
    storage, _ = UserStorage.objects.get_or_create(user=request.user)
    return JsonResponse({
        "ok": True,
        "used_size": storage.used_size,
        "used_size_mb": storage.used_size_mb(),
        "total_size": storage.total_size,
        "total_size_gb": storage.total_size_gb(),
        "usage_percent": storage.usage_percent,
        "remaining_size": storage.remaining_size,
    })

@login_required
@require_http_methods(["GET"])
def file_download(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user, node_type=NodeType.FILE)
    if not hasattr(node, "blob") or not node.blob.file:
        raise Http404("File not found.")
    
    resp = FileResponse(node.blob.file.open("rb"), as_attachment=True, filename=node.blob.original_name)
    return resp

@login_required
@require_http_methods(["DELETE", "POST"])
def file_delete(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user)
    file_service.move_to_trash(node)
    return JsonResponse({"ok": True, "message": "Moved to trash."})

@login_required
@email_verification_required
@require_http_methods(["GET"])
def recent_files(request):
    qs = file_service.get_recent_files(request.user)
    data = [node.to_dict() for node in qs]
    return JsonResponse({"ok": True, "files": data})

@login_required
@email_verification_required
@require_http_methods(["GET"])
def starred_files(request):
    qs = file_service.get_starred_files(request.user)
    data = [node.to_dict() for node in qs]
    return JsonResponse({"ok": True, "files": data})

@login_required
@email_verification_required
@require_http_methods(["GET"])
def trash_files(request):
    qs = file_service.get_trashed_files(request.user)
    data = [node.to_dict() for node in qs]
    return JsonResponse({"ok": True, "files": data})

@login_required
@require_http_methods(["POST"])
def restore_file(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user, trashed=True)
    file_service.restore_file(node)
    return JsonResponse({"ok": True, "message": "Restored."})

@login_required
@require_http_methods(["DELETE", "POST"])
def permanent_delete(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user, trashed=True)
    file_service.permanent_delete(node)
    return JsonResponse({"ok": True, "message": "Permanently deleted."})

@login_required
@require_http_methods(["DELETE", "POST"])
def empty_trash(request):
    file_service.empty_trash(request.user)
    return JsonResponse({"ok": True, "message": "Trash emptied."})
