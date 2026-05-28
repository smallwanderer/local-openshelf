import json

from django.contrib import admin, messages
from django.contrib.admin.sites import NotRegistered
from django.utils import timezone
from django.utils.html import format_html
from django.utils.text import Truncator

from config.enums import AIStatus
from document_ai.models import (
    ChunkEmbedding,
    DocumentChunk,
    DocumentParseResult,
    RAGJob,
    SearchJob,
)
from document_ai.tasks import enqueue_embedding_tasks, generate_rag_response, perform_vector_search


def _format_duration(started_at, completed_at):
    if not started_at:
        return "-"
    end = completed_at or timezone.now()
    seconds = (end - started_at).total_seconds()
    return f"{seconds:.2f}s"


def _json_preview(value, *, length=500):
    if value in (None, "", [], {}):
        return "-"
    try:
        rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        rendered = str(value)
    return Truncator(rendered).chars(length)


class HasErrorsFilter(admin.SimpleListFilter):
    title = "has errors"
    parameter_name = "has_errors"

    def lookups(self, request, model_admin):
        return (
            ("yes", "Yes"),
            ("no", "No"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.exclude(errors=[])
        if self.value() == "no":
            return queryset.filter(errors=[])
        return queryset


@admin.register(DocumentParseResult)
class DocumentParseResultAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "node_name",
        "owner_email",
        "status",
        "parser_name",
        "parser_mode",
        "chunk_count",
        "input_page_count",
        "result_page_count",
        "recovery_attempts",
        "parsed_at",
        "updated_at",
    )
    list_filter = (
        "status",
        "parser_name",
        "parser_mode",
        HasErrorsFilter,
        "created_at",
        "updated_at",
    )
    search_fields = (
        "node__name",
        "node__owner__email",
        "input_document_hash",
    )
    readonly_fields = (
        "node",
        "timings_pretty",
        "errors_pretty",
        "metadata_pretty",
        "created_at",
        "updated_at",
        "parsed_at",
        "last_recovered_at",
    )
    raw_id_fields = ("node",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("node", "node__owner")

    @admin.display(description="File")
    def node_name(self, obj):
        return obj.node.name

    @admin.display(description="Owner")
    def owner_email(self, obj):
        return obj.node.owner.email

    @admin.display(description="Timings")
    def timings_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.timings, length=2000))

    @admin.display(description="Errors")
    def errors_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.errors, length=2000))

    @admin.display(description="Metadata")
    def metadata_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.metadata, length=2000))


@admin.register(DocumentChunk)
class DocumentChunkAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "node_name",
        "owner_email",
        "chunk_index",
        "status",
        "section_title",
        "pages",
        "token_count",
        "embedding_count",
        "recovery_attempts",
        "updated_at",
    )
    list_filter = (
        "status",
        "section_title",
        "created_at",
        "updated_at",
    )
    search_fields = (
        "text",
        "section_title",
        "parse_result__node__name",
        "parse_result__node__owner__email",
    )
    readonly_fields = (
        "parse_result",
        "text_preview",
        "chunk_meta_pretty",
        "error_message_pretty",
        "created_at",
        "updated_at",
        "last_recovered_at",
    )
    raw_id_fields = ("parse_result",)
    actions = ("requeue_embedding_for_selected_chunks",)
    date_hierarchy = "created_at"
    ordering = ("parse_result", "chunk_index")
    list_select_related = ("parse_result", "parse_result__node", "parse_result__node__owner")

    @admin.display(description="File")
    def node_name(self, obj):
        return obj.parse_result.node.name

    @admin.display(description="Owner")
    def owner_email(self, obj):
        return obj.parse_result.node.owner.email

    @admin.display(description="Pages")
    def pages(self, obj):
        if obj.page_from and obj.page_to:
            return f"{obj.page_from}-{obj.page_to}"
        return obj.page_from or "-"

    @admin.display(description="Embeddings")
    def embedding_count(self, obj):
        return obj.embeddings.count()

    @admin.display(description="Text preview")
    def text_preview(self, obj):
        return Truncator(obj.text or "").chars(500)

    @admin.display(description="Chunk metadata")
    def chunk_meta_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.chunk_meta, length=2000))

    @admin.display(description="Error message")
    def error_message_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.error_message, length=2000))

    @admin.action(description="Requeue embedding for selected pending/failed chunks")
    def requeue_embedding_for_selected_chunks(self, request, queryset):
        candidate_qs = queryset.filter(status__in=[AIStatus.PENDING, AIStatus.FAILED])
        node_ids = set(candidate_qs.values_list("parse_result__node_id", flat=True))
        updated = candidate_qs.update(status=AIStatus.PENDING, error_message={})
        for node_id in node_ids:
            enqueue_embedding_tasks.delay(node_id)
        self.message_user(
            request,
            f"Queued embedding for {updated} chunks across {len(node_ids)} files.",
            messages.SUCCESS,
        )


