from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView

from drf_yasg.utils import swagger_auto_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from config.enums import AIStatus
from document_ai.models import RAGJob, SearchJob
from document_ai.search.serializers import (
    RAGJobCreateResponseSerializer,
    RAGJobSerializer,
    RAGRequestSerializer,
    SearchJobCreateResponseSerializer,
    SearchJobSerializer,
    VectorSearchRequestSerializer,
    VectorSearchResponseSerializer,
    VectorTuningRequestSerializer,
)
from document_ai.search.query_frontend import prepare_retrieval_query
from document_ai.tasks import generate_rag_response, perform_vector_search
from files.models import Node, NodeType


EMPTY_SCOPE_SENTINEL = "00000000-0000-0000-0000-000000000000"


def _expand_scope_node_ids(user, node_ids) -> list[str]:
    if not node_ids:
        return []

    requested_uids = [str(node_id) for node_id in node_ids]
    nodes = list(
        Node.objects.filter(
            owner=user,
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
            owner=user,
            node_type=NodeType.FILE,
            trashed=False,
            path__startswith=f"{folder_path}/",
            blob__isnull=False,
        ).values_list("uid", flat=True)
        file_uids.update(str(uid) for uid in descendant_uids)

    return sorted(file_uids) or [EMPTY_SCOPE_SENTINEL]


@method_decorator(csrf_exempt, name="dispatch")
class VectorSearchView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="Vector search",
        operation_description="Queue a vector search job. Query embedding runs in a Celery worker.",
        request_body=VectorSearchRequestSerializer,
        responses={202: SearchJobCreateResponseSerializer()},
    )
    def post(self, request, *args, **kwargs):
        if not getattr(request.user, "email_verified", False):
            return Response(
                {"error": "Email verification required"},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = VectorSearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        node_ids = serializer.validated_data.get("node_ids") or []
        scoped_node_ids = _expand_scope_node_ids(request.user, node_ids)
        query_plan = prepare_retrieval_query(
            serializer.validated_data["query"],
            mode="search",
        )
        job = SearchJob.objects.create(
            owner=request.user,
            query=query_plan.retrieval_query,
            top_k=serializer.validated_data.get("top_k", 5),
            threshold=serializer.validated_data.get("threshold"),
            node_ids=scoped_node_ids,
        )
        async_result = perform_vector_search.apply_async(args=[job.id], queue="search")
        job.task_id = async_result.id
        job.save(update_fields=["task_id"])

        return Response(
            {
                "job_id": job.id,
                "status": job.status,
                "poll_url": request.build_absolute_uri(f"/api/document-ai/v1/search/jobs/{job.id}/"),
            },
            status=status.HTTP_202_ACCEPTED,
        )


@method_decorator(csrf_exempt, name="dispatch")
class VectorSearchJobView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="Vector search job status",
        operation_description="Return queued vector search job status and results when completed.",
        responses={200: SearchJobSerializer()},
    )
    def get(self, request, job_id: int, *args, **kwargs):
        try:
            job = SearchJob.objects.get(id=job_id, owner=request.user)
        except SearchJob.DoesNotExist:
            return Response(
                {"error": "Search job not found"},
                status=status.HTTP_404_NOT_FOUND,
            )


        return Response(SearchJobSerializer(job).data, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name="dispatch")
