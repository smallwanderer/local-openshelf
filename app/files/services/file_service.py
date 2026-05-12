from datetime import timedelta

from django.core.files.storage import default_storage
from django.db.models import Q
from django.utils import timezone

from files.models import FileBlob, Node, NodeType

TRASH_RETENTION_DAYS = 7
TRASH_RETENTION_PERIOD = timedelta(days=TRASH_RETENTION_DAYS)


def _trash_cutoff():
    return timezone.now() - TRASH_RETENTION_PERIOD


def _subtree_queryset(node):
    qs = Node.objects.filter(owner=node.owner)
    if node.node_type == NodeType.FOLDER:
        return qs.filter(Q(pk=node.pk) | Q(path__startswith=node.path + "/"))
    return qs.filter(pk=node.pk)


def _delete_nodes_with_blobs(queryset):
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
    return deleted_count


def purge_expired_trash(user):
    expired_qs = Node.objects.filter(
        owner=user,
        trashed=True,
        deleted_at__lte=_trash_cutoff(),
    )
    return _delete_nodes_with_blobs(expired_qs)


def _with_file_relations(qs):
    return qs.select_related("blob", "parse_result").prefetch_related("parse_result__chunks")

def get_user_files(user, q=None, parent_id=None):
    qs = Node.objects.filter(owner=user, trashed=False).order_by("-created_at")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
    else:
        if parent_id:
            # Frontend sends UUID as parent_id
            qs = qs.filter(parent__uid=parent_id)
        else:
            qs = qs.filter(parent__isnull=True)
    return _with_file_relations(qs)

def create_folder(user, name, parent=None):
    return Node.objects.create(owner=user, name=name, ext="", node_type=NodeType.FOLDER, parent=parent)

def toggle_star_status(node):
    node.starred = not node.starred
    node.save()
    return node.starred

def move_to_trash(node):
    deleted_at = timezone.now()
    _subtree_queryset(node).update(
        trashed=True,
        deleted_at=deleted_at,
        updated_at=deleted_at,
    )
    node.refresh_from_db(fields=["trashed", "deleted_at", "updated_at"])
    return node

def get_recent_files(user, limit=20):
    qs = Node.objects.filter(owner=user, node_type=NodeType.FILE, trashed=False).order_by("-updated_at")
    return _with_file_relations(qs)[:limit]

def get_starred_files(user):
    qs = Node.objects.filter(owner=user, starred=True, trashed=False).order_by("-created_at")
    return _with_file_relations(qs)

def get_trashed_files(user):
    purge_expired_trash(user)
    qs = (
        Node.objects.filter(owner=user, trashed=True)
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
    _subtree_queryset(node).update(
        trashed=False,
        deleted_at=None,
        updated_at=restored_at,
    )
    node.refresh_from_db(fields=["trashed", "deleted_at", "updated_at"])
    return node

def permanent_delete(node):
    return _delete_nodes_with_blobs(_subtree_queryset(node))

def empty_trash(user):
    return _delete_nodes_with_blobs(Node.objects.filter(owner=user, trashed=True))
