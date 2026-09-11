"""Workspace-scoped persistence and authorization for ordinary RAG conversations."""

from __future__ import annotations

import uuid

from django.db import transaction
from django.db.models import F, Max
from django.utils import timezone

from document_ai.models import RAGConversation, RAGMessage
from files.models import Node
from workspaces.models import WorkspaceMembership


class ConversationNotFound(Exception):
    pass


class ConversationPermissionDenied(Exception):
    pass


class ConversationRevisionConflict(Exception):
    pass


class ConversationIdempotencyConflict(Exception):
    pass


class InvalidConversationScope(Exception):
    def __init__(self, invalid_node_ids):
        self.invalid_node_ids = invalid_node_ids
        super().__init__("Conversation scope contains invalid or unavailable nodes.")


def _is_workspace_admin(user, workspace) -> bool:
    return WorkspaceMembership.objects.filter(
        user=user, workspace=workspace, status=WorkspaceMembership.STATUS_ACTIVE,
        role=WorkspaceMembership.ROLE_ADMIN,
    ).exists()


def get_conversation(*, workspace, uid, include_deleted: bool = False) -> RAGConversation:
    queryset = RAGConversation.objects.filter(workspace=workspace, uid=uid)
    if not include_deleted:
        queryset = queryset.filter(deleted_at__isnull=True)
    conversation = queryset.first()
    if conversation is None:
        raise ConversationNotFound
    return conversation


def can_manage_conversation(*, user, conversation: RAGConversation) -> bool:
    return conversation.created_by_id == user.id or _is_workspace_admin(user, conversation.workspace)


def require_manager(*, user, conversation: RAGConversation) -> None:
    if not can_manage_conversation(user=user, conversation=conversation):
        raise ConversationPermissionDenied


def validate_conversation_node_ids(*, workspace, node_ids: list[str] | None) -> list[str]:
    normalized = []
    invalid = []
    for value in node_ids or []:
        try:
            normalized_value = str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            invalid.append(str(value))
            continue
        if normalized_value not in normalized:
            normalized.append(normalized_value)
    existing = {
        str(value)
        for value in Node.objects.filter(
            workspace=workspace, uid__in=normalized, trashed=False
        ).values_list("uid", flat=True)
    }
    invalid.extend(value for value in normalized if value not in existing)
    if invalid:
        raise InvalidConversationScope(invalid)
    return normalized


def create_conversation(*, workspace, user, title: str = "", default_node_ids: list[str] | None = None):
    validated_node_ids = validate_conversation_node_ids(
        workspace=workspace, node_ids=default_node_ids
    )
    return RAGConversation.objects.create(
        workspace=workspace, created_by=user, title=title.strip()[:160],
        default_node_ids=validated_node_ids,
    )


def update_conversation(*, conversation, user, title=None, default_node_ids=None, expected_revision: int):
    require_manager(user=user, conversation=conversation)
    updates = {"revision": F("revision") + 1, "updated_at": timezone.now()}
    if title is not None:
        updates["title"] = title.strip()[:160]
    if default_node_ids is not None:
        updates["default_node_ids"] = validate_conversation_node_ids(
            workspace=conversation.workspace, node_ids=default_node_ids
        )
    updated = RAGConversation.objects.filter(
        pk=conversation.pk, revision=expected_revision, deleted_at__isnull=True
    ).update(**updates)
    if updated != 1:
        raise ConversationRevisionConflict
    conversation.refresh_from_db()
    return conversation


def delete_conversation(*, conversation, user) -> RAGConversation:
    require_manager(user=user, conversation=conversation)
    if conversation.rag_jobs.filter(status__in=["pending", "processing"]).exists():
        raise RuntimeError("CONVERSATION_BUSY")
    conversation.deleted_at = timezone.now()
    conversation.save(update_fields=["deleted_at", "updated_at"])
    return conversation


def append_user_message(*, conversation, user, content: str, client_request_id, node_ids: list[str]):
    """Persist an idempotent user turn; generation is attached separately."""
    require_manager(user=user, conversation=conversation)
    with transaction.atomic():
        locked = RAGConversation.objects.select_for_update().get(pk=conversation.pk)
        existing = RAGMessage.objects.filter(
            conversation=locked, role=RAGMessage.ROLE_USER, client_request_id=client_request_id,
        ).first()
        if existing is not None:
            if existing.content != content or existing.node_ids != node_ids:
                raise ConversationIdempotencyConflict
            return existing, False
        sequence = (RAGMessage.objects.filter(conversation=locked).aggregate(maximum=Max("sequence"))["maximum"] or 0) + 1
        message = RAGMessage.objects.create(
            conversation=locked, sequence=sequence, role=RAGMessage.ROLE_USER,
            created_by=user, content=content, client_request_id=client_request_id, node_ids=node_ids,
        )
        locked.save(update_fields=["updated_at"])
    return message, True
