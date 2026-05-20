from django.urls import path
from document_ai.search.views import (
    SandboxPageView,
    VectorSearchJobView,
    VectorSearchView,
    VectorSandboxView,
)

app_name = "document_ai"

urlpatterns = [
    path("v1/search/", VectorSearchView.as_view(), name="vector-search"),
    path("v1/search/jobs/<int:job_id>/", VectorSearchJobView.as_view(), name="vector-search-job"),
    path("v1/tuning/", VectorSandboxView.as_view(), name="vector-tuning"),
    path("sandbox/", SandboxPageView.as_view(), name="sandbox-page"),
]
