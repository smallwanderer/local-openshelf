from django.urls import path
from document_ai.search.views import (
    RAGStreamView,
    SandboxPageView,
    VectorSearchView,
    VectorSandboxView,
)
from document_ai.status_views import server_policy
from document_ai.conversation_views import (
    ConversationDetailView,
    ConversationListView,
    ConversationMessageListView,
)
from document_ai.operation_views import (
    collect_operation_resources,
    operation_events,
    operation_metrics,
    operation_resources,
    operation_status,
    operation_trace,
)

app_name = "document_ai"

urlpatterns = [
    path("v1/search/", VectorSearchView.as_view(), name="vector-search"),
    path("v1/rag/stream/", RAGStreamView.as_view(), name="rag-stream"),
    path("v1/rag/conversations/", ConversationListView.as_view(), name="rag-conversations"),
    path("v1/rag/conversations/<uuid:uid>/", ConversationDetailView.as_view(), name="rag-conversation"),
    path("v1/rag/conversations/<uuid:uid>/messages/", ConversationMessageListView.as_view(), name="rag-conversation-messages"),
    path("v1/rag/conversations/<uuid:conversation_uid>/messages/stream/", RAGStreamView.as_view(), name="rag-conversation-stream"),
    path("v1/server-policy/", server_policy, name="server-policy"),
    path("v1/operations/status/", operation_status, name="operation-status"),
    path("v1/operations/metrics/", operation_metrics, name="operation-metrics"),
    path("v1/operations/events/", operation_events, name="operation-events"),
    path("v1/operations/traces/<str:trace_id>/", operation_trace, name="operation-trace"),
    path("v1/operations/resources/", operation_resources, name="operation-resources"),
    path("v1/operations/resources/collect/", collect_operation_resources, name="collect-operation-resources"),
    path("v1/tuning/", VectorSandboxView.as_view(), name="vector-tuning"),
    path("sandbox/", SandboxPageView.as_view(), name="sandbox-page"),
    # Internal embedding endpoints (livez/readyz/embed) live only in
    # config.embedding_urls, served only by embedding-executor's model-owning
    # process. They are deliberately absent from this urlconf.
]
