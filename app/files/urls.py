# files/urls.py
from django.urls import path
from uuid import UUID
from files.views import page_views, healthcheck
from files.api_v1 import file_views as api_views

app_name = "files"

urlpatterns = [
    # ==========================
    # HTML Page Routes (Frontend Shells)
    # ==========================
    path("", page_views.index, name="index"),
    path("upload/", page_views.upload, name="page_upload"),
    path("recent/", page_views.recent, name="page_recent"),
    path("starred/", page_views.starred, name="page_starred"),
    path("trash/", page_views.trash, name="page_trash"),
    path("rag/", page_views.rag_workspace, name="page_rag"),
    path("ai/history/", page_views.ai_search_history, name="page_ai_history"),
    path("<uuid:uid>/", page_views.node_view, name="page_node"),

    # ==========================
    # API Routes (Backend JSON)
    # ==========================
    path("api/v1/files/", api_views.file_list, name="api_list"),
    path("api/v1/upload/", api_views.upload_file, name="api_upload"),
    path("api/v1/create_folder/", api_views.create_folder, name="api_create_folder"),
    path("api/v1/folders/", api_views.all_folders, name="api_all_folders"),
    path("api/v1/rag/scope-nodes/", api_views.rag_scope_nodes, name="api_rag_scope_nodes"),
    path("api/v1/storage/", api_views.get_storage_usage, name="api_storage_usage"),
    path("api/v1/ai/readiness/", api_views.ai_readiness, name="api_ai_readiness"),
    path("api/v1/ai/search-history/", api_views.recent_search_history, name="api_search_history"),
    path("api/v1/bulk/delete/", api_views.bulk_delete, name="api_bulk_delete"),
    path("api/v1/bulk/restore/", api_views.bulk_restore, name="api_bulk_restore"),
    path("api/v1/bulk/move/", api_views.bulk_move, name="api_bulk_move"),
    path("api/v1/<uuid:uid>/", api_views.file_detail, name="api_detail"),
    path("api/v1/<uuid:uid>/parsed_text/", api_views.get_parsed_text, name="api_parsed_text"),
    path("api/v1/<uuid:uid>/meta/", api_views.update_meta, name="api_update_meta"),
    path("api/v1/<uuid:uid>/rename/", api_views.rename_file, name="api_rename"),
    path("api/v1/<uuid:uid>/move/", api_views.move_file, name="api_move"),
    path("api/v1/<uuid:uid>/ai/retry/", api_views.retry_ai_processing, name="api_retry_ai"),
    path("api/v1/<uuid:uid>/download/", api_views.file_download, name="api_download"),
    path("api/v1/<uuid:uid>/delete/", api_views.file_delete, name="api_delete"),
    path("api/v1/recent/", api_views.recent_files, name="api_recent"),
    path("api/v1/starred/", api_views.starred_files, name="api_starred"),
    path("api/v1/trash/", api_views.trash_files, name="api_trash"),
    path("api/v1/trash/empty/", api_views.empty_trash, name="api_empty_trash"),
    path("api/v1/<uuid:uid>/restore/", api_views.restore_file, name="api_restore"),
    path("api/v1/<uuid:uid>/permanent_delete/", api_views.permanent_delete, name="api_permanent_delete"),
    path("api/v1/toggle_star/<uuid:uid>/", api_views.toggle_star, name="api_toggle_star"),

    # ==========================
    # Healthcheck (Docker)
    # ==========================
    path("healthcheck/", healthcheck.healthcheck, name="healthcheck"),
]
