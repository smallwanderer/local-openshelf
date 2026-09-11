import uuid

from django.db import models
from django.conf import settings
from pgvector.django import VectorField, HnswIndex

from config.enums import AIStatus, FileLanguage, QueryAnswerMode, QueryIntent, RAGStage


class ExecutorJobReceipt(models.Model):
    """Small durable receipt for executor HTTP dispatch.

    The receipt deliberately records job identity and terminal metadata only.
    Document contents and vectors remain in their owning tables.
    """

    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
    )

    job_id = models.CharField(max_length=64, primary_key=True)
    work_kind = models.CharField(max_length=64)
    target_key = models.CharField(max_length=160)
    payload_fingerprint = models.CharField(max_length=64)
    source_snapshot = models.JSONField(default=dict, blank=True)
    runtime_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    attempt = models.PositiveIntegerField(default=1)
    result = models.JSONField(default=dict, blank=True)
    error = models.JSONField(default=dict, blank=True)
    retryable = models.BooleanField(default=False)
    followups = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["work_kind", "target_key", "status"], name="docai_exec_target_status_idx"),
            models.Index(fields=["status", "updated_at"], name="docai_exec_status_updated_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["work_kind", "target_key"],
                condition=models.Q(status="running"),
                name="docai_exec_one_active_target",
            ),
        ]

