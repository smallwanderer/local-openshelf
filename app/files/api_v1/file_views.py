import json
import logging

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods
from django.views.decorators.clickjacking import xframe_options_sameorigin

from accounts.decorators import email_verification_required
from config.enums import AIStatus
from document_ai.models import DocumentChunk, DocumentParseResult, RAGJob
from document_ai.tasks import enqueue_embedding_tasks, parse_document_with_docling
from files.models import Node, NodeType, UserStorage
from files.services import file_service
from files.services.storage import save_file, validate_upload

logger = logging.getLogger(__name__)


def _json_body(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


def _node_queryset_for_uids(user, uids, *, trashed=None):
    qs = Node.objects.filter(owner=user, uid__in=uids)
    if trashed is not None:
        qs = qs.filter(trashed=trashed)
    return qs


@login_required
@email_verification_required
@require_http_methods(["GET"])
def file_list(request):
    q = request.GET.get("q", "").strip()
    parent_id = request.GET.get("parent_id")
    tag = request.GET.get("tag", "").strip()
    page = request.GET.get("page", 1)
    limit = request.GET.get("limit", 50)

    # If q starts with #, treat as tag search
    if q.startswith("#") and len(q) > 1:
        tag = q[1:]
        q = ""

    qs = file_service.get_user_files(request.user, q, parent_id, tag=tag)

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
        "has_next": page_obj.has_next(),
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
            parent=parent,
        )
        return JsonResponse({
            "ok": True,
            "status": "duplicate" if result.duplicate else "done",
            "file": node.to_dict(),
            "warnings": result.warnings,
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
            parent=parent,
        )
        return JsonResponse({"ok": True, "folder": node.to_dict()})
    except Exception as e:
        logger.exception("creating folder failed")
        return JsonResponse({"ok": False, "status": "error", "errors": [str(e)]}, status=500)


@login_required
@email_verification_required
@require_http_methods(["POST"])
def rename_file(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user)
    try:
        data = json.loads(request.body)
        new_name = data.get("name", "").strip()
    except json.JSONDecodeError:
        new_name = request.POST.get("name", "").strip()

    if not new_name:
        return JsonResponse({"ok": False, "status": "error", "errors": ["A new name is required."]}, status=400)

    try:
        node.move(new_name=new_name)
    except Exception as e:
        logger.exception("file rename failed")
        return JsonResponse({"ok": False, "status": "error", "errors": [str(e)]}, status=500)

    return JsonResponse({"ok": True, "file": node.to_dict()})


@login_required
@email_verification_required
@require_http_methods(["POST"])
def move_file(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user)
    data = _json_body(request)
    parent_id = data.get("parent_id", request.POST.get("parent_id"))

    if parent_id is None:
        return JsonResponse({"ok": False, "status": "error", "errors": ["A destination folder must be specified."]}, status=400)

    try:
        if parent_id == "" or parent_id == "root":
            node.move(to_root=True)
        else:
            target_parent = get_object_or_404(Node, uid=parent_id, owner=request.user, node_type=NodeType.FOLDER)
            node.move(new_parent=target_parent)
    except ValueError as e:
        return JsonResponse({"ok": False, "status": "error", "errors": [str(e)]}, status=400)
    except Exception as e:
        logger.exception("file move failed")
        return JsonResponse({"ok": False, "status": "error", "errors": [str(e)]}, status=500)

    return JsonResponse({"ok": True, "file": node.to_dict()})


@login_required
@email_verification_required
@require_http_methods(["POST"])
def bulk_delete(request):
    data = _json_body(request)
    uids = data.get("uids") or []
    if not isinstance(uids, list) or not uids:
        return JsonResponse({"ok": False, "errors": ["At least one item must be selected."]}, status=400)

    nodes = list(_node_queryset_for_uids(request.user, uids, trashed=False))
    for node in nodes:
        file_service.move_to_trash(node)

    return JsonResponse({"ok": True, "count": len(nodes), "message": "Moved selected items to trash."})


@login_required
@email_verification_required
@require_http_methods(["POST"])
def bulk_restore(request):
    data = _json_body(request)
    uids = data.get("uids") or []
    if not isinstance(uids, list) or not uids:
        return JsonResponse({"ok": False, "errors": ["At least one item must be selected."]}, status=400)

    restored = 0
    errors = []
    for node in _node_queryset_for_uids(request.user, uids, trashed=True):
        try:
            file_service.restore_file(node)
            restored += 1
        except ValueError as exc:
            errors.append(str(exc))

    status = 207 if errors else 200
    return JsonResponse({"ok": not errors, "count": restored, "errors": errors}, status=status)


@login_required
@email_verification_required
@require_http_methods(["POST"])
def bulk_move(request):
    data = _json_body(request)
    uids = data.get("uids") or []
    parent_id = data.get("parent_id")
    if not isinstance(uids, list) or not uids:
        return JsonResponse({"ok": False, "errors": ["At least one item must be selected."]}, status=400)
    if parent_id is None:
        return JsonResponse({"ok": False, "errors": ["A destination folder must be specified."]}, status=400)

    target_parent = None
    to_root = parent_id in {"", "root"}
    if not to_root:
        target_parent = get_object_or_404(Node, uid=parent_id, owner=request.user, node_type=NodeType.FOLDER, trashed=False)

    moved = 0
    errors = []
    for node in _node_queryset_for_uids(request.user, uids, trashed=False):
        try:
            if to_root:
                node.move(to_root=True)
            else:
                node.move(new_parent=target_parent)
            moved += 1
        except ValueError as exc:
            errors.append(f"{node.name}: {exc}")

    status = 207 if errors else 200
    return JsonResponse({"ok": not errors, "count": moved, "errors": errors}, status=status)


@login_required
@email_verification_required
@require_http_methods(["POST"])
def toggle_star(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user)
    is_starred = file_service.toggle_star_status(node)
    return JsonResponse({"ok": True, "starred": is_starred})


@login_required
@email_verification_required
@require_http_methods(["GET"])
def file_detail(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user)
    return JsonResponse({"ok": True, "file": node.to_dict()})


@login_required
@email_verification_required
@require_http_methods(["GET"])
def get_parsed_text(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user, node_type=NodeType.FILE)
    if not hasattr(node, "parse_result"):
        return JsonResponse({"ok": False, "errors": ["No AI parse result found for this file."]}, status=404)
    
    chunks = DocumentChunk.objects.filter(parse_result=node.parse_result).order_by("chunk_index")
    text_content = "\n\n".join(chunk.text for chunk in chunks if chunk.text)
    
    if not text_content:
        return JsonResponse({"ok": False, "errors": ["Text is empty or still parsing."]}, status=404)
        
    return JsonResponse({"ok": True, "text": text_content})


@login_required
@email_verification_required
@require_http_methods(["POST"])
def update_meta(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user, node_type=NodeType.FILE)
    data = _json_body(request)
    
    if hasattr(node, "parse_result"):
        pr = node.parse_result
        updated = False
        if "summary" in data:
            pr.summary = data["summary"]
            updated = True
        if "auto_tags" in data and isinstance(data["auto_tags"], list):
            pr.auto_tags = data["auto_tags"]
            updated = True
        
        if updated:
            pr.save(update_fields=["summary", "auto_tags", "updated_at"])
    else:
        return JsonResponse({"ok": False, "errors": ["Parse result not found for this file."]}, status=400)
    
    return JsonResponse({"ok": True, "file": node.to_dict()})


@login_required
@email_verification_required
@require_http_methods(["GET"])
def all_folders(request):
    qs = Node.objects.filter(owner=request.user, node_type=NodeType.FOLDER, trashed=False).order_by("path")
    data = [{"uid": str(node.uid), "name": node.name, "path": node.path} for node in qs]
    return JsonResponse({"ok": True, "folders": data})


@login_required
@email_verification_required
@require_http_methods(["GET"])
def rag_scope_nodes(request):
    q = request.GET.get("q", "").strip()
    try:
        limit = min(max(int(request.GET.get("limit", 300)), 1), 1000)
    except ValueError:
        limit = 300

    qs = (
        Node.objects.filter(
            owner=request.user,
            trashed=False,
        )
        .filter(Q(node_type=NodeType.FOLDER) | Q(node_type=NodeType.FILE, blob__isnull=False))
        .select_related("blob", "parse_result", "parent")
        .order_by("path", "name")
    )
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(path__icontains=q))

    nodes = list(qs[:limit])
    nodes_by_id = {node.id: node for node in nodes}
    children_by_parent = {}
    roots = []
    for node in nodes:
        if node.parent_id in nodes_by_id:
            children_by_parent.setdefault(node.parent_id, []).append(node)
        else:
            roots.append(node)

    def sort_scope_node(node):
        type_rank = 0 if node.node_type == NodeType.FOLDER else 1
        return (type_rank, node.name.lower(), node.path.lower())

    ordered_nodes = []

    def append_tree(bucket):
        for node in sorted(bucket, key=sort_scope_node):
            ordered_nodes.append(node)
            append_tree(children_by_parent.get(node.id, []))

    append_tree(roots)
    nodes = ordered_nodes
    folder_file_counts = {}
    folder_paths = [node.path.rstrip("/") for node in nodes if node.node_type == NodeType.FOLDER]
    for folder_path in folder_paths:
        folder_file_counts[folder_path] = Node.objects.filter(
            owner=request.user,
            node_type=NodeType.FILE,
            trashed=False,
            blob__isnull=False,
            path__startswith=f"{folder_path}/",
        ).count()

    data = []
    for node in nodes:
        item = {
            "uid": str(node.uid),
            "name": node.name,
            "path": node.path,
            "node_type": node.node_type,
            "depth": max(len([part for part in node.path.strip("/").split("/") if part]) - 1, 0),
            "ext": node.ext,
            "file_count": 0,
        }
        if node.node_type == NodeType.FOLDER:
            item["file_count"] = folder_file_counts.get(node.path.rstrip("/"), 0)
        data.append(item)

    return JsonResponse({
        "ok": True,
        "nodes": data,
        "count": len(data),
        "limit": limit,
    })


