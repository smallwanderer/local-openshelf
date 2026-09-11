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
from django.db.models import F, Q
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from accounts.models import SyncQuota
from accounts.client_identity import serialize_client_identity
from config.enums import NodeType
from files.models import FileBlob, Node
from files.services import file_service
from files.services.storage import ALLOWED_EXTENSIONS, save_file
from files.services.utils import calculate_sha256

from .auth import api_token_required

logger = logging.getLogger(__name__)

SYNC_ROOT_PREFIX = "sync"
MAX_REL_PATH_LENGTH = 900


def _json_body(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


def _normalize_root_name(value):
    if not isinstance(value, str):
        raise ValueError("root_name must be a string.")
    root_name = value.strip()
    if (
        not root_name
        or root_name in {".", ".."}
        or "/" in root_name
        or "\\" in root_name
        or "\x00" in root_name
        or len(root_name) > 255
    ):
        raise ValueError("root_name must be one valid folder name.")
    return root_name


def _normalize_rel_path(value):
    if not isinstance(value, str):
        raise ValueError("rel_path must be a string.")
    normalized = value.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.endswith("/")
        or "\x00" in normalized
        or len(normalized) > MAX_REL_PATH_LENGTH
    ):
        raise ValueError("rel_path must be a non-empty relative path.")
    parts = normalized.split("/")
    if any(not part or part in {".", ".."} or len(part) > 255 for part in parts):
        raise ValueError("rel_path contains an invalid path segment.")
    return "/".join(parts)


def _restore_folder_if_needed(node):
    if node.trashed:
        node.trashed = False
        node.deleted_at = None
        node.ai_processing_enabled = True
        node.save(
            update_fields=[
                "trashed",
                "deleted_at",
                "ai_processing_enabled",
                "updated_at",
            ]
        )
    return node


def _get_or_create_sync_root(workspace, user, folder_name):
    """Get or create the /sync/<folder_name>/ node hierarchy.

    Returns the leaf folder Node.
    """
    folder_name = _normalize_root_name(folder_name)
    # 1. Ensure /sync/ root folder exists
    sync_root, _ = Node.objects.get_or_create(
        workspace=workspace,
        name=SYNC_ROOT_PREFIX,
        parent=None,
        node_type=NodeType.FOLDER,
        defaults={"ext": "", "owner": user},
    )
    _restore_folder_if_needed(sync_root)
    # 2. Ensure /sync/<folder_name>/ exists
    target_folder, _ = Node.objects.get_or_create(
        workspace=workspace,
        name=folder_name,
        parent=sync_root,
        node_type=NodeType.FOLDER,
        defaults={"ext": "", "owner": user},
    )
    return _restore_folder_if_needed(target_folder)


def _resolve_sync_folder(workspace, user, *, root_uid="", root_name="default", create=True):
    if root_uid:
        try:
            sync_folder = Node.objects.select_related("parent").get(
                workspace=workspace,
                uid=root_uid,
                node_type=NodeType.FOLDER,
                trashed=False,
            )
        except (Node.DoesNotExist, ValueError):
            raise ValueError("Unknown or inactive sync root.")
        parent = sync_folder.parent
        if (
            parent is None
            or parent.workspace_id != workspace.id
            or parent.parent_id is not None
            or parent.name != SYNC_ROOT_PREFIX
            or parent.node_type != NodeType.FOLDER
            or parent.trashed
        ):
            raise ValueError("root_uid does not identify a sync root.")
        return sync_folder
    if create:
        return _get_or_create_sync_root(workspace, user, root_name)
    root_name = _normalize_root_name(root_name)
    try:
        return Node.objects.get(
            workspace=workspace,
            name=root_name,
            parent__workspace=workspace,
            parent__name=SYNC_ROOT_PREFIX,
            parent__parent=None,
            parent__node_type=NodeType.FOLDER,
            parent__trashed=False,
            node_type=NodeType.FOLDER,
            trashed=False,
        )
    except Node.DoesNotExist:
        raise ValueError("Unknown or inactive sync root.")


def _is_within_sync_folder(node, sync_folder):
    return node.pk != sync_folder.pk and node.path.startswith(
        sync_folder.path.rstrip("/") + "/"
    )


def _ensure_parent_dirs(workspace, user, sync_folder, rel_path):
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
            workspace=workspace,
            name=dir_name,
            parent=parent,
            node_type=NodeType.FOLDER,
            defaults={"ext": "", "owner": user},
        )
        parent = _restore_folder_if_needed(child)
    return parent


def _ensure_directory(workspace, user, sync_folder, rel_path):
    parent = sync_folder
    final_created = False
    for dir_name in rel_path.split("/"):
        child, created = Node.objects.get_or_create(
            workspace=workspace,
            name=dir_name,
            parent=parent,
            node_type=NodeType.FOLDER,
            defaults={"ext": "", "owner": user},
        )
        parent = _restore_folder_if_needed(child)
        final_created = created
    return parent, final_created


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


def _bool_from_request(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "off", "no"}


