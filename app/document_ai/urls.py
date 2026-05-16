from django.urls import path
from document_ai.search.views import VectorSearchJobView, VectorSearchView

app_name = "document_ai"

urlpatterns = [
    path("v1/search/", VectorSearchView.as_view(), name="vector-search"),
    path("v1/search/jobs/<int:job_id>/", VectorSearchJobView.as_view(), name="vector-search-job"),
]
