import json
import uuid

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView, View

from drf_yasg.utils import swagger_auto_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.api_responses import api_error_response, session_access_error
from document_ai.embedding.providers.base import EmbeddingBusyError
from document_ai.search.serializers import (
    RAGRequestSerializer,
    SynchronousSearchResponseSerializer,
    VectorSearchRequestSerializer,
    VectorTuningRequestSerializer,
)
from document_ai.search.execution import search_documents_sync
from document_ai.search.profiles import (
    get_effective_retrieval_config,
    profile_threshold_to_retriever,
    retrieval_tuning_params,
)
from document_ai.parsers.text_utils import normalize_extracted_text
from document_ai.services.llm_endpoint_service import build_rag_llm_snapshot
from document_ai.services.rag_admission import (
    acquire_rag_admission_token_async,
    rag_retry_after_seconds,
)
from document_ai.services.rag_runtime_config import (
    LLMRuntimeNotConfigured,
    server_rag_runtime_availability,
)
from files.models import Node, NodeType


EMPTY_SCOPE_SENTINEL = "00000000-0000-0000-0000-000000000000"


def _workspace_access_error(request):
    error = session_access_error(request.user)
    if error is not None:
        return error
    if getattr(request, "workspace", None) is None:
        return api_error_response("WORKSPACE_REQUIRED", "No active workspace.", status=403)
    return None


def _llm_runtime_unavailable_response(runtime_status: dict | None = None) -> JsonResponse:
    detail = runtime_status or {}
    reason_code = detail.get("reason_code") or "LLM_RUNTIME_NOT_CONFIGURED"
    if reason_code == "LLM_UNAVAILABLE_OOM":
        message = (
            "메모리 부족으로 로컬 LLM 답변 생성이 비활성화되었습니다. "
            "문서 검색은 계속 사용할 수 있습니다."
        )
    elif reason_code == "LLM_UNAVAILABLE_TIMEOUT":
        message = (
            "로컬 LLM이 제한 시간 안에 준비되지 않아 답변 생성이 비활성화되었습니다. "
            "문서 검색은 계속 사용할 수 있습니다."
        )
    else:
        message = (
            "현재 로컬 LLM을 사용할 수 없어 답변을 생성할 수 없습니다. "
            "문서 검색은 계속 사용할 수 있습니다."
        )
    return JsonResponse(
        {
            "ok": False,
            "error": {
                "code": "LLM_RUNTIME_UNAVAILABLE",
                "reason": reason_code,
                "message": message,
                "search_available": True,
                "retryable": bool(detail.get("retryable", True)),
                "recovery_command": "python3 install.py --retry-llm",
                "details": {
                    "reason": reason_code,
                    "search_available": True,
                    "retryable": bool(detail.get("retryable", True)),
                    "recovery_command": "python3 install.py --retry-llm",
                },
            }
        },
        status=503,
    )


def _expand_scope_node_ids(user, node_ids, *, workspace=None) -> list[str]:
    if not node_ids:
        return []

    requested_uids = [str(node_id) for node_id in node_ids]
    if workspace is None:
        membership = user.workspace_memberships.filter(
            status="active", workspace__kind="personal"
        ).select_related("workspace").first()
        workspace = membership.workspace if membership else None
    nodes = list(
        Node.objects.filter(
            workspace=workspace,
            uid__in=requested_uids,
            trashed=False,
        ).only("uid", "node_type", "path")
    )
    if not nodes:
        return [EMPTY_SCOPE_SENTINEL]

    file_uids = {str(node.uid) for node in nodes if node.node_type == NodeType.FILE}
    folder_paths = [node.path.rstrip("/") for node in nodes if node.node_type == NodeType.FOLDER]

    for folder_path in folder_paths:
        descendant_uids = Node.objects.filter(
            workspace=workspace,
            node_type=NodeType.FILE,
            trashed=False,
            path__startswith=f"{folder_path}/",
            blob__isnull=False,
        ).values_list("uid", flat=True)
        file_uids.update(str(uid) for uid in descendant_uids)

    return sorted(file_uids) or [EMPTY_SCOPE_SENTINEL]