# ── Endpoints ─────────────────────────────────────────────────────────


@csrf_exempt
@require_http_methods(["GET"])
@api_token_required
def ping(request):
    return JsonResponse({"ok": True, "message": "pong"})


@csrf_exempt
@require_http_methods(["GET"])
@api_token_required
def identity(request):
    return JsonResponse(serialize_client_identity(request.api_token))


@csrf_exempt
@require_http_methods(["POST"])
@api_token_required
def diff(request):
    """Compare client manifest against server state, return needed actions."""
    data = _json_body(request)
    entries = data.get("entries", [])
    root_path = data.get("root_path", "")

    if not isinstance(entries, list):
        return JsonResponse({"ok": False, "errors": ["entries must be a list."]}, status=400)
    if not entries and not root_path and not data.get("root_name"):
        return JsonResponse({"ok": False, "errors": ["Empty manifest."]}, status=400)

    # root_name avoids sending a client absolute path and lets callers
    # disambiguate two local roots that happen to share the same basename.
    folder_name = (
        data.get("root_name")
        or os.path.basename(str(root_path).rstrip("/\\"))
        or "default"
    )
    try:
        folder_name = _normalize_root_name(folder_name)
        try:
            sync_folder = _resolve_sync_folder(
                request.workspace,
                request.user,
                root_name=folder_name,
                create=False,
            )
        except ValueError:
            sync_folder = None
    except ValueError as exc:
        return JsonResponse({"ok": False, "errors": [str(exc)]}, status=400)

    # Build server-side path→node index under sync folder
    if sync_folder is None:
        server_nodes = []
        sync_prefix = ""
    else:
        sync_prefix = sync_folder.path.rstrip("/") + "/"
        server_nodes = Node.objects.filter(workspace=request.workspace, trashed=False).filter(
            Q(pk=sync_folder.pk) | Q(path__startswith=sync_prefix)
        ).select_related("blob")

    # Map: relative path (from sync_folder) → node
    server_map = {}
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

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return JsonResponse(
                {"ok": False, "errors": [f"entries[{index}] must be an object."]},
                status=400,
            )
        try:
            rel_path = _normalize_rel_path(entry.get("rel_path", ""))
        except ValueError as exc:
            return JsonResponse(
                {"ok": False, "errors": [f"entries[{index}]: {exc}"]},
                status=400,
            )
        if rel_path in client_set:
            return JsonResponse(
                {"ok": False, "errors": [f"Duplicate rel_path: {rel_path}"]},
                status=400,
            )
        client_set.add(rel_path)
        is_dir = entry.get("is_dir", False)
        if not isinstance(is_dir, bool):
            return JsonResponse(
                {"ok": False, "errors": [f"entries[{index}].is_dir must be a boolean."]},
                status=400,
            )
        content_hash = entry.get("content_hash", "")

        if rel_path in server_map:
            server_node = server_map[rel_path]
            server_is_dir = server_node.node_type == NodeType.FOLDER
            if server_is_dir != is_dir:
                actions.append({
                    "rel_path": rel_path,
                    "action": "conflict",
                    "reason": "type_mismatch",
                    "server_node_uid": str(server_node.uid),
                })
                continue
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
    missing_server_paths = []
    for rel_path, node in sorted(
        server_map.items(),
        key=lambda item: (item[0].count("/"), item[0]),
    ):
        if rel_path not in client_set and not any(
            rel_path.startswith(parent_path + "/") for parent_path in missing_server_paths
        ):
            missing_server_paths.append(rel_path)
            actions.append({
                "rel_path": rel_path,
                "action": "delete",
                "server_node_uid": str(node.uid),
            })

    return JsonResponse({
        "ok": True,
        "actions": actions,
        "sync_id": sync_id,
        "root_name": folder_name,
        "root_uid": str(sync_folder.uid) if sync_folder is not None else "",
    })