class DocumentParseResult(models.Model):
    """
    문서 파싱 결과의 운용용 요약 모델
    
    Column Example:
        - `parser_name = "docling"`
        - `parser_mode = "convert_string_md"`
        - `status = "SUCCESS"`
        - `input_format = "md"`
        - `input_document_hash = "abc123..."`
        - `input_page_count = 5`
        - `result_page_count = 0`
        - `chunk_count = 12`
        - `timings = {...}`
        - `errors = []`
        - `parsed_at = 2026-04-06T15:00:00`
    
    Metadata Example:
        {
          "parser_version": "2.82.0",
          "tokenizer_name": "BAAI/bge-m3",
          "max_tokens": 1024,
          "file_ext": ".hwpx"
        }
    """

    node = models.OneToOneField(
        "files.Node",
        on_delete=models.CASCADE,
        related_name="parse_result",
    )

    # Parser Identification
    parser_name = models.CharField(
        max_length=64, default="docling"
    )
    parser_mode = models.CharField(
        max_length=64, blank=True
    )

    # Parser Status
    status = models.CharField(
        max_length=32,
        choices=AIStatus.choices,
        default=AIStatus.PENDING,
        db_index=True,
    )

    # Document Information
    input_format = models.CharField(max_length=32, blank=True)
    input_document_hash = models.CharField(max_length=64, blank=True)

    # Pages Statistics
    input_page_count = models.PositiveIntegerField(null=True, blank=True)
    result_page_count = models.PositiveIntegerField(null=True, blank=True)

    # Chunk Statistics
    chunk_count = models.PositiveIntegerField(default=0)

    # One server-wide embedding contract applies to the whole document.
    # Keeping this marker here avoids repeating a generation FK on every
    # chunk embedding row.
    embedding_generation_id = models.CharField(max_length=96, blank=True)
    embedding_runtime_fingerprint = models.CharField(max_length=64, blank=True)

    # Execution Result
    timings = models.JSONField(default=dict, blank=True)
    errors = models.JSONField(default=list, blank=True)

    # trace_id, queue_wait_ms, parse_processing_ms 등 앱 레벨 성능 계측치.
    # `timings`(Docling 자체 내부 프로파일링)과는 별개.
    performance_metrics = models.JSONField(default=dict, blank=True)
    
    # Optional Debug / Reproducibility Metadata
    metadata = models.JSONField(default=dict, blank=True)

    # AI Summary and Tags
    summary = models.TextField(blank=True, default="")
    auto_tags = models.JSONField(default=list, blank=True)

    # Management Fields
    parsed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Recovery Tracking
    recovery_attempts = models.PositiveIntegerField(
        default=0,
        help_text="복구 태스크가 재큐잉한 누적 횟수. MAX 초과 시 복구 후보에서 제외됩니다.",
    )
    last_recovered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="마지막으로 복구 태스크가 재큐잉한 시각.",
    )

    class Meta:
        indexes = [
            models.Index(fields=["parser_name"]),
            models.Index(fields=["input_document_hash"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.node.name} - ParseResult"

    def to_dict(self):
        return {
            "id": self.id,
            "node_id": self.node_id,
            "parser_name": self.parser_name,
            "parser_mode": self.parser_mode,
            "status": self.status,
            "input_format": self.input_format,
            "input_document_hash": self.input_document_hash,
            "input_page_count": self.input_page_count,
            "result_page_count": self.result_page_count,
            "chunk_count": self.chunk_count,
            "timings": self.timings,
            "errors": self.errors,
            "performance_metrics": self.performance_metrics,
            "metadata": self.metadata,
            "summary": self.summary,
            "auto_tags": self.auto_tags,
            "parsed_at": self.parsed_at.isoformat() if self.parsed_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

class DocumentChunk(models.Model):
    parse_result = models.ForeignKey(
        "document_ai.DocumentParseResult",
        on_delete=models.CASCADE,
        related_name="chunks",
        db_index=False,
    )

    chunk_index = models.PositiveIntegerField()
    text = models.TextField()
    token_count = models.PositiveIntegerField(null=True, blank=True)

    section_title = models.CharField(max_length=255, blank=True)
    page_from = models.PositiveIntegerField(null=True, blank=True)
    page_to = models.PositiveIntegerField(null=True, blank=True)

    chunk_meta = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=32,
        choices=AIStatus.choices,
        default=AIStatus.PENDING,
        db_index=True,
    )
    error_message = models.JSONField(default=dict, blank=True)

    # trace_id, queue_wait_ms, embedding_processing_ms 등 앱 레벨 성능 계측치.
    performance_metrics = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Recovery Tracking
    recovery_attempts = models.PositiveIntegerField(
        default=0,
        help_text="임베딩 복구 태스크가 재큐잉한 누적 횟수. MAX 초과 시 복구 후보에서 제외됩니다.",
    )
    last_recovered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="마지막으로 임베딩 복구 태스크가 재큐잉한 시각.",
    )

    class Meta:
        ordering = ["chunk_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["parse_result", "chunk_index"],
                name="uniq_chunk_index_per_parse_result",
            )
        ]

    def __str__(self):
        return f"{self.parse_result.node.name} - Chunk {self.chunk_index}"

    def to_dict(self):
        return {
            "id": self.id,
            "parse_result_id": self.parse_result_id,
            "chunk_index": self.chunk_index,
            "text": self.text,
            "section_title": self.section_title,
            "page_from": self.page_from,
            "page_to": self.page_to,
            "token_count": self.token_count,
            "chunk_meta": self.chunk_meta,
            "performance_metrics": self.performance_metrics,
            "created_at": self.created_at.isoformat(),
        }


class EmbeddingGeneration(models.Model):
    generation_id = models.CharField(max_length=96, unique=True)
    scope = models.CharField(max_length=32, default="production")
    runtime_fingerprint = models.CharField(max_length=64, blank=True)
    catalog_id = models.CharField(max_length=96, blank=True)
    model_id = models.CharField(max_length=128)
    model_revision = models.CharField(max_length=64, blank=True)
    provider = models.CharField(max_length=64)
    store = models.CharField(max_length=64)
    dimension = models.PositiveIntegerField(default=1024)
    supports_sparse = models.BooleanField(default=True)
    status = models.CharField(max_length=32, default="READY")
    expected_chunks = models.PositiveIntegerField(default=0)
    completed_chunks = models.PositiveIntegerField(default=0)
    failed_chunks = models.PositiveIntegerField(default=0)
    activated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["scope", "status"]),
        ]

    def __str__(self):
        return f"{self.scope}:{self.generation_id} ({self.status})"


