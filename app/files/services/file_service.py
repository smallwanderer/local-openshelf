import logging
from datetime import timedelta

from django.core.files.storage import default_storage
from django.db.models import Q
from django.utils import timezone

from files.models import FileBlob, Node, NodeType

logger = logging.getLogger(__name__)

TRASH_RETENTION_DAYS = 7
TRASH_RETENTION_PERIOD = timedelta(days=TRASH_RETENTION_DAYS)


def _trash_cutoff():
    return timezone.now() - TRASH_RETENTION_PERIOD


def _subtree_queryset(node):
    qs = Node.objects.filter(workspace=node.workspace)
    if node.node_type == NodeType.FOLDER:
        return qs.filter(Q(pk=node.pk) | Q(path__startswith=node.path + "/"))
    return qs.filter(pk=node.pk)


def _delete_nodes_with_blobs(queryset, *, reason: str, workspace_id=None):
    node_ids = list(queryset.values_list("id", flat=True))
    if not node_ids:
        return 0

    file_names = list(
        FileBlob.objects.filter(node_id__in=node_ids)
        .exclude(file="")
        .values_list("file", flat=True)
    )
    for file_name in file_names:
        if default_storage.exists(file_name):
            default_storage.delete(file_name)

    deleted_count, _ = Node.objects.filter(id__in=node_ids).delete()
    logger.info(
        "Files permanently deleted: reason=%s, workspace_id=%s, count=%s, node_ids=%s",
        reason,
        workspace_id,
        deleted_count,
        node_ids,
    )
    return deleted_count


def purge_expired_trash(workspace):
    expired_qs = Node.objects.filter(
        workspace=workspace,
        trashed=True,
        deleted_at__lte=_trash_cutoff(),
    )
    return _delete_nodes_with_blobs(expired_qs, reason="trash_expired", workspace_id=workspace.id)


def _with_file_relations(qs):
    return qs.select_related("parent", "blob", "parse_result").prefetch_related(
        "parse_result__chunks",
    )

def get_workspace_files(workspace, q=None, parent_id=None, tag=None):
    qs = Node.objects.filter(workspace=workspace, trashed=False).order_by("-created_at")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
    elif tag:
        qs = qs.filter(parse_result__auto_tags__contains=[tag])
    else:
        if parent_id:
            # Frontend sends UUID as parent_id
            qs = qs.filter(parent__uid=parent_id)
        else:
            qs = qs.filter(parent__isnull=True)
    return _with_file_relations(qs)

def create_folder(workspace, owner, name, parent=None):
    return Node.objects.create(
        workspace=workspace, owner=owner, name=name, ext="", node_type=NodeType.FOLDER, parent=parent
    )

def toggle_star_status(node):
    node.starred = not node.starred
    node.save()
    return node.starred

def move_to_trash(node):
    deleted_at = timezone.now()
    affected = _subtree_queryset(node).update(
        trashed=True,
        ai_processing_enabled=False,
        deleted_at=deleted_at,
        updated_at=deleted_at,
    )
    node.refresh_from_db(fields=["trashed", "ai_processing_enabled", "deleted_at", "updated_at"])
    logger.info(
        "File moved to trash: node_id=%s, owner_id=%s, name=%s, affected=%s",
        node.id,
        node.owner_id,
        node.name,
        affected,
    )
    return node

def get_recent_files(workspace, limit=20):
    qs = Node.objects.filter(workspace=workspace, node_type=NodeType.FILE, trashed=False).order_by("-updated_at")
    return _with_file_relations(qs)[:limit]

def get_starred_files(workspace):
    qs = Node.objects.filter(workspace=workspace, starred=True, trashed=False).order_by("-created_at")
    return _with_file_relations(qs)

def get_trashed_files(workspace):
    purge_expired_trash(workspace)
    qs = (
        Node.objects.filter(workspace=workspace, trashed=True)
        .filter(Q(parent__isnull=True) | Q(parent__trashed=False))
        .order_by("-deleted_at")
    )
    return _with_file_relations(qs)

def restore_file(node):
    expiration_time = node.deleted_at + TRASH_RETENTION_PERIOD if node.deleted_at else None
    if expiration_time is not None and expiration_time <= timezone.now():
        permanent_delete(node)
        raise ValueError("This item can no longer be restored because the 7-day retention period has expired.")

    restored_at = timezone.now()
    affected = _subtree_queryset(node).update(
        trashed=False,
        deleted_at=None,
        updated_at=restored_at,
    )
    node.refresh_from_db(fields=["trashed", "deleted_at", "updated_at"])
    logger.info(
        "File restored from trash: node_id=%s, owner_id=%s, name=%s, affected=%s",
        node.id,
        node.owner_id,
        node.name,
        affected,
    )
    return node

def permanent_delete(node):
    return _delete_nodes_with_blobs(
        _subtree_queryset(node), reason="permanent_delete", workspace_id=node.workspace_id
    )

def empty_trash(workspace):
    return _delete_nodes_with_blobs(
        Node.objects.filter(workspace=workspace, trashed=True), reason="empty_trash", workspace_id=workspace.id
    )
