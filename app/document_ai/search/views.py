from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from drf_yasg.utils import swagger_auto_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from document_ai.models import SearchJob
from document_ai.search.serializers import (
    SearchJobCreateResponseSerializer,
    SearchJobSerializer,
    VectorSearchRequestSerializer,
    VectorSearchResponseSerializer,
)
from document_ai.tasks import perform_vector_search


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
        job = SearchJob.objects.create(
            owner=request.user,
            query=serializer.validated_data["query"],
            top_k=serializer.validated_data.get("top_k", 5),
            threshold=serializer.validated_data.get("threshold"),
            node_ids=[str(node_id) for node_id in node_ids],
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
