from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.api_responses import api_error_response, session_access_error
from document_ai.models import RAGConversation, RAGMessage
from document_ai.services.rag_conversation_service import (
    ConversationNotFound, ConversationPermissionDenied, ConversationRevisionConflict,
    InvalidConversationScope, create_conversation, delete_conversation,
    get_conversation, update_conversation,
)


def _access_error(request):
    error = session_access_error(request.user)
    if error is not None:
        return error
    if getattr(request, "workspace", None) is None:
        return api_error_response("WORKSPACE_REQUIRED", "No active workspace.", status=403)
    return None


def _conversation_payload(conversation):
    return {
        "uid": str(conversation.uid), "title": conversation.title,
        "default_node_ids": conversation.default_node_ids, "revision": conversation.revision,
        "created_by_id": conversation.created_by_id, "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


def _message_payload(message):
    rag_job = message.rag_job
    return {
        "uid": str(message.uid), "sequence": message.sequence, "role": message.role,
        "content": message.content, "node_ids": message.node_ids,
        "reply_to_uid": str(message.reply_to.uid) if message.reply_to_id else None,
        "created_by_id": message.created_by_id, "created_at": message.created_at,
        "rag_job": None if rag_job is None else {
            "id": rag_job.id, "status": rag_job.status, "answer": rag_job.answer,
            "citations": rag_job.citations, "error_message": rag_job.error_message,
            "performance_metrics": rag_job.performance_metrics, "completed_at": rag_job.completed_at,
        },
    }


class ConversationListView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        if (error := _access_error(request)) is not None:
            return error
        conversations = RAGConversation.objects.filter(
            workspace=request.workspace, deleted_at__isnull=True
        ).order_by("-updated_at", "-id")[:50]
        return Response({"conversations": [_conversation_payload(item) for item in conversations]})

    def post(self, request):
        if (error := _access_error(request)) is not None:
            return error
        title = request.data.get("title", "")
        node_ids = request.data.get("default_node_ids", [])
        if not isinstance(title, str) or not isinstance(node_ids, list) or not all(isinstance(item, str) for item in node_ids):
            return api_error_response("INVALID_REQUEST", "Invalid conversation fields.", status=400)
        try:
            conversation = create_conversation(
                workspace=request.workspace, user=request.user, title=title, default_node_ids=node_ids
            )
        except InvalidConversationScope as exc:
            return api_error_response(
                "INVALID_DOCUMENT_SCOPE", "Conversation scope contains unavailable nodes.",
                status=400, details={"node_ids": exc.invalid_node_ids},
            )
        return Response({"conversation": _conversation_payload(conversation)}, status=status.HTTP_201_CREATED)


class ConversationDetailView(APIView):
    permission_classes = [permissions.AllowAny]

    def _get(self, request, uid):
        try:
            return get_conversation(workspace=request.workspace, uid=uid)
        except ConversationNotFound:
            return None

    def get(self, request, uid):
        if (error := _access_error(request)) is not None:
            return error
        conversation = self._get(request, uid)
        if conversation is None:
            return api_error_response("NOT_FOUND", "Conversation not found.", status=404)
        return Response({"conversation": _conversation_payload(conversation)})

    def patch(self, request, uid):
        if (error := _access_error(request)) is not None:
            return error
        conversation = self._get(request, uid)
        if conversation is None:
            return api_error_response("NOT_FOUND", "Conversation not found.", status=404)
        expected_revision = request.data.get("expected_revision")
        title = request.data.get("title")
        node_ids = request.data.get("default_node_ids")
        if not isinstance(expected_revision, int) or (title is not None and not isinstance(title, str)) or (node_ids is not None and (not isinstance(node_ids, list) or not all(isinstance(item, str) for item in node_ids))):
            return api_error_response("INVALID_REQUEST", "Invalid conversation update.", status=400)
        try:
            conversation = update_conversation(
                conversation=conversation, user=request.user, title=title,
                default_node_ids=node_ids, expected_revision=expected_revision,
            )
        except ConversationPermissionDenied:
            return api_error_response("PERMISSION_DENIED", "You cannot modify this conversation.", status=403)
        except ConversationRevisionConflict:
            return api_error_response("REVISION_CONFLICT", "Conversation was updated by another user.", status=409)
        except InvalidConversationScope as exc:
            return api_error_response(
                "INVALID_DOCUMENT_SCOPE", "Conversation scope contains unavailable nodes.",
                status=400, details={"node_ids": exc.invalid_node_ids},
            )
        return Response({"conversation": _conversation_payload(conversation)})

    def delete(self, request, uid):
        if (error := _access_error(request)) is not None:
            return error
        conversation = self._get(request, uid)
        if conversation is None:
            return api_error_response("NOT_FOUND", "Conversation not found.", status=404)
        try:
            delete_conversation(conversation=conversation, user=request.user)
        except ConversationPermissionDenied:
            return api_error_response("PERMISSION_DENIED", "You cannot delete this conversation.", status=403)
        except RuntimeError:
            return api_error_response("CONVERSATION_BUSY", "Cancel the active answer first.", status=409)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ConversationMessageListView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, uid):
        if (error := _access_error(request)) is not None:
            return error
        try:
            conversation = get_conversation(workspace=request.workspace, uid=uid)
        except ConversationNotFound:
            return api_error_response("NOT_FOUND", "Conversation not found.", status=404)
        messages = RAGMessage.objects.filter(conversation=conversation).select_related("rag_job", "reply_to")
        return Response({"messages": [_message_payload(message) for message in messages]})