class VectorSearchView(APIView):
    permission_classes = [permissions.AllowAny]

    @swagger_auto_schema(
        operation_summary="Vector search",
        operation_description="Run an authenticated vector search and return results directly.",
        request_body=VectorSearchRequestSerializer,
        responses={200: SynchronousSearchResponseSerializer()},
    )
    def post(self, request, *args, **kwargs):
        auth_error = _workspace_access_error(request)
        if auth_error is not None:
            return auth_error

        serializer = VectorSearchRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error_response(
                "INVALID_REQUEST",
                "Request validation failed.",
                status=400,
                details=dict(serializer.errors),
            )

        mode = serializer.validated_data["mode"]
        raw_query = serializer.validated_data["query"]
        node_ids = serializer.validated_data.get("node_ids") or []
        scoped_node_ids = _expand_scope_node_ids(request.user, node_ids, workspace=request.workspace)
        normalized_query = normalize_extracted_text(raw_query).strip()
        # "advanced" used to run the query through the LLM query-understanding
        # pipeline (query_frontend.prepare_retrieval_query); that pipeline was
        # evaluated and deprecated for underperforming direct retrieval, so
        # "advanced" is kept as an accepted API value but now runs the same
        # direct pipeline as "basic".
        retrieval_query = normalized_query
        orm = {}
        query_plan_payload = {
            "mode": mode,
            "source": "direct",
            "retrieval_query": retrieval_query,
            "intent": "",
            "confidence": None,
            "warnings": [],
            "filters": [],
            "sorts": [],
        }
        try:
            retrieval_config = get_effective_retrieval_config(request.workspace)
            top_k = (
                serializer.validated_data["top_k"]
                if "top_k" in request.data
                else retrieval_config["search_top_k"]
            )
            threshold = (
                serializer.validated_data.get("threshold")
                if "threshold" in request.data
                else profile_threshold_to_retriever(retrieval_config["retrieval_threshold"])
            )
            results, metrics = search_documents_sync(
                owner=request.user,
                workspace=request.workspace,
                query=retrieval_query,
                top_k=top_k,
                threshold=threshold,
                node_ids=scoped_node_ids,
                tuning_params=retrieval_tuning_params(retrieval_config),
                orm_constraints={
                    "filter_kwargs": orm.get("filter_kwargs") or {},
                    "exclude_kwargs": orm.get("exclude_kwargs") or {},
                    "order_by": orm.get("order_by") or [],
                },
            )
        except EmbeddingBusyError as exc:
            response = api_error_response(
                "EMBEDDING_BUSY",
                str(exc),
                status=503,
                details={"retryable": True},
            )
            response["Retry-After"] = str(exc.retry_after_seconds)
            return response
        return Response(
            {
                "results": results,
                "performance_metrics": metrics,
                "query_plan": query_plan_payload,
            },
            status=status.HTTP_200_OK,
        )


@method_decorator(csrf_exempt, name="dispatch")
class VectorSandboxView(APIView):
    permission_classes = [permissions.AllowAny]

    @swagger_auto_schema(
        operation_summary="Vector search sandbox (Tuning)",
        operation_description="Run vector search with custom tuning parameters.",
        request_body=VectorTuningRequestSerializer,
        responses={200: SynchronousSearchResponseSerializer()},
    )
    def post(self, request, *args, **kwargs):
        auth_error = _workspace_access_error(request)
        if auth_error is not None:
            return auth_error

        serializer = VectorTuningRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return api_error_response(
                "INVALID_REQUEST",
                "Request validation failed.",
                status=400,
                details=dict(serializer.errors),
            )

        tuning_keys = [
            "dense_weight", "sparse_weight", "candidate_multiplier",
            "per_node_candidate_cap", "query_sparse_top_n", "evidence_top_k",
            "pool_top_k", "pool_tau", "doc_length_penalty_alpha",
            "evidence_context_window"
        ]
        tuning_params = {k: v for k, v in serializer.validated_data.items() if k in tuning_keys}

        try:
            results, metrics = search_documents_sync(
                owner=request.user,
                workspace=request.workspace,
                query=serializer.validated_data["query"],
                top_k=serializer.validated_data.get("top_k", 5),
                tuning_params=tuning_params,
            )
        except EmbeddingBusyError as exc:
            response = api_error_response(
                "EMBEDDING_BUSY",
                str(exc),
                status=503,
                details={"retryable": True},
            )
            response["Retry-After"] = str(exc.retry_after_seconds)
            return response
        return Response({"results": results, "performance_metrics": metrics}, status=status.HTTP_200_OK)


class SandboxPageView(LoginRequiredMixin, TemplateView):
    template_name = "document_ai/sandbox.html"