@login_required
@email_verification_required
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
@email_verification_required
@require_http_methods(["GET"])
def ai_readiness(request):
    file_qs = Node.objects.filter(
        owner=request.user,
        node_type=NodeType.FILE,
        trashed=False,
        blob__isnull=False,
    )
    total_files = file_qs.distinct().count()

    parse_counts = DocumentParseResult.objects.filter(
        node__owner=request.user,
        node__node_type=NodeType.FILE,
        node__trashed=False,
        node__blob__isnull=False,
    ).aggregate(
        completed=Count("id", filter=Q(status=AIStatus.COMPLETED)),
        pending=Count("id", filter=Q(status=AIStatus.PENDING)),
        processing=Count("id", filter=Q(status=AIStatus.PROCESSING)),
        failed=Count("id", filter=Q(status=AIStatus.FAILED)),
    )

    files_with_parse_result = sum(parse_counts.values())
    parse_pending = (total_files - files_with_parse_result) + (parse_counts["pending"] or 0)
    parse_processing = parse_counts["processing"] or 0
    parse_failed = parse_counts["failed"] or 0
    parse_completed = parse_counts["completed"] or 0

    embedding_qs = (
        file_qs.filter(parse_result__status=AIStatus.COMPLETED)
        .annotate(
            total_chunks=Count("parse_result__chunks", distinct=True),
            completed_chunks=Count(
                "parse_result__chunks",
                filter=Q(parse_result__chunks__status=AIStatus.COMPLETED),
                distinct=True,
            ),
            processing_chunks=Count(
                "parse_result__chunks",
                filter=Q(parse_result__chunks__status=AIStatus.PROCESSING),
                distinct=True,
            ),
            pending_chunks=Count(
                "parse_result__chunks",
                filter=Q(parse_result__chunks__status=AIStatus.PENDING),
                distinct=True,
            ),
            failed_chunks=Count(
                "parse_result__chunks",
                filter=Q(parse_result__chunks__status=AIStatus.FAILED),
                distinct=True,
            ),
        )
    )
    embedding_ready = embedding_qs.filter(total_chunks__gt=0, completed_chunks=F("total_chunks")).count()
    embedding_failed = embedding_qs.filter(failed_chunks__gt=0).exclude(
        completed_chunks=F("total_chunks")
    ).count()
    embedding_processing = embedding_qs.filter(
        Q(processing_chunks__gt=0) | Q(completed_chunks__gt=0)
    ).exclude(completed_chunks=F("total_chunks")).exclude(failed_chunks__gt=0).count()
    embedding_pending = max(
        parse_completed - embedding_ready - embedding_failed - embedding_processing,
        0,
    )

    ready_percent = round((embedding_ready / total_files) * 100, 1) if total_files else 0.0
    searchable_files = embedding_ready

    if total_files == 0:
        summary = "업로드된 파일이 없습니다."
    elif embedding_ready == total_files:
        summary = "모든 파일이 AI 검색/RAG 준비 완료 상태입니다."
    elif embedding_ready == 0:
        summary = "아직 AI 검색/RAG에 사용할 수 있는 문서가 없습니다."
    else:
        summary = f"{embedding_ready}개 문서가 AI 검색/RAG에 준비되었습니다."

    return JsonResponse({
        "ok": True,
        "total_files": total_files,
        "searchable_files": searchable_files,
        "ready_percent": ready_percent,
        "summary": summary,
        "parse": {
            "completed": parse_completed,
            "pending": parse_pending,
            "processing": parse_processing,
            "failed": parse_failed,
        },
        "embedding": {
            "completed": embedding_ready,
            "pending": embedding_pending,
            "processing": embedding_processing,
            "failed": embedding_failed,
        },
    })