class VectorSandboxView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="Vector search sandbox (Tuning)",
        operation_description="Run vector search with custom tuning parameters.",
        request_body=VectorTuningRequestSerializer,
        responses={202: SearchJobCreateResponseSerializer()},
    )
    def post(self, request, *args, **kwargs):
        serializer = VectorTuningRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tuning_keys = [
            "dense_weight", "sparse_weight", "candidate_multiplier",
            "per_node_candidate_cap", "query_sparse_top_n", "evidence_top_k",
            "pool_top_k", "pool_tau", "doc_length_penalty_alpha",
            "evidence_context_window"
        ]
        tuning_params = {k: v for k, v in serializer.validated_data.items() if k in tuning_keys}

        job = SearchJob.objects.create(
            owner=request.user,
            query=serializer.validated_data["query"],
            top_k=serializer.validated_data.get("top_k", 5),
            tuning_params=tuning_params,
        )
        async_result = perform_vector_search.apply_async(args=[job.id], queue="search")
        job.task_id = async_result.id
        job.save(update_fields=["task_id"])

        return Response(
            {
                "job_id": job.id,
                "status": job.status,
                "poll_url": request.build_absolute_uri(f"/api/document-ai/v1/search/jobs/{job.id}/"),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class SandboxPageView(LoginRequiredMixin, TemplateView):
    template_name = "document_ai/sandbox.html"


@method_decorator(csrf_exempt, name="dispatch")
class RAGView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="RAG answer",
        operation_description="Queue a vector search job and a RAG answer job. Answer generation uses the rag LLM worker.",
        request_body=RAGRequestSerializer,
        responses={202: RAGJobCreateResponseSerializer()},
    )
    def post(self, request, *args, **kwargs):
        if not getattr(request.user, "email_verified", False):
            return Response(
                {"error": "Email verification required"},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = RAGRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        node_ids = serializer.validated_data.get("node_ids") or []
        scoped_node_ids = _expand_scope_node_ids(request.user, node_ids)
        question = serializer.validated_data["question"]
        top_k = serializer.validated_data.get("top_k", getattr(settings, "RAG_SEARCH_TOP_K", 3))
        threshold = serializer.validated_data.get(
            "threshold",
            getattr(settings, "RAG_RETRIEVAL_THRESHOLD", None),
        )
        query_plan = prepare_retrieval_query(question, mode="rag")

        search_job = SearchJob.objects.create(
            owner=request.user,
            query=query_plan.retrieval_query,
            top_k=top_k,
            threshold=threshold,
            node_ids=scoped_node_ids,
        )
        async_result = perform_vector_search.apply_async(args=[search_job.id], queue="search")
        search_job.task_id = async_result.id
        search_job.save(update_fields=["task_id"])

        rag_job = RAGJob.objects.create(
            owner=request.user,
            search_job=search_job,
            question=question,
            top_k=top_k,
            language=serializer.validated_data.get("language", "ko"),
            node_ids=[str(node_id) for node_id in node_ids],
        )

        return Response(
            {
                "job_id": rag_job.id,
                "search_job_id": search_job.id,
                "status": rag_job.status,
                "poll_url": request.build_absolute_uri(f"/api/document-ai/v1/rag/jobs/{rag_job.id}/"),
            },
            status=status.HTTP_202_ACCEPTED,
        )


@method_decorator(csrf_exempt, name="dispatch")
class RAGJobView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="RAG job status",
        operation_description="Return RAG job status and queue answer generation after search is completed.",
        responses={200: RAGJobSerializer()},
    )
    def get(self, request, job_id: int, *args, **kwargs):
        try:
            rag_job = RAGJob.objects.select_related("search_job").get(id=job_id, owner=request.user)
        except RAGJob.DoesNotExist:
            return Response(
                {"error": "RAG job not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        self._maybe_queue_answer(rag_job)
        rag_job.refresh_from_db()
        if rag_job.search_job_id:
            rag_job.search_job.refresh_from_db()
        return Response(RAGJobSerializer(rag_job).data, status=status.HTTP_200_OK)

    def _maybe_queue_answer(self, rag_job: RAGJob):
        if rag_job.status != AIStatus.PENDING or rag_job.task_id:
            return

        search_job = rag_job.search_job
        if not search_job:
            rag_job.status = AIStatus.FAILED
            rag_job.error_message = "Search job is missing."
            rag_job.save(update_fields=["status", "error_message"])
            return

        if search_job.status == AIStatus.FAILED:
            rag_job.status = AIStatus.FAILED
            rag_job.completed_at = search_job.completed_at
            rag_job.error_message = search_job.error_message or "Search failed."
            rag_job.save(update_fields=["status", "completed_at", "error_message"])
            return

        if search_job.status != AIStatus.COMPLETED:
            return

        async_result = generate_rag_response.apply_async(args=[rag_job.id], queue="rag")
        rag_job.task_id = async_result.id
        rag_job.save(update_fields=["task_id"])