@csrf_exempt
@require_http_methods(["POST"])
@api_token_required
def upload(request):
    """Upload a single file into the sync folder structure."""
    if "file" not in request.FILES:
        return JsonResponse({"ok": False, "errors": ["No file provided."]}, status=400)

    uploaded_file = request.FILES["file"]
    rel_path_value = request.POST.get("rel_path", "")
    ai_processing_value = request.POST.get("ai_processing_enabled")

    try:
        rel_path = _normalize_rel_path(rel_path_value)
        sync_folder = _resolve_sync_folder(
            request.workspace,
            request.user,
            root_uid=request.POST.get("root_uid", ""),
            root_name=request.POST.get("root_name") or request.POST.get("folder_name", "default"),
        )
    except ValueError as exc:
        return JsonResponse({"ok": False, "errors": [str(exc)]}, status=400)

    # Extension check
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return JsonResponse(
            {"ok": False, "errors": [f"Extension {ext} is not allowed."]},
            status=400,
        )

    parent = _ensure_parent_dirs(request.workspace, request.user, sync_folder, rel_path)

    try:
        # Check if node already exists (update case)
        file_name = rel_path.split("/")[-1]
        existing = Node.objects.filter(
            workspace=request.workspace,
            parent=parent,
            name=file_name,
            node_type=NodeType.FILE,
        ).first()
        old_size = (
            existing.blob.size or 0
            if existing and hasattr(existing, "blob")
            else 0
        )
        quota_delta = max(0, uploaded_file.size - old_size)
        ok, err = _check_sync_quota(request.user, quota_delta)
        if not ok:
            return JsonResponse({"ok": False, "errors": [err]}, status=400)

        if existing:
            # Update: delete old blob, create new one
            content_hash = calculate_sha256(uploaded_file)
            uploaded_file.seek(0)
            with transaction.atomic():
                if hasattr(existing, "blob"):
                    existing.blob.delete()
                existing.trashed = False
                existing.deleted_at = None
                if ai_processing_value is not None:
                    existing.ai_processing_enabled = _bool_from_request(ai_processing_value)
                existing.save(
                    update_fields=[
                        "trashed",
                        "deleted_at",
                        "ai_processing_enabled",
                        "updated_at",
                    ]
                )
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
                "root_uid": str(sync_folder.uid),
            })

        # New file: use existing save_file to trigger the full pipeline
        node = save_file(
            workspace=request.workspace,
            owner=request.user,
            file=uploaded_file,
            description=f"synced: {rel_path}",
            parent=parent,
            ai_processing_enabled=_bool_from_request(ai_processing_value, default=True),
        )

        # Update sync quota
        _update_sync_quota(request.user, uploaded_file.size)

        return JsonResponse({
            "ok": True,
            "node_uid": str(node.uid),
            "action": "created",
            "root_uid": str(sync_folder.uid),
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
    try:
        rel_path = _normalize_rel_path(data.get("rel_path", ""))
        sync_folder = _resolve_sync_folder(
            request.workspace,
            request.user,
            root_uid=data.get("root_uid", ""),
            root_name=data.get("root_name") or data.get("folder_name", "default"),
        )
    except ValueError as exc:
        return JsonResponse({"ok": False, "errors": [str(exc)]}, status=400)

    node, created = _ensure_directory(request.workspace, request.user, sync_folder, rel_path)
    return JsonResponse({
        "ok": True,
        "node_uid": str(node.uid),
        "created": created,
        "root_uid": str(sync_folder.uid),
    })


@csrf_exempt
@require_http_methods(["POST"])
@api_token_required
def delete(request):
    """Soft-delete nodes by UID."""
    data = _json_body(request)
    node_uids = data.get("node_uids", [])
    if not isinstance(node_uids, list) or not node_uids:
        return JsonResponse({"ok": False, "errors": ["node_uids is required."]}, status=400)
    if len(node_uids) > 1000:
        return JsonResponse({"ok": False, "errors": ["At most 1000 nodes may be deleted at once."]}, status=400)
    has_root_selector = bool(
        data.get("root_uid") or data.get("root_name") or data.get("folder_name")
    )
    sync_folder = None
    if has_root_selector:
        try:
            sync_folder = _resolve_sync_folder(
                request.workspace,
                request.user,
                root_uid=data.get("root_uid", ""),
                root_name=data.get("root_name") or data.get("folder_name", "default"),
                create=False,
            )
        except ValueError as exc:
            return JsonResponse({"ok": False, "errors": [str(exc)]}, status=400)

    nodes = []
    invalid_uids = []
    legacy_root_names = set()
    for uid in node_uids:
        try:
            node = Node.objects.get(uid=uid, workspace=request.workspace, trashed=False)
        except (Node.DoesNotExist, ValueError, TypeError):
            invalid_uids.append(str(uid))
            continue
        if sync_folder is not None:
            if not _is_within_sync_folder(node, sync_folder):
                invalid_uids.append(str(uid))
                continue
        else:
            path_parts = node.path.strip("/").split("/")
            if len(path_parts) < 3 or path_parts[0] != SYNC_ROOT_PREFIX:
                invalid_uids.append(str(uid))
                continue
            legacy_root_names.add(path_parts[1])
        nodes.append(node)

    if len(legacy_root_names) > 1:
        invalid_uids.extend(str(uid) for uid in node_uids)

    if invalid_uids:
        return JsonResponse(
            {
                "ok": False,
                "errors": ["Every node must exist inside the selected sync root."],
                "invalid_node_uids": invalid_uids,
            },
            status=400,
        )

    if sync_folder is None:
        try:
            sync_folder = _resolve_sync_folder(
                request.workspace,
                request.user,
                root_name=next(iter(legacy_root_names)),
                create=False,
            )
        except (StopIteration, ValueError):
            return JsonResponse(
                {"ok": False, "errors": ["Cannot resolve the legacy sync root."]},
                status=400,
            )

    deleted = 0
    with transaction.atomic():
        for node in nodes:
            if node.trashed:
                continue
            file_service.move_to_trash(node)
            deleted += 1

    return JsonResponse({"ok": True, "deleted": deleted, "root_uid": str(sync_folder.uid)})


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
