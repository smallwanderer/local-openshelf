"""Sync API views for the Local Folder Connector.

Endpoints:
    POST /api/sync/v1/ping/     - connectivity check
    POST /api/sync/v1/diff/     - receive manifest, return diff actions
    POST /api/sync/v1/upload/   - upload a single file
    POST /api/sync/v1/mkdir/    - create a directory node
    POST /api/sync/v1/delete/   - soft-delete nodes
    POST /api/sync/v1/confirm/  - confirm sync completion

All endpoints require Bearer token authentication.
"""

import json
import logging
import os
import uuid

from django.db import transaction
from django.db.models import F
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.models import SyncQuota
from config.enums import NodeType
from files.models import FileBlob, Node
from files.services import file_service
from files.services.storage import ALLOWED_EXTENSIONS, save_file
from files.services.utils import extract_ext

from .auth import api_token_required

logger = logging.getLogger(__name__)

SYNC_ROOT_PREFIX = "sync"


def _json_body(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


def _get_or_create_sync_root(user, folder_name):
    """Get or create the /sync/<folder_name>/ node hierarchy.

    Returns the leaf folder Node.
    """
    # 1. Ensure /sync/ root folder exists
    sync_root, _ = Node.objects.get_or_create(
        owner=user,
        name=SYNC_ROOT_PREFIX,
        parent=None,
        node_type=NodeType.FOLDER,
        defaults={"ext": ""},
    )
    # 2. Ensure /sync/<folder_name>/ exists
    target_folder, _ = Node.objects.get_or_create(
        owner=user,
        name=folder_name,
        parent=sync_root,
        node_type=NodeType.FOLDER,
        defaults={"ext": ""},
    )
    return target_folder


def _ensure_parent_dirs(user, sync_folder, rel_path):
    """Ensure all parent directories for rel_path exist under sync_folder.

    For rel_path="a/b/c/file.txt", creates folders a, a/b, a/b/c if needed.
    Returns the immediate parent Node.
    """
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) <= 1:
        return sync_folder

    parent = sync_folder
    for dir_name in parts[:-1]:
        child, _ = Node.objects.get_or_create(
            owner=user,
            name=dir_name,
            parent=parent,
            node_type=NodeType.FOLDER,
            defaults={"ext": ""},
        )
        parent = child
    return parent


def _check_sync_quota(user, file_size):
    """Check if sync quota allows the upload. Returns (ok, error_msg)."""
    quota, _ = SyncQuota.objects.get_or_create(user=user)
    if quota.used_size + file_size > quota.total_size:
        remaining_mb = round(quota.remaining_size / 1024 / 1024, 2)
        return False, f"Sync quota exceeded. Remaining: {remaining_mb} MB."
    return True, ""


def _update_sync_quota(user, file_size):
    """Increase used_size in sync quota."""
    SyncQuota.objects.filter(user=user).update(used_size=F("used_size") + file_size)


# ── Endpoints ─────────────────────────────────────────────────────────


@csrf_exempt
@require_http_methods(["GET"])
@api_token_required
def ping(request):
    return JsonResponse({"ok": True, "message": "pong"})


@csrf_exempt
@require_http_methods(["POST"])
@api_token_required
def diff(request):
    """Compare client manifest against server state, return needed actions."""
    data = _json_body(request)
    entries = data.get("entries", [])
    root_path = data.get("root_path", "")

    if not entries and not root_path:
        return JsonResponse({"ok": False, "errors": ["Empty manifest."]}, status=400)

    # Derive folder name from root_path
    folder_name = os.path.basename(root_path.rstrip("/")) or "default"
    sync_folder = _get_or_create_sync_root(request.user, folder_name)

    # Build server-side path→node index under sync folder
    server_nodes = Node.objects.filter(
        owner=request.user,
        path__startswith=sync_folder.path,
        trashed=False,
    ).select_related("blob")

    # Map: relative path (from sync_folder) → node
    server_map = {}
    sync_prefix = sync_folder.path.rstrip("/") + "/"
    for node in server_nodes:
        if node.pk == sync_folder.pk:
            continue
        rel = node.path[len(sync_prefix):]
        if rel:
            server_map[rel] = node

    # Client entries index
    client_set = set()
    actions = []
    sync_id = uuid.uuid4().hex[:12]

    for entry in entries:
        rel_path = entry.get("rel_path", "")
        if not rel_path:
            continue
        client_set.add(rel_path)
        is_dir = entry.get("is_dir", False)
        content_hash = entry.get("content_hash", "")

        if rel_path in server_map:
            server_node = server_map[rel_path]
            if is_dir:
                continue  # directory exists, nothing to do
            # Compare hash
            if hasattr(server_node, "blob") and server_node.blob:
                if server_node.blob.sha256 == content_hash:
                    continue  # identical
            actions.append({
                "rel_path": rel_path,
                "action": "update",
                "server_node_uid": str(server_node.uid),
            })
        else:
            if is_dir:
                actions.append({"rel_path": rel_path, "action": "mkdir"})
            else:
                actions.append({"rel_path": rel_path, "action": "upload"})

    # Detect deletions: server has it, client doesn't
    for rel_path, node in server_map.items():
        if rel_path not in client_set:
            actions.append({
                "rel_path": rel_path,
                "action": "delete",
                "server_node_uid": str(node.uid),
            })

    return JsonResponse({"ok": True, "actions": actions, "sync_id": sync_id})