class ChunkEmbedding(models.Model):
    """
    bge-m3 embedding 결과 저장 모델
    - 어떤 chunk를 어떤 모델로 벡터화했는지 저장
    """

    chunk = models.OneToOneField(
        "document_ai.DocumentChunk",
        on_delete=models.CASCADE,
        related_name="embedding",
    )

    # Multi-dimension vector fields for different embedding models
    # BGE-M3 (1024), Harrier (640), Granite-278m (768), Qwen/OpenAI (1536), Granite-107m/MiniLM (384)
    vector = VectorField(dimensions=1024, null=True, blank=True)
    vector_640 = VectorField(dimensions=640, null=True, blank=True)
    vector_768 = VectorField(dimensions=768, null=True, blank=True)
    vector_1536 = VectorField(dimensions=1536, null=True, blank=True)
    vector_384 = VectorField(dimensions=384, null=True, blank=True)
    sparse_vector = models.JSONField(default=dict, blank=True)

    # Status
    status = models.CharField(
        max_length=32,
        choices=AIStatus.choices,
        default=AIStatus.PENDING,
    )

    # Error
    error_message = models.CharField(max_length=255, blank=True)

    embedded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


    class Meta:
        indexes = [
            HnswIndex(
                name='chunk_embedding_active_hnsw_idx',
                fields=['vector'],
                m=16,
                ef_construction=64,
                opclasses=['vector_ip_ops']
            ),
        ]

    def __str__(self):
        return f"{self.chunk.parse_result.node.name} - embedding"

    def to_dict(self):
        return {
            "id": self.id,
            "chunk_id": self.chunk_id,
            "sparse_terms": len(self.sparse_vector or {}),
            "embedded_at": self.embedded_at.isoformat() if self.embedded_at else None,
            "created_at": self.created_at.isoformat(),
        }


class ChunkSegmentEmbedding(models.Model):
    """
    검색/RAG contextual compression에서 lazy 생성하는 segment embedding.
    chunk embedding과 동일한 임베딩 정책을 사용하므로 별도 모델 식별자는 저장하지 않는다.
    """

    chunk = models.ForeignKey(
        "document_ai.DocumentChunk",
        on_delete=models.CASCADE,
        related_name="segment_embeddings",
        db_index=False,
    )
    window_size = models.PositiveSmallIntegerField(default=2)
    segment_index = models.PositiveIntegerField()
    text = models.TextField()
    char_start = models.PositiveIntegerField(default=0)
    char_end = models.PositiveIntegerField(default=0)
    vector = VectorField(dimensions=1024, null=True, blank=True)
    sparse_vector = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["chunk", "window_size", "segment_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["chunk", "window_size", "segment_index"],
                name="uniq_segment_emb_per_chunk_window_index",
            )
        ]
        indexes = [
            models.Index(fields=["last_used_at"]),
        ]

    def __str__(self):
        return f"{self.chunk_id} - Segment {self.segment_index} (w={self.window_size})"


def _ensure_workspace_from_owner(instance):
    if instance.workspace_id or not instance.owner_id:
        return
    from workspaces.models import WorkspaceMembership

    instance.workspace_id = WorkspaceMembership.objects.filter(
        user_id=instance.owner_id,
        status=WorkspaceMembership.STATUS_ACTIVE,
        workspace__kind="personal",
    ).values_list("workspace_id", flat=True).first()
    if not instance.workspace_id:
        raise ValueError("A workspace is required.")