@login_required
@email_verification_required
@require_http_methods(["GET"])
@xframe_options_sameorigin
def file_download(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user, node_type=NodeType.FILE)
    if not hasattr(node, "blob") or not node.blob.file:
        raise Http404("File not found.")

    as_attachment = request.GET.get("inline") != "1"
    resp = FileResponse(node.blob.file.open("rb"), as_attachment=as_attachment, filename=node.blob.original_name)
    return resp


@login_required
@email_verification_required
@require_http_methods(["DELETE", "POST"])
def file_delete(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user)
    file_service.move_to_trash(node)
    return JsonResponse({"ok": True, "message": "Moved to trash."})


@login_required
@email_verification_required
@require_http_methods(["POST"])
def retry_ai_processing(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user, node_type=NodeType.FILE, trashed=False)
    ai_status = node.get_ai_status() or {}
    parse_status = ai_status.get("parse_status")
    embedding_status = ai_status.get("embedding_status")

    if parse_status in {AIStatus.PENDING, AIStatus.PROCESSING} or embedding_status == AIStatus.PROCESSING:
        return JsonResponse({"ok": False, "errors": ["AI processing is already queued or running."]}, status=409)

    if not hasattr(node, "parse_result") or parse_status == AIStatus.FAILED:
        if hasattr(node, "parse_result"):
            node.parse_result.status = AIStatus.PENDING
            node.parse_result.errors = []
            node.parse_result.save(update_fields=["status", "errors", "updated_at"])
        parse_document_with_docling.delay(node.id)
        return JsonResponse({"ok": True, "action": "parse_requeued", "message": "Parsing retry has been queued."})

    if embedding_status == AIStatus.FAILED:
        updated = DocumentChunk.objects.filter(
            parse_result=node.parse_result,
            status=AIStatus.FAILED,
        ).update(status=AIStatus.PENDING, error_message={})
        enqueue_embedding_tasks.delay(node.id)
        return JsonResponse({
            "ok": True,
            "action": "embedding_requeued",
            "chunk_count": updated,
            "message": "Embedding retry has been queued.",
        })

    return JsonResponse({"ok": False, "errors": ["There is no failed AI work to retry."]}, status=400)


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
@email_verification_required
@require_http_methods(["POST"])
def restore_file(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user, trashed=True)
    try:
        file_service.restore_file(node)
    except ValueError as exc:
        return JsonResponse({"ok": False, "errors": [str(exc)]}, status=400)
    return JsonResponse({"ok": True, "message": "Restored."})


