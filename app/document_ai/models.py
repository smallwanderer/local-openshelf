from django.db import models
from pgvector.django import VectorField, HnswIndex

from config.enums import AIStatus, FileLanguage

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

    # Execution Result
    timings = models.JSONField(default=dict, blank=True)
    errors = models.JSONField(default=list, blank=True)
    
    # Optional Debug / Reproducibility Metadata
    metadata = models.JSONField(default=dict, blank=True)

    # Management Fields
    parsed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
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
            "metadata": self.metadata,
            "parsed_at": self.parsed_at.isoformat() if self.parsed_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

class DocumentChunk(models.Model):
    parse_result = models.ForeignKey(
        "document_ai.DocumentParseResult",
        on_delete=models.CASCADE,
        related_name="chunks",
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

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["chunk_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["parse_result", "chunk_index"],
                name="uniq_chunk_index_per_parse_result",
            )
        ]
        indexes = [
            models.Index(fields=["parse_result", "chunk_index"]),
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
            "created_at": self.created_at.isoformat(),
        }


class ChunkEmbedding(models.Model):
    """
    bge-m3 embedding 결과 저장 모델
    - 어떤 chunk를 어떤 모델로 벡터화했는지 저장
    """

    chunk = models.ForeignKey(
        "document_ai.DocumentChunk",
        on_delete=models.CASCADE,
        related_name="embeddings",
    )

    model_name = models.CharField(max_length=128, default="BAAI/bge-m3")
    model_version = models.CharField(max_length=32, blank=True)

    # dimension ?? 
    vector = VectorField(dimensions=1024, null=True, blank=True)
    sparse_vector = models.JSONField(default=dict, blank=True)

    # Status
    status = models.CharField(
        max_length=32,
        choices=AIStatus.choices,
        default=AIStatus.PENDING,
        db_index=True,
    )

    # Error
    error_message = models.CharField(max_length=255, blank=True)

    embedded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["chunk", "model_name", "model_version"],
                name="uniq_embedding_per_chunk_model_version",
            )
        ]
        indexes = [
            models.Index(fields=["model_name"]),
            HnswIndex(
                name='chunk_embedding_vector_hnsw_idx',
                fields=['vector'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops']
            )
        ]

    def __str__(self):
        return f"{self.chunk.parse_result.node.name} - {self.model_name}"

    def to_dict(self):
        return {
            "id": self.id,
            "chunk_id": self.chunk_id,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "sparse_terms": len(self.sparse_vector or {}),
            "embedded_at": self.embedded_at.isoformat() if self.embedded_at else None,
            "created_at": self.created_at.isoformat(),
        }
