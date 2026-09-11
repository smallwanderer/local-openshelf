from __future__ import annotations

from files.models import Node, NodeType
from workspaces.models import WorkspaceMembership


def is_active_member(membership, *, workspace_id: int) -> bool:
    return bool(
        membership
        and membership.workspace_id == workspace_id
        and membership.status == WorkspaceMembership.STATUS_ACTIVE
    )


def can_view_node(*, membership, node: Node) -> bool:
    return is_active_member(membership, workspace_id=node.workspace_id)


def can_modify_node(*, actor, membership, node: Node) -> bool:
    if not can_view_node(membership=membership, node=node):
        return False
    if membership.role == WorkspaceMembership.ROLE_ADMIN:
        return True
    return node.owner_id == actor.id


def can_modify_subtree(*, actor, membership, node: Node) -> bool:
    """Return whether actor may mutate node and every affected descendant.

    Folder rename/move/trash/restore/permanent-delete operations change the
    entire path subtree. A non-admin owner must not use ownership of the root
    folder to mutate documents created by another workspace member.
    """
    if not can_modify_node(actor=actor, membership=membership, node=node):
        return False
    if membership.role == WorkspaceMembership.ROLE_ADMIN or node.node_type != NodeType.FOLDER:
        return True
    return not (
        Node.objects.filter(
            workspace_id=node.workspace_id,
            path__startswith=f"{node.path}/",
        )
        .exclude(owner_id=actor.id)
        .exists()
    )