@login_required
@email_verification_required
@require_http_methods(["DELETE", "POST"])
def permanent_delete(request, uid):
    node = get_object_or_404(Node, uid=uid, owner=request.user, trashed=True)
    file_service.permanent_delete(node)
    return JsonResponse({"ok": True, "message": "Permanently deleted."})


@login_required
@email_verification_required
@require_http_methods(["DELETE", "POST"])
def empty_trash(request):
    file_service.empty_trash(request.user)
    return JsonResponse({"ok": True, "message": "Trash emptied."})


@login_required
@email_verification_required
@require_http_methods(["GET"])
def recent_search_history(request):
    limit = min(int(request.GET.get("limit", 10)), 20)
    rag_jobs = (
        RAGJob.objects.filter(owner=request.user, status=AIStatus.COMPLETED)
        .select_related("search_job")
        .order_by("-completed_at")[:limit]
    )
    history = []
    for job in rag_jobs:
        result_count = len(job.search_job.results) if job.search_job and job.search_job.results else 0
        history.append({
            "id": job.id,
            "question": job.question,
            "answer_preview": (job.answer[:120] + "...") if len(job.answer) > 120 else job.answer,
            "answer": job.answer,
            "citations": job.citations,
            "result_count": result_count,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "created_at": job.created_at.isoformat(),
        })
    return JsonResponse({"ok": True, "history": history})