@admin.register(ChunkEmbedding)
class ChunkEmbeddingAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "node_name",
        "chunk_index",
        "model_name",
        "model_version",
        "status",
        "dense_dim",
        "sparse_terms",
        "embedded_at",
        "updated_at",
    )
    list_filter = (
        "status",
        "model_name",
        "model_version",
        "embedded_at",
        "created_at",
    )
    search_fields = (
        "chunk__parse_result__node__name",
        "chunk__parse_result__node__owner__email",
        "model_name",
        "model_version",
        "error_message",
    )
    readonly_fields = (
        "chunk",
        "dense_dim",
        "sparse_terms",
        "error_message",
        "embedded_at",
        "created_at",
        "updated_at",
    )
    raw_id_fields = ("chunk",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("chunk", "chunk__parse_result", "chunk__parse_result__node")

    @admin.display(description="File")
    def node_name(self, obj):
        return obj.chunk.parse_result.node.name

    @admin.display(description="Chunk")
    def chunk_index(self, obj):
        return obj.chunk.chunk_index

    @admin.display(description="Dense dim")
    def dense_dim(self, obj):
        return len(obj.vector or [])

    @admin.display(description="Sparse terms")
    def sparse_terms(self, obj):
        return len(obj.sparse_vector or {})


@admin.register(SearchJob)
class SearchJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "owner_email",
        "query_preview",
        "top_k",
        "status",
        "result_count",
        "duration",
        "task_id",
        "created_at",
        "completed_at",
    )
    list_filter = (
        "status",
        "created_at",
        "started_at",
        "completed_at",
    )
    search_fields = (
        "query",
        "owner__email",
        "task_id",
        "error_message",
    )
    readonly_fields = (
        "owner",
        "query",
        "tuning_params_pretty",
        "results_pretty",
        "error_message",
        "task_id",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
        "duration",
        "result_count",
    )
    raw_id_fields = ("owner",)
    actions = ("requeue_selected_search_jobs",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("owner",)

    @admin.display(description="Owner")
    def owner_email(self, obj):
        return obj.owner.email

    @admin.display(description="Query")
    def query_preview(self, obj):
        return Truncator(obj.query).chars(80)

    @admin.display(description="Results")
    def result_count(self, obj):
        return len(obj.results or [])

    @admin.display(description="Duration")
    def duration(self, obj):
        return _format_duration(obj.started_at, obj.completed_at)

    @admin.display(description="Tuning params")
    def tuning_params_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.tuning_params, length=2000))

    @admin.display(description="Results")
    def results_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.results, length=4000))

    @admin.action(description="Requeue selected failed/pending search jobs")
    def requeue_selected_search_jobs(self, request, queryset):
        candidate_qs = queryset.filter(status__in=[AIStatus.PENDING, AIStatus.FAILED])
        queued = 0
        for job in candidate_qs:
            async_result = perform_vector_search.apply_async(args=[job.id], queue="search")
            job.task_id = async_result.id
            job.status = AIStatus.PENDING
            job.error_message = ""
            job.started_at = None
            job.completed_at = None
            job.save(
                update_fields=[
                    "task_id",
                    "status",
                    "error_message",
                    "started_at",
                    "completed_at",
                ]
            )
            queued += 1
        self.message_user(request, f"Requeued {queued} search jobs.", messages.SUCCESS)


