from django.urls import path

from document_ai.cli_views import (
    CLIRAGStreamView,
    CLIVectorSearchView,
    cli_identity,
    cli_status,
)
from files.cli_views import cli_document_list, cli_upload


app_name = "cli_api"

urlpatterns = [
    path("identity/", cli_identity, name="identity"),
    path("status/", cli_status, name="status"),
    path("documents/", cli_document_list, name="documents"),
    path("upload/", cli_upload, name="upload"),
    path("search/", CLIVectorSearchView.as_view(), name="search"),
    path("ask/stream/", CLIRAGStreamView.as_view(), name="ask-stream"),
]