class QueryUnderstandingLog(models.Model):
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="query_understanding_logs",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="document_ai_query_understanding_logs",
    )
    mode = models.CharField(max_length=32, default="search", db_index=True)
    raw_query = models.TextField()
    normalized_query = models.TextField(blank=True)
    semantic_query = models.TextField(blank=True)
    intent = models.CharField(
        max_length=64,
        choices=QueryIntent.choices,
        default=QueryIntent.AMBIGUOUS,
        db_index=True,
    )
    answer_mode = models.CharField(
        max_length=64,
        choices=QueryAnswerMode.choices,
        default=QueryAnswerMode.AMBIGUOUS,
    )
    retrieval_required = models.BooleanField(default=True)
    confidence = models.FloatField(default=0.0)
    reason = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=32, default="success", db_index=True)
    warnings = models.JSONField(default=list, blank=True)
    classification = models.JSONField(default=dict, blank=True)
    query_dsl = models.JSONField(default=dict, blank=True)
    orm = models.JSONField(default=dict, blank=True)
    raw_result = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["workspace", "mode", "-created_at"], name="docai_qu_ws_mode_created_idx"),
            models.Index(fields=["owner", "mode", "-created_at"]),
            models.Index(fields=["intent", "-created_at"]),
            models.Index(fields=["retrieval_required", "-created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"QueryUnderstandingLog({self.id}) {self.intent}: {self.raw_query[:40]}"

    def save(self, *args, **kwargs):
        _ensure_workspace_from_owner(self)
        return super().save(*args, **kwargs)


class SearchJob(models.Model):
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="search_jobs",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="document_ai_search_jobs",
    )
    query_log = models.ForeignKey(
        "document_ai.QueryUnderstandingLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="search_jobs",
    )

    query = models.TextField()
    top_k = models.PositiveIntegerField(default=5)
    threshold = models.FloatField(null=True, blank=True)
    node_ids = models.JSONField(default=list, blank=True)
    tuning_params = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=32,
        choices=AIStatus.choices,
        default=AIStatus.PENDING,
        db_index=True,
    )
    task_id = models.CharField(max_length=255, blank=True)
    results = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    performance_metrics = models.JSONField(default=dict, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["workspace", "status", "-created_at"], name="docai_se_ws_status_created_idx"),
            models.Index(fields=["owner", "status", "-created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"SearchJob({self.id}) {self.status}: {self.query[:40]}"

    def save(self, *args, **kwargs):
        _ensure_workspace_from_owner(self)
        if self.query_log_id and self.query_log.workspace_id != self.workspace_id:
            raise ValueError("Search job and query log must belong to the same workspace.")
        return super().save(*args, **kwargs)


class LLMEndpoint(models.Model):
    """OpenAI-compatible LLM endpoint registered by a user."""

    ENDPOINT_OPENAI_COMPATIBLE = "openai_compatible"
    ENDPOINT_OLLAMA = "ollama"
    ENDPOINT_LLAMA_CPP = "llama_cpp"
    ENDPOINT_VLLM = "vllm"

    ENDPOINT_TYPE_CHOICES = [
        (ENDPOINT_OPENAI_COMPATIBLE, "OpenAI compatible"),
        (ENDPOINT_OLLAMA, "Ollama"),
        (ENDPOINT_LLAMA_CPP, "llama.cpp"),
        (ENDPOINT_VLLM, "vLLM"),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="document_ai_llm_endpoints",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="llm_endpoints",
    )
    name = models.CharField(max_length=128)
    endpoint_type = models.CharField(
        max_length=32,
        choices=ENDPOINT_TYPE_CHOICES,
        default=ENDPOINT_OPENAI_COMPATIBLE,
    )
    base_url = models.URLField(
        max_length=512,
        help_text="Base URL without /v1 suffix when the endpoint already exposes OpenAI-compatible routes.",
    )
    default_model = models.CharField(max_length=256)
    api_key = models.CharField(max_length=512, blank=True)
    is_active = models.BooleanField(default=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_check_status = models.CharField(max_length=32, blank=True)
    last_check_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name"],
                name="uniq_llm_endpoint_name_per_workspace",
            )
        ]
        indexes = [
            models.Index(fields=["owner", "is_active"]),
            models.Index(fields=["workspace", "is_active"], name="llmendpoint_ws_active_idx"),
        ]

    def save(self, *args, **kwargs):
        _ensure_workspace_from_owner(self)
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.owner or 'deleted user'})"

    @property
    def normalized_base_url(self) -> str:
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3].rstrip("/")
        return base_url

    @property
    def chat_completions_url(self) -> str:
        return f"{self.normalized_base_url}/v1/chat/completions"

    @property
    def responses_url(self) -> str:
        return f"{self.normalized_base_url}/v1/responses"