@admin.register(RAGJob)
class RAGJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "owner_email",
        "question_preview",
        "status",
        "search_status",
        "citation_count",
        "task_id",
        "created_at",
        "completed_at",
    )
    list_filter = (
        "status",
        "language",
        "created_at",
        "started_at",
        "completed_at",
    )
    search_fields = (
        "question",
        "answer",
        "owner__email",
        "task_id",
        "error_message",
    )
    readonly_fields = (
        "owner",
        "search_job",
        "question",
        "answer",
        "citations_pretty",
        "error_message",
        "task_id",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
        "citation_count",
    )
    raw_id_fields = ("owner", "search_job")
    actions = ("requeue_selected_rag_jobs",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("owner", "search_job")

    @admin.display(description="Owner")
    def owner_email(self, obj):
        return obj.owner.email

    @admin.display(description="Question")
    def question_preview(self, obj):
        return Truncator(obj.question).chars(80)

    @admin.display(description="Search")
    def search_status(self, obj):
        return obj.search_job.status if obj.search_job_id else "-"

    @admin.display(description="Citations")
    def citation_count(self, obj):
        return len(obj.citations or [])

    @admin.display(description="Citations")
    def citations_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.citations, length=4000))

    @admin.action(description="Requeue selected completed-search RAG jobs")
    def requeue_selected_rag_jobs(self, request, queryset):
        queued = 0
        for job in queryset.select_related("search_job"):
            if not job.search_job_id or job.search_job.status != AIStatus.COMPLETED:
                continue
            async_result = generate_rag_response.apply_async(args=[job.id], queue="rag")
            job.task_id = async_result.id
            job.status = AIStatus.PENDING
            job.error_message = ""
            job.answer = ""
            job.citations = []
            job.started_at = None
            job.completed_at = None
            job.save(
                update_fields=[
                    "task_id",
                    "status",
                    "error_message",
                    "answer",
                    "citations",
                    "started_at",
                    "completed_at",
                ]
            )
            queued += 1
        self.message_user(request, f"Requeued {queued} RAG jobs.", messages.SUCCESS)


try:
    from django_celery_results.models import TaskResult

    admin.site.unregister(TaskResult)
except (ImportError, NotRegistered):
    TaskResult = None


if TaskResult is not None:

    @admin.register(TaskResult)
    class TaskResultAdmin(admin.ModelAdmin):
        list_display = (
            "task_id",
            "task_name",
            "status",
            "worker",
            "duration",
            "date_created",
            "date_done",
            "result_preview",
        )
        list_filter = (
            "status",
            "task_name",
            "worker",
            "date_created",
            "date_done",
        )
        search_fields = (
            "task_id",
            "task_name",
            "worker",
            "result",
            "traceback",
        )
        readonly_fields = (
            "task_id",
            "task_name",
            "task_args",
            "task_kwargs",
            "status",
            "worker",
            "content_type",
            "content_encoding",
            "result_pretty",
            "traceback_pretty",
            "meta",
            "date_created",
            "date_done",
            "duration",
        )
        date_hierarchy = "date_created"
        ordering = ("-date_created",)
        actions = ("delete_successful_results",)

        @admin.display(description="Duration")
        def duration(self, obj):
            return _format_duration(obj.date_created, obj.date_done)

        @admin.display(description="Result")
        def result_preview(self, obj):
            return Truncator(obj.result or "").chars(120)

        @admin.display(description="Result")
        def result_pretty(self, obj):
            return format_html("<pre>{}</pre>", Truncator(obj.result or "-").chars(4000))

        @admin.display(description="Traceback")
        def traceback_pretty(self, obj):
            return format_html("<pre>{}</pre>", Truncator(obj.traceback or "-").chars(4000))

        @admin.action(description="Delete selected SUCCESS task results")
        def delete_successful_results(self, request, queryset):
            deleted, _ = queryset.filter(status="SUCCESS").delete()
            self.message_user(request, f"Deleted {deleted} successful task results.", messages.SUCCESS)
