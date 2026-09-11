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
    LLMEndpoint,
    QueryUnderstandingLog,
    RAGJob,
    ResourceSnapshot,
    SearchJob,
    UserLLMPreference,
)
from document_ai.orchestration import enqueue_embedding
from document_ai.tracing_utils import enqueue_kwargs


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
        return int(ChunkEmbedding.objects.filter(chunk=obj).exists())

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
            enqueue_embedding(node_id, **enqueue_kwargs())
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
        "status",
        "dense_dim",
        "sparse_terms",
        "embedded_at",
        "updated_at",
    )
    list_filter = (
        "status",
        "embedded_at",
        "created_at",
    )
    search_fields = (
        "chunk__parse_result__node__name",
        "chunk__parse_result__node__owner__email",
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


@admin.register(QueryUnderstandingLog)
class QueryUnderstandingLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "owner_email",
        "mode",
        "intent",
        "semantic_query_preview",
        "retrieval_required",
        "confidence",
        "source",
        "status",
        "created_at",
    )
    list_filter = (
        "mode",
        "intent",
        "answer_mode",
        "retrieval_required",
        "source",
        "status",
        "created_at",
    )
    search_fields = (
        "raw_query",
        "normalized_query",
        "semantic_query",
        "reason",
        "owner__email",
        "error_message",
    )
    readonly_fields = (
        "owner",
        "mode",
        "raw_query",
        "normalized_query",
        "semantic_query",
        "intent",
        "answer_mode",
        "retrieval_required",
        "confidence",
        "reason",
        "source",
        "status",
        "warnings_pretty",
        "classification_pretty",
        "query_dsl_pretty",
        "orm_pretty",
        "raw_result_pretty",
        "error_message",
        "created_at",
    )
    raw_id_fields = ("owner",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("owner",)

    @admin.display(description="Owner")
    def owner_email(self, obj):
        return obj.owner.email

    @admin.display(description="Semantic query")
    def semantic_query_preview(self, obj):
        return Truncator(obj.semantic_query).chars(80) if obj.semantic_query else "-"

    @admin.display(description="Warnings")
    def warnings_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.warnings, length=2000))

    @admin.display(description="Classification")
    def classification_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.classification, length=2000))

    @admin.display(description="QueryDSL")
    def query_dsl_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.query_dsl, length=3000))

    @admin.display(description="ORM")
    def orm_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.orm, length=3000))

    @admin.display(description="Raw result")
    def raw_result_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.raw_result, length=4000))


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
        "query_log",
        "query",
        "tuning_params_pretty",
        "results_pretty",
        "performance_metrics_pretty",
        "error_message",
        "task_id",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
        "duration",
        "result_count",
    )
    raw_id_fields = ("owner", "query_log")
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

    @admin.display(description="Performance metrics")
    def performance_metrics_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.performance_metrics, length=4000))

@admin.register(RAGJob)
class RAGJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "owner_email",
        "question_preview",
        "status",
        "stage",
        "query_intent",
        "search_status",
        "llm_model",
        "citation_count",
        "task_id",
        "created_at",
        "completed_at",
    )
    list_filter = (
        "status",
        "stage",
        "query_intent",
        "answer_mode",
        "retrieval_required",
        "language",
        "llm_endpoint_name",
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
        "stage_message",
        "llm_endpoint_name",
        "llm_model",
    )
    readonly_fields = (
        "owner",
        "search_job",
        "query_log",
        "question",
        "retrieval_query",
        "query_intent",
        "answer_mode",
        "retrieval_required",
        "query_confidence",
        "answer",
        "citations_pretty",
        "performance_metrics_pretty",
        "error_message",
        "stage",
        "stage_message",
        "llm_endpoint_name",
        "llm_base_url",
        "llm_model",
        "task_id",
        "started_at",
        "completed_at",
        "created_at",
        "updated_at",
        "citation_count",
    )
    raw_id_fields = ("owner", "search_job", "query_log", "llm_endpoint")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("owner", "search_job", "llm_endpoint")

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

    @admin.display(description="Performance metrics")
    def performance_metrics_pretty(self, obj):
        return format_html("<pre>{}</pre>", _json_preview(obj.performance_metrics, length=4000))

@admin.register(LLMEndpoint)
class LLMEndpointAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "workspace",
        "owner",
        "endpoint_type",
        "base_url",
        "default_model",
        "is_active",
        "updated_at",
    )
    list_filter = ("endpoint_type", "is_active", "created_at", "updated_at")
    search_fields = ("name", "base_url", "default_model", "workspace__name", "owner__email")
    raw_id_fields = ("workspace", "owner")
    readonly_fields = ("created_at", "updated_at", "last_checked_at", "last_check_status", "last_check_message")
    ordering = ("workspace__name", "name")


@admin.register(UserLLMPreference)
class UserLLMPreferenceAdmin(admin.ModelAdmin):
    list_display = ("id", "workspace", "user", "rag_endpoint", "rag_model", "updated_at")
    search_fields = ("workspace__name", "user__email", "rag_model", "rag_endpoint__name")
    raw_id_fields = ("workspace", "user", "rag_endpoint")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ResourceSnapshot)
class ResourceSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "service",
        "cpu_percent",
        "mem_mb",
        "gpu_mem_mb",
        "db_connections",
        "disk_free_mb",
        "collected_at",
    )
    list_filter = ("service",)
    date_hierarchy = "collected_at"
    ordering = ("-collected_at",)
    readonly_fields = ("collected_at",)


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