class UserLLMPreference(models.Model):
    """Workspace-wide defaults for LLM-backed document AI tasks.

    `user` is kept only as a legacy audit reference to whoever originally
    configured the preference; `workspace` is the sole scope going forward
    so team members share one RAG default.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="legacy_llm_preference",
    )
    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="llm_preference",
    )
    rag_endpoint = models.ForeignKey(
        "document_ai.LLMEndpoint",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_preferences",
    )
    rag_model = models.CharField(max_length=256, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.workspace_id and self.user_id:
            from workspaces.models import WorkspaceMembership

            self.workspace_id = WorkspaceMembership.objects.filter(
                user_id=self.user_id,
                status=WorkspaceMembership.STATUS_ACTIVE,
                workspace__kind="personal",
            ).values_list("workspace_id", flat=True).first()
        if not self.workspace_id:
            raise ValueError("A workspace is required.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"LLM preferences for {self.workspace}"

    def get_rag_model(self) -> str:
        if self.rag_model:
            return self.rag_model
        if self.rag_endpoint_id and self.rag_endpoint:
            return self.rag_endpoint.default_model
        return ""


class RAGJob(models.Model):
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="rag_jobs",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="document_ai_rag_jobs",
    )
    conversation = models.ForeignKey(
        "document_ai.RAGConversation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="rag_jobs",
    )
    search_job = models.ForeignKey(
        "document_ai.SearchJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_jobs",
    )
    query_log = models.ForeignKey(
        "document_ai.QueryUnderstandingLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_jobs",
    )

    question = models.TextField()
    retrieval_query = models.TextField(blank=True)
    query_intent = models.CharField(
        max_length=64,
        choices=QueryIntent.choices,
        default=QueryIntent.AMBIGUOUS,
        db_index=True,
    )
    answer_mode = models.CharField(
        max_length=64,
        choices=QueryAnswerMode.choices,
        default=QueryAnswerMode.RAG,
    )
    retrieval_required = models.BooleanField(default=True)
    query_confidence = models.FloatField(default=0.0)
    top_k = models.PositiveIntegerField(default=5)
    language = models.CharField(max_length=8, default="ko")
    node_ids = models.JSONField(default=list, blank=True)
    llm_endpoint = models.ForeignKey(
        "document_ai.LLMEndpoint",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_jobs",
    )
    llm_endpoint_name = models.CharField(max_length=128, blank=True)
    llm_base_url = models.URLField(max_length=512, blank=True)
    llm_model = models.CharField(max_length=256, blank=True)

    status = models.CharField(
        max_length=32,
        choices=AIStatus.choices,
        default=AIStatus.PENDING,
        db_index=True,
    )
    stage = models.CharField(
        max_length=32,
        choices=RAGStage.choices,
        default=RAGStage.QUEUED,
        db_index=True,
    )
    stage_message = models.CharField(max_length=255, blank=True)
    task_id = models.CharField(max_length=255, blank=True)
    answer = models.TextField(blank=True)
    citations = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)
    performance_metrics = models.JSONField(default=dict, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["workspace", "status", "-created_at"], name="docai_ra_ws_status_created_idx"),
            models.Index(fields=["owner", "status", "-created_at"]),
            models.Index(fields=["workspace", "owner", "status", "-completed_at"], name="docai_rag_ws_own_st_cmp_idx"),
            models.Index(fields=["workspace", "owner", "-created_at"], name="docai_rag_ws_own_created_idx"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"RAGJob({self.id}) {self.status}: {self.question[:40]}"

    def save(self, *args, **kwargs):
        _ensure_workspace_from_owner(self)
        if self.conversation_id and self.conversation.workspace_id != self.workspace_id:
            raise ValueError("RAG job and conversation must belong to the same workspace.")
        if self.search_job_id and self.search_job.workspace_id != self.workspace_id:
            raise ValueError("RAG and search jobs must belong to the same workspace.")
        if self.query_log_id and self.query_log.workspace_id != self.workspace_id:
            raise ValueError("RAG job and query log must belong to the same workspace.")
        return super().save(*args, **kwargs)


class RAGConversation(models.Model):
    """A workspace-scoped container for ordinary RAG turns.

    The workspace is the data-isolation boundary. ``created_by`` records who
    may continue or manage the conversation; it is deliberately nullable so
    account removal does not remove workspace history.
    """

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="rag_conversations"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_conversations",
    )
    title = models.CharField(max_length=160, blank=True)
    default_node_ids = models.JSONField(default=list, blank=True)
    revision = models.PositiveIntegerField(default=1)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(
                fields=["workspace", "-updated_at", "-id"],
                name="docai_conv_ws_updated_idx",
            ),
        ]

    def __str__(self):
        return f"RAGConversation({self.uid})"


class RAGMessage(models.Model):
    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_CHOICES = [(ROLE_USER, "User"), (ROLE_ASSISTANT, "Assistant")]

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    conversation = models.ForeignKey(
        RAGConversation, on_delete=models.CASCADE, related_name="messages"
    )
    sequence = models.PositiveIntegerField()
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_messages",
    )
    content = models.TextField(blank=True)
    client_request_id = models.UUIDField(null=True, blank=True)
    node_ids = models.JSONField(default=list, blank=True)
    reply_to = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="replies"
    )
    rag_job = models.OneToOneField(
        RAGJob,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="conversation_message",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sequence", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "sequence"], name="docai_msg_conversation_sequence_uniq"
            ),
            models.UniqueConstraint(
                fields=["conversation", "client_request_id"],
                condition=models.Q(role="user", client_request_id__isnull=False),
                name="docai_user_message_request_uniq",
            ),
        ]

    def __str__(self):
        return f"RAGMessage({self.conversation_id}:{self.sequence} {self.role})"

    def save(self, *args, **kwargs):
        if self.reply_to_id and self.reply_to.conversation_id != self.conversation_id:
            raise ValueError("Reply and message must belong to the same conversation.")
        if self.rag_job_id:
            if self.rag_job.conversation_id != self.conversation_id:
                raise ValueError("RAG job and message must belong to the same conversation.")
            if self.rag_job.workspace_id != self.conversation.workspace_id:
                raise ValueError("RAG job and message must belong to the same workspace.")
        return super().save(*args, **kwargs)


class ResourceSnapshot(models.Model):
    """
    운영자가 수동으로 실행하는 `collect_resource_snapshot` 명령이 남기는
    자원 사용량 한 줄 기록. 상시 수집(cron)은 하지 않으므로 row 수가 적어
    별도 보존정책이 필요 없다 (performance-and-reliability.md 참고).

    `service`는 `app`/`embedding-executor`/`db`/`redis`/`dotori-llm` 같은
    컨테이너 식별자이거나, 디스크 여유 공간처럼 컨테이너 단위가 아닌
    항목은 `disk:uploads`/`disk:logs`/`disk:config`처럼 구분한다.

    현재는 docker.sock 마운트 없이 수집 가능한 항목(DB connection 수,
    디스크 여유 공간)만 채워진다. `cpu_percent`/`mem_mb`/`gpu_mem_mb`는
    컨테이너별 `docker stats`/`nvidia-smi` 연동 전까지는 비워둔다.
    """

    service = models.CharField(max_length=64, db_index=True)
    cpu_percent = models.FloatField(null=True, blank=True)
    mem_mb = models.FloatField(null=True, blank=True)
    gpu_mem_mb = models.FloatField(null=True, blank=True)
    db_connections = models.PositiveIntegerField(null=True, blank=True)
    disk_free_mb = models.FloatField(null=True, blank=True)
    collected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["service", "-collected_at"]),
        ]
        ordering = ["-collected_at"]

    def __str__(self):
        return f"ResourceSnapshot({self.service}) at {self.collected_at.isoformat()}"
