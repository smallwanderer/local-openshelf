from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from config.enums import AIStatus, NodeType
from config.tracing import get_trace_id, new_trace_id, set_trace_id
from document_ai.db_span import capture_db_spans
from document_ai.models import DocumentChunk, DocumentParseResult, RAGJob, SearchJob
from document_ai.embedding.executor import (
    RetryableEmbeddingError,
    embed_document_chunk_sync,
    embed_document_chunks_batch_sync,
)
from document_ai.rag.generation import generate_rag_response_sync
from document_ai.tracing_utils import enqueue_kwargs
from files.models import FileBlob, Node

pytestmark = pytest.mark.integration

User = get_user_model()


class EmbeddingInstrumentationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="perf-embed@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        node = Node.objects.create(
            owner=self.user, name="doc.txt", ext=".txt", node_type=NodeType.FILE
        )
        with patch("document_ai.signals.enqueue_parse"):
            FileBlob.objects.create(
                node=node,
                original_name="doc.txt",
                file=SimpleUploadedFile("doc.txt", b"hello", content_type="text/plain"),
                mime_type="text/plain",
                size=5,
                status="ready",
            )
        self.parse_result = DocumentParseResult.objects.create(
            node=node, status=AIStatus.COMPLETED, chunk_count=1
        )

    def _create_chunk(self, **kwargs):
        defaults = dict(
            parse_result=self.parse_result,
            chunk_index=0,
            text="some chunk text",
            status=AIStatus.PROCESSING,
        )
        defaults.update(kwargs)
        return DocumentChunk.objects.create(**defaults)

    def test_success_path_records_trace_id_and_timings(self):
        chunk = self._create_chunk()
        enqueued_at = timezone.now().isoformat()
        fake_embedding = SimpleNamespace(dense_vector=[0.1, 0.2], sparse_vector={"1": 1.0})

        with patch(
            "document_ai.embedding.embeding_models.embed_document", return_value=fake_embedding
        ), patch("document_ai.embedding.store_registry.get_embedding_store_instance") as get_store:
            result = embed_document_chunk_sync(
                chunk.id, trace_id="fixed-trace-id", enqueued_at=enqueued_at
            )

        self.assertEqual(result["status"], "success")
        get_store.return_value.save_chunk_embedding.assert_called_once()
        chunk.refresh_from_db()
        metrics = chunk.performance_metrics
        self.assertEqual(metrics["trace_id"], "fixed-trace-id")
        self.assertIn("queue_wait_ms", metrics)
        self.assertIn("embedding_processing_ms", metrics)
        self.assertGreaterEqual(metrics["queue_wait_ms"], 0)

    def test_permanent_failure_records_metrics(self):
        chunk = self._create_chunk(text="")

        result = embed_document_chunk_sync(
            chunk.id, trace_id="fail-trace-id", enqueued_at=timezone.now().isoformat()
        )

        self.assertEqual(result["status"], "failed")
        chunk.refresh_from_db()
        metrics = chunk.performance_metrics
        self.assertEqual(metrics["trace_id"], "fail-trace-id")
        self.assertIn("queue_wait_ms", metrics)
        self.assertIn("embedding_processing_ms", metrics)

    def test_generates_trace_id_when_none_provided(self):
        chunk = self._create_chunk(text="")

        embed_document_chunk_sync(chunk.id)

        chunk.refresh_from_db()
        self.assertTrue(chunk.performance_metrics["trace_id"])

    def test_batch_success_path_embeds_all_chunks_in_one_call(self):
        chunk_a = self._create_chunk(chunk_index=0, text="chunk a")
        chunk_b = self._create_chunk(chunk_index=1, text="chunk b")
        enqueued_at = timezone.now().isoformat()
        calls = []

        def fake_embed_documents(texts, **kwargs):
            calls.append(list(texts))
            return [
                SimpleNamespace(dense_vector=[0.1, 0.2], sparse_vector={"1": 1.0})
                for _ in texts
            ]

        with patch(
            "document_ai.embedding.embeding_models.embed_documents", fake_embed_documents
        ), patch("document_ai.embedding.store_registry.get_embedding_store_instance") as get_store:
            result = embed_document_chunks_batch_sync(
                [chunk_a.id, chunk_b.id], trace_id="batch-trace-id", enqueued_at=enqueued_at
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["count"], 2)
        self.assertEqual(calls, [["chunk a", "chunk b"]])  # one model call, not two
        self.assertEqual(get_store.return_value.save_chunk_embedding.call_count, 2)
        for chunk in (chunk_a, chunk_b):
            chunk.refresh_from_db()
            self.assertEqual(chunk.status, AIStatus.COMPLETED)
            self.assertEqual(chunk.performance_metrics["trace_id"], "batch-trace-id")

    def test_batch_skips_empty_chunk_without_failing_rest_of_batch(self):
        chunk_a = self._create_chunk(chunk_index=0, text="chunk a")
        empty_chunk = self._create_chunk(chunk_index=1, text="   ")
        chunk_b = self._create_chunk(chunk_index=2, text="chunk b")
        calls = []

        def fake_embed_documents(texts, **kwargs):
            calls.append(list(texts))
            return [
                SimpleNamespace(dense_vector=[0.1, 0.2], sparse_vector={"1": 1.0})
                for _ in texts
            ]

        with patch(
            "document_ai.embedding.embeding_models.embed_documents", fake_embed_documents
        ), patch("document_ai.embedding.store_registry.get_embedding_store_instance"):
            result = embed_document_chunks_batch_sync(
                [chunk_a.id, empty_chunk.id, chunk_b.id],
                enqueued_at=timezone.now().isoformat(),
            )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["count"], 2)
        # The empty chunk never reaches the model call -- batch only carries
        # the two valid texts.
        self.assertEqual(calls, [["chunk a", "chunk b"]])
        empty_chunk.refresh_from_db()
        self.assertEqual(empty_chunk.status, AIStatus.FAILED)
        chunk_a.refresh_from_db()
        chunk_b.refresh_from_db()
        self.assertEqual(chunk_a.status, AIStatus.COMPLETED)
        self.assertEqual(chunk_b.status, AIStatus.COMPLETED)

    def test_batch_retries_whole_batch_on_runtime_error(self):
        chunk_a = self._create_chunk(chunk_index=0, text="chunk a")
        chunk_b = self._create_chunk(chunk_index=1, text="chunk b")

        def failing_embed_documents(texts, **kwargs):
            raise RuntimeError("embedding-executor is busy (EMBEDDING_BUSY), retry after 5s")

        with patch(
            "document_ai.embedding.embeding_models.embed_documents", failing_embed_documents
        ):
            with self.assertRaises(RetryableEmbeddingError):
                embed_document_chunks_batch_sync(
                    [chunk_a.id, chunk_b.id],
                    enqueued_at=timezone.now().isoformat(),
                    retries=0,
                    max_retries=3,
                )

        # Whole batch retries as one unit -- chunks stay PROCESSING, not
        # individually marked failed on a retryable error.
        for chunk in (chunk_a, chunk_b):
            chunk.refresh_from_db()
            self.assertEqual(chunk.status, AIStatus.PROCESSING)

    def test_batch_permanently_fails_all_chunks_when_retries_exhausted(self):
        chunk_a = self._create_chunk(chunk_index=0, text="chunk a")
        chunk_b = self._create_chunk(chunk_index=1, text="chunk b")

        def failing_embed_documents(texts, **kwargs):
            raise RuntimeError("GPU OOM while embedding batch")

        with patch(
            "document_ai.embedding.embeding_models.embed_documents", failing_embed_documents
        ):
            result = embed_document_chunks_batch_sync(
                [chunk_a.id, chunk_b.id],
                enqueued_at=timezone.now().isoformat(),
                retries=3,
                max_retries=3,
            )

        self.assertEqual(result["status"], "failed")
        for chunk in (chunk_a, chunk_b):
            chunk.refresh_from_db()
            self.assertEqual(chunk.status, AIStatus.FAILED)


class ParseInstrumentationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="perf-parse@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )

    def _create_file_node(self) -> Node:
        node = Node.objects.create(
            owner=self.user, name="parse-me.txt", ext=".txt", node_type=NodeType.FILE
        )
        with patch("document_ai.signals.enqueue_parse"):
            FileBlob.objects.create(
                node=node,
                original_name="parse-me.txt",
                file=SimpleUploadedFile("parse-me.txt", b"hello", content_type="text/plain"),
                mime_type="text/plain",
                size=5,
                status="ready",
            )
        return node

    def test_success_path_records_metrics_on_parse_result(self):
        from document_ai.processing.parsing import execute_parse_node

        node = self._create_file_node()
        fake_parse_result = SimpleNamespace(
            status="success",
            input_format="txt",
            input_document_hash="abc",
            input_page_count=1,
            page_count=1,
            chunks=[],
            errors=[],
            timings={},
            parser_mode="convert_string_md",
            parser_backend="docling",
            parser_version="1.0",
            file_ext=".txt",
        )

        parser = SimpleNamespace(
            spec=SimpleNamespace(backend="docling"),
            parse=lambda _path: fake_parse_result,
        )
        result = execute_parse_node(node.id, parser=parser, **enqueue_kwargs())

        self.assertEqual(result["status"], "success")
        node.parse_result.refresh_from_db()
        metrics = node.parse_result.performance_metrics
        self.assertIn("trace_id", metrics)
        self.assertIn("queue_wait_ms", metrics)
        self.assertIn("parse_processing_ms", metrics)

    def test_failure_path_records_metrics_on_parse_result(self):
        from document_ai.processing.parsing import ParseExecutionFailed, execute_parse_node

        node = self._create_file_node()

        parser = SimpleNamespace(
            spec=SimpleNamespace(backend="docling"),
            parse=lambda _path: (_ for _ in ()).throw(ValueError("boom")),
        )
        with self.assertRaises(ParseExecutionFailed):
            execute_parse_node(node.id, parser=parser, **enqueue_kwargs())

        node.parse_result.refresh_from_db()
        metrics = node.parse_result.performance_metrics
        self.assertTrue(metrics.get("failed"))
        self.assertIn("trace_id", metrics)
        self.assertIn("queue_wait_ms", metrics)
        self.assertIn("parse_processing_ms", metrics)


class TraceIdMiddlewareTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="trace-mw@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )

    def test_response_carries_trace_id_header(self):
        client = Client()
        client.force_login(self.user)

        response = client.get("/files/healthcheck/")

        self.assertIn("X-Trace-Id", response)
        self.assertTrue(response["X-Trace-Id"])


class DbSpanCaptureTests(TestCase):
    def test_captures_sql_without_leaking_params(self):
        set_trace_id(new_trace_id())
        secret_query = "super-secret-search-text"

        with self.assertLogs("db_span", level="INFO") as captured:
            with capture_db_spans():
                list(User.objects.filter(email=secret_query))

        self.assertTrue(captured.records)
        for record in captured.records:
            message = record.getMessage()
            self.assertNotIn(secret_query, message)
            self.assertIn("seq=", message)
            self.assertIn("duration_ms=", message)


class RagTokenThroughputTests(TestCase):
    """Verifies output_tokens_per_second is derived from the LLM's own reported
    usage (via stream_options.include_usage), not a local tokenizer estimate."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="rag-ips@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.server_target_patch = patch(
            "document_ai.services.llm_endpoint_service.get_cached_server_rag_target",
            return_value=SimpleNamespace(
                base_url="http://test-rag:8080",
                model="test-rag-model",
                as_snapshot=lambda: {
                    "llm_endpoint_name": "Test server runtime",
                    "llm_base_url": "http://test-rag:8080",
                    "llm_model": "test-rag-model",
                },
            ),
        )
        self.server_target_patch.start()
        self.addCleanup(self.server_target_patch.stop)

    def test_success_path_records_usage_and_throughput(self):
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="요약",
            top_k=3,
            status=AIStatus.COMPLETED,
            results=[
                {
                    "node_id": "11111111-1111-1111-1111-111111111111",
                    "node_name": "policy.pdf",
                    "file_ext": ".pdf",
                    "doc_score": 0.91,
                    "evidences": [
                        {
                            "chunk_id": 10,
                            "text": "정부는 공급 확대와 할인 지원을 병행한다.",
                            "context_text": "정부는 공급 확대와 할인 지원을 병행한다.",
                            "compressed_text": "압축 근거: 공급 확대와 할인 지원을 병행한다.",
                            "compression": {"enabled": True, "method": "embedding_lazy_segment"},
                            "section": "정책",
                            "pages": "1",
                            "distance": -0.91,
                            "dense_score": 0.8,
                            "sparse_score": 0.95,
                            "hybrid_score": 0.9,
                        }
                    ],
                }
            ],
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="요약",
            top_k=3,
            language="ko",
        )

        class FakeResponse:
            status_code = 200
            text = ""

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=False):
                return [
                    'data: {"choices":[{"delta":{"content":"final answer text"}}]}',
                    'data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":40}}',
                    "data: [DONE]",
                ]

        class FakeRedisNoCancel:
            def exists(self, key):
                return False

            def delete(self, key):
                return 0

        with patch("requests.post", return_value=FakeResponse()) as post, patch(
            "redis.Redis.from_url", return_value=FakeRedisNoCancel()
        ):
            result = generate_rag_response_sync(rag_job.id)

        self.assertEqual(result["status"], "success")
        request_payload = post.call_args.kwargs["json"]
        self.assertTrue(request_payload["stream_options"]["include_usage"])

        rag_job.refresh_from_db()
        metrics = rag_job.performance_metrics
        self.assertEqual(metrics["input_token_count"], 120)
        self.assertEqual(metrics["output_token_count"], 40)
        self.assertIn("output_tokens_per_second", metrics)
        self.assertGreater(metrics["output_tokens_per_second"], 0)