@csrf_exempt
@require_http_methods(["POST"])
@api_token_required
def upload(request):
    """Upload a single file into the sync folder structure."""
    if "file" not in request.FILES:
        return JsonResponse({"ok": False, "errors": ["No file provided."]}, status=400)

    uploaded_file = request.FILES["file"]
    rel_path = request.POST.get("rel_path", "")
    sync_id = request.POST.get("sync_id", "")
    content_hash = request.POST.get("content_hash", "")

    if not rel_path:
        return JsonResponse({"ok": False, "errors": ["rel_path is required."]}, status=400)

    # Extension check
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return JsonResponse(
            {"ok": False, "errors": [f"Extension {ext} is not allowed."]},
            status=400,
        )

    # Quota check
    ok, err = _check_sync_quota(request.user, uploaded_file.size)
    if not ok:
        return JsonResponse({"ok": False, "errors": [err]}, status=400)

    # Derive folder from the sync config stored in local db
    # For now, extract from rel_path structure
    folder_name = request.POST.get("folder_name", "default")
    # Try to get it from existing sync root
    try:
        sync_root = Node.objects.get(
            owner=request.user,
            name=SYNC_ROOT_PREFIX,
            parent=None,
            node_type=NodeType.FOLDER,
        )
        # Find the first child folder as the sync target
        children = Node.objects.filter(
            owner=request.user,
            parent=sync_root,
            node_type=NodeType.FOLDER,
            trashed=False,
        ).order_by("-created_at")
        if children.exists():
            sync_folder = children.first()
        else:
            sync_folder = _get_or_create_sync_root(request.user, folder_name)
    except Node.DoesNotExist:
        sync_folder = _get_or_create_sync_root(request.user, folder_name)

    parent = _ensure_parent_dirs(request.user, sync_folder, rel_path)

    try:
        # Check if node already exists (update case)
        file_name = rel_path.split("/")[-1]
        existing = Node.objects.filter(
            owner=request.user,
            parent=parent,
            name=file_name,
            node_type=NodeType.FILE,
            trashed=False,
        ).first()

        if existing and hasattr(existing, "blob"):
            # Update: delete old blob, create new one
            old_size = existing.blob.size or 0
            with transaction.atomic():
                existing.blob.delete()
                FileBlob.objects.create(
                    node=existing,
                    file=uploaded_file,
                    original_name=file_name,
                    size=uploaded_file.size,
                    mime_type=getattr(uploaded_file, "content_type", ""),
                    sha256=content_hash,
                )
            # Adjust quota: subtract old, add new
            SyncQuota.objects.filter(user=request.user).update(
                used_size=F("used_size") - old_size + uploaded_file.size
            )
            return JsonResponse({
                "ok": True,
                "node_uid": str(existing.uid),
                "action": "updated",
            })

        # New file: use existing save_file to trigger the full pipeline
        node = save_file(
            owner=request.user,
            file=uploaded_file,
            description=f"synced: {rel_path}",
            parent=parent,
        )

        # Update sync quota
        _update_sync_quota(request.user, uploaded_file.size)

        return JsonResponse({
            "ok": True,
            "node_uid": str(node.uid),
            "action": "created",
        })
    except Exception as exc:
        logger.exception("Sync upload failed for %s", rel_path)
        return JsonResponse({"ok": False, "errors": [str(exc)]}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
@api_token_required
def mkdir(request):
    """Create a directory node in the sync folder."""
    data = _json_body(request)
    rel_path = data.get("rel_path", "")
    if not rel_path:
        return JsonResponse({"ok": False, "errors": ["rel_path is required."]}, status=400)

    folder_name = data.get("folder_name", "default")
    try:
        sync_root = Node.objects.get(
            owner=request.user, name=SYNC_ROOT_PREFIX,
            parent=None, node_type=NodeType.FOLDER,
        )
        children = Node.objects.filter(
            owner=request.user, parent=sync_root,
            node_type=NodeType.FOLDER, trashed=False,
        ).order_by("-created_at")
        sync_folder = children.first() if children.exists() else _get_or_create_sync_root(request.user, folder_name)
    except Node.DoesNotExist:
        sync_folder = _get_or_create_sync_root(request.user, folder_name)

    parent = _ensure_parent_dirs(request.user, sync_folder, rel_path + "/dummy")
    # The last part of rel_path is the directory to create
    dir_name = rel_path.split("/")[-1]

    node, created = Node.objects.get_or_create(
        owner=request.user,
        name=dir_name,
        parent=parent if rel_path.count("/") > 0 else sync_folder,
        node_type=NodeType.FOLDER,
        defaults={"ext": ""},
    )
    return JsonResponse({
        "ok": True,
        "node_uid": str(node.uid),
        "created": created,
    })


@csrf_exempt
@require_http_methods(["POST"])
@api_token_required
def delete(request):
    """Soft-delete nodes by UID."""
    data = _json_body(request)
    node_uids = data.get("node_uids", [])
    if not node_uids:
        return JsonResponse({"ok": False, "errors": ["node_uids is required."]}, status=400)

    deleted = 0
    for uid in node_uids:
        try:
            node = Node.objects.get(uid=uid, owner=request.user, trashed=False)
            file_service.move_to_trash(node)
            deleted += 1
        except Node.DoesNotExist:
            logger.warning("Sync delete: node %s not found", uid)

    return JsonResponse({"ok": True, "deleted": deleted})


@csrf_exempt
@require_http_methods(["POST"])
@api_token_required
def confirm(request):
    """Confirm sync completion (logging only for now)."""
    data = _json_body(request)
    sync_id = data.get("sync_id", "")
    results = data.get("results", [])

    total = len(results)
    success = sum(1 for r in results if r.get("success"))
    failed = total - success

    logger.info(
        "Sync confirmed: id=%s user=%s total=%d success=%d failed=%d",
        sync_id, request.user.email, total, success, failed,
    )
    return JsonResponse({"ok": True, "sync_id": sync_id, "total": total, "success": success, "failed": failed})
