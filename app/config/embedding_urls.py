from django.urls import path

from document_ai import executor_views
from document_ai.embedding import internal_views

# Used only by the one process that owns the BGE-M3 model (embedding-executor's
# gunicorn worker, see DOTORI_EMBEDDING_MODEL_PROCESS in docker-compose.yml and
# ROOT_URLCONF branching in settings.py). No user-facing routes exist here at
# all -- not admin, not files, not accounts -- so there is nothing else to leak
# even if this urlconf were ever reached from an unexpected process.
urlpatterns = [
    path("livez", executor_views.livez, name="livez"),
    path("readyz", executor_views.readyz, name="readyz"),
    path("embed/", internal_views.embed, name="embed"),
    path("internal/v1/jobs/execute/", executor_views.execute_job, name="executor-job-execute"),
    path("internal/v1/jobs/<str:job_id>/", executor_views.job_status, name="executor-job-status"),
]