class RAGStreamView(View):
    """Run retrieval synchronously and stream final-answer deltas as NDJSON."""

    http_method_names = ["post"]

    async def post(self, request, *args, **kwargs):
        user = await request.auser()
        auth_error = session_access_error(user)
        if auth_error is not None:
            return auth_error
        if getattr(request, "workspace", None) is None:
            return api_error_response("WORKSPACE_REQUIRED", "No active workspace.", status=403)

        try:
            request_data = json.loads(request.body or b"{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return api_error_response(
                "INVALID_REQUEST",
                "Malformed JSON request body.",
                status=400,
            )
        if not isinstance(request_data, dict):
            return api_error_response(
                "INVALID_REQUEST",
                "Expected a JSON object.",
                status=400,
            )

        serializer = RAGRequestSerializer(data=request_data)
        if not serializer.is_valid():
            return api_error_response(
                "INVALID_REQUEST",
                "Request validation failed.",
                status=400,
                details=dict(serializer.errors),
            )

        try:
            llm_snapshot = await sync_to_async(
                build_rag_llm_snapshot, thread_sensitive=True
            )(request.workspace)
        except LLMRuntimeNotConfigured:
            return _llm_runtime_unavailable_response()
        if not llm_snapshot.get("llm_endpoint"):
            runtime_available, runtime_status = await sync_to_async(
                server_rag_runtime_availability, thread_sensitive=False
            )()
            if not runtime_available:
                return _llm_runtime_unavailable_response(runtime_status)

        # Resolve the target before admission. Local and external endpoints
        # have independent non-blocking gates, so external traffic never
        # consumes calibrated local GPU slots (or vice versa).
        admission_token = await acquire_rag_admission_token_async(llm_snapshot)
        if admission_token is None:
            response = JsonResponse(
                {
                    "ok": False,
                    "error": {
                        "code": "RAG_CAPACITY_EXCEEDED",
                        "message": "RAG 생성 요청이 많아 잠시 후 다시 시도해 주세요.",
                        "details": {},
                    }
                },
                status=503,
            )
            response["Retry-After"] = str(rag_retry_after_seconds())
            return response

        conversation = None
        reply_to = None
        handed_off = False
        try:
            node_ids = serializer.validated_data.get("node_ids") or []
            conversation_uid = kwargs.get("conversation_uid")
            if conversation_uid is not None:
                from document_ai.services.rag_conversation_service import (
                    ConversationIdempotencyConflict,
                    ConversationNotFound,
                    ConversationPermissionDenied,
                    append_user_message,
                    get_conversation,
                )

                try:
                    conversation = await sync_to_async(get_conversation, thread_sensitive=True)(
                        workspace=request.workspace, uid=conversation_uid
                    )
                    if "node_ids" not in request_data:
                        node_ids = list(conversation.default_node_ids or [])
                    try:
                        request_id = uuid.UUID(str(request_data.get("client_request_id")))
                    except (TypeError, ValueError, AttributeError):
                        return api_error_response(
                            "INVALID_REQUEST", "client_request_id must be a UUID.", status=400
                        )
                    reply_to, created = await sync_to_async(
                        append_user_message, thread_sensitive=True
                    )(
                        conversation=conversation,
                        user=user,
                        content=serializer.validated_data["question"],
                        client_request_id=request_id,
                        node_ids=[str(node_id) for node_id in node_ids],
                    )
                    if not created:
                        return api_error_response(
                            "IDEMPOTENCY_REPLAY",
                            "This message was already submitted; reload the conversation.",
                            status=409,
                        )
                except ConversationIdempotencyConflict:
                    return api_error_response(
                        "IDEMPOTENCY_CONFLICT",
                        "client_request_id was already used for a different message.",
                        status=409,
                    )
                except ConversationNotFound:
                    return api_error_response("NOT_FOUND", "Conversation not found.", status=404)
                except ConversationPermissionDenied:
                    return api_error_response(
                        "PERMISSION_DENIED", "You cannot continue this conversation.", status=403
                    )
            scoped_node_ids = await sync_to_async(
                _expand_scope_node_ids, thread_sensitive=True
            )(user, node_ids, workspace=request.workspace)
            question = serializer.validated_data["question"]
            retrieval_config = await sync_to_async(
                get_effective_retrieval_config, thread_sensitive=True
            )(request.workspace)
            top_k = (
                serializer.validated_data["top_k"]
                if "top_k" in request_data
                else retrieval_config["rag_search_top_k"]
            )
            threshold = (
                serializer.validated_data.get("threshold")
                if "threshold" in request_data
                else await sync_to_async(profile_threshold_to_retriever, thread_sensitive=True)(
                    retrieval_config["retrieval_threshold"]
                )
            )
            retrieval_query = normalize_extracted_text(question).strip()
            from document_ai.rag.streaming import (
                RAGSearchBusyError,
                RAGSearchError,
                create_rag_streaming_response_async,
            )

            # From here on, the streaming response owns the admission token.
            handed_off = True
            try:
                return await create_rag_streaming_response_async(
                    owner=user,
                    workspace=request.workspace,
                    question=question,
                    retrieval_query=retrieval_query,
                    top_k=top_k,
                    threshold=threshold,
                    tuning_params=retrieval_tuning_params(retrieval_config),
                    language=serializer.validated_data.get("language", "ko"),
                    requested_node_ids=node_ids,
                    scoped_node_ids=scoped_node_ids,
                    llm_snapshot=llm_snapshot,
                    admission_token=admission_token,
                    conversation=conversation,
                    reply_to=reply_to,
                )
            except RAGSearchBusyError as exc:
                response = api_error_response(
                    "EMBEDDING_BUSY",
                    str(exc),
                    status=503,
                    details={"retryable": True},
                )
                response["Retry-After"] = str(exc.retry_after_seconds)
                return response
            except RAGSearchError as exc:
                return api_error_response(
                    "SEARCH_FAILED",
                    str(exc),
                    status=500,
                )
        finally:
            if not handed_off:
                await admission_token.release_async()
