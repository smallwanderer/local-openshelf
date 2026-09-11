from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import requests
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from django.http import StreamingHttpResponse
from django.test import TestCase, override_settings

from config.enums import AIStatus, QueryAnswerMode, QueryIntent, RAGStage
from document_ai.models import LLMEndpoint, QueryUnderstandingLog, RAGJob, SearchJob, UserLLMPreference
from document_ai.services.llm_endpoint_service import check_llm_endpoint
from document_ai.rag.generation import generate_rag_response_sync
from document_ai.search.views import EMPTY_SCOPE_SENTINEL, _expand_scope_node_ids
from document_ai.search.query_frontend import prepare_retrieval_query
from document_ai.search.execution import perform_vector_search_sync
from files.models import FileBlob, Node, NodeType

pytestmark = pytest.mark.unit

User = get_user_model()


class FakeRedisNoCancel:
    def exists(self, key):
        return False

    def delete(self, key):
        return 0


def _search_results():
    return [
        {
            "node_id": "11111111-1111-1111-1111-111111111111",
            "node_name": "policy.pdf",
            "file_ext": ".pdf",
            "doc_score": 0.91,
            "evidences": [
                {
                    "chunk_id": 10,
                    "text": "정부는 공급 확대와 할인 지원을 병행한다.",
                    "context_text": "넓은 문맥입니다. 정부는 공급 확대와 할인 지원을 병행한다.",
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
    ]


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
class RAGFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="rag@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.workspace = self.user.workspace_memberships.get(workspace__kind="personal").workspace
        self.client.force_login(self.user)
        self.query_frontend_env = patch.dict("os.environ", {"QUERY_UNDERSTANDING_FRONTEND_MODE": "passthrough"})
        self.query_frontend_env.start()
        self.addCleanup(self.query_frontend_env.stop)
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

    def test_search_request_returns_results_without_queue_or_polling(self):
        with patch(
            "document_ai.search.views.search_documents_sync",
            return_value=(_search_results(), {"request_search_ms": 12.5}),
        ) as search:
            response = self.client.post(
                "/api/document-ai/v1/search/",
                data={"query": "대책 요약", "top_k": 3},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["node_name"], "policy.pdf")
        self.assertEqual(SearchJob.objects.count(), 0)
        search.assert_called_once()

    def test_perform_vector_search_sync_preserves_embedding_busy_marker(self):
        from document_ai.embedding.providers.base import EmbeddingBusyError

        search_job = SearchJob.objects.create(owner=self.user, query="대책 요약", top_k=3)

        def fake_retrieve(**kwargs):
            raise EmbeddingBusyError("embedding-executor is busy", retry_after_seconds=4.0)

        fake_retriever = SimpleNamespace(retrieve=fake_retrieve)
        with patch("document_ai.search.retriever.VectorRetriever", return_value=fake_retriever):
            result = perform_vector_search_sync(search_job.id, max_retries=0)

        # Must stay distinguishable as EMBEDDING_BUSY, not collapse into a
        # generic string error -- callers (RAG) rely on this to return a
        # retryable 503 instead of a 500.
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "EMBEDDING_BUSY")
        self.assertEqual(result["retry_after_seconds"], 4.0)
        search_job.refresh_from_db()
        self.assertEqual(search_job.status, AIStatus.FAILED)

    def test_create_rag_jobs_sync_raises_busy_error_when_search_reports_embedding_busy(self):
        from document_ai.rag.streaming import RAGSearchBusyError, _create_rag_jobs_sync

        with patch(
            "document_ai.search.execution.perform_vector_search_sync",
            return_value={
                "status": "failed",
                "error": "embedding-executor is busy",
                "error_code": "EMBEDDING_BUSY",
                "retry_after_seconds": 3.0,
            },
        ):
            with self.assertRaises(RAGSearchBusyError) as ctx:
                _create_rag_jobs_sync(
                    owner=self.user,
                    question="대책 요약",
                    retrieval_query="대책 요약",
                    top_k=3,
                    threshold=None,
                    language="ko",
                    requested_node_ids=[],
                    scoped_node_ids=[],
                    llm_snapshot={},
                )

        self.assertEqual(ctx.exception.retry_after_seconds, 3.0)

    def test_rag_stream_endpoint_returns_ndjson_without_polling_contract(self):
        stream_response = StreamingHttpResponse(
            [
                b'{"type":"started","job_id":7}\n',
                b'{"type":"token","text":"answer"}\n',
                b'{"type":"completed","job_id":7}\n',
            ],
            content_type="application/x-ndjson",
        )
        with patch(
            "document_ai.rag.streaming.create_rag_streaming_response_async",
            new=AsyncMock(return_value=stream_response),
        ) as create_stream, patch(
            "document_ai.search.views.server_rag_runtime_availability",
            return_value=(True, {}),
        ), patch(
            "document_ai.search.views.acquire_rag_admission_token_async",
            new=AsyncMock(
                return_value=SimpleNamespace(release_async=AsyncMock())
            ),
        ):
            response = self.client.post(
                "/api/document-ai/v1/rag/stream/",
                data={"question": "문서 요약", "top_k": 3, "language": "ko"},
                content_type="application/json",
            )

        payload = b"".join(response.streaming_content).decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/x-ndjson"))
        self.assertIn('"type":"token"', payload)
        self.assertNotIn("poll_url", payload)
        create_stream.assert_called_once()

    def _retired_rag_request_creates_search_and_rag_jobs_then_queues_search(self):
        with patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async:
            apply_async.return_value = SimpleNamespace(id="search-task-id")

            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={
                    "question": "농축산물 수급 안정 대책을 요약해줘",
                    "top_k": 5,
                    "language": "ko",
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        search_job = SearchJob.objects.get(pk=payload["search_job_id"])
        rag_job = RAGJob.objects.get(pk=payload["job_id"])

        self.assertEqual(search_job.owner, self.user)
        self.assertEqual(search_job.query, "농축산물 수급 안정 대책을 요약해줘")
        self.assertIsNone(search_job.query_log)
        self.assertEqual(search_job.top_k, 5)
        self.assertEqual(search_job.threshold, settings.RAG_RETRIEVAL_THRESHOLD)
        self.assertEqual(search_job.task_id, "search-task-id")
        self.assertEqual(rag_job.search_job, search_job)
        self.assertIsNone(rag_job.query_log)
        self.assertEqual(rag_job.retrieval_query, "농축산물 수급 안정 대책을 요약해줘")
        self.assertEqual(rag_job.query_intent, QueryIntent.DOCUMENT_QUESTION)
        self.assertEqual(rag_job.answer_mode, QueryAnswerMode.RAG)
        self.assertTrue(rag_job.retrieval_required)
        self.assertEqual(rag_job.status, AIStatus.PENDING)
        self.assertEqual(rag_job.stage, RAGStage.SEARCHING)
        apply_async.assert_called_once_with(args=[search_job.id], queue="search")

    def _retired_rag_request_returns_503_when_server_runtime_failed_with_oom(self):
        runtime_status = {
            "status": "unavailable",
            "reason_code": "LLM_UNAVAILABLE_OOM",
            "retryable": True,
        }
        with patch(
            "document_ai.search.views.server_rag_runtime_availability",
            return_value=(False, runtime_status),
        ), patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async:
            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={"question": "문서 내용을 요약해줘", "language": "ko"},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 503)
        payload = response.json()["error"]
        self.assertEqual(payload["code"], "LLM_RUNTIME_UNAVAILABLE")
        self.assertEqual(payload["reason"], "LLM_UNAVAILABLE_OOM")
        self.assertTrue(payload["search_available"])
        self.assertEqual(payload["recovery_command"], "python3 install.py --retry-llm")
        self.assertEqual(SearchJob.objects.count(), 0)
        self.assertEqual(RAGJob.objects.count(), 0)
        apply_async.assert_not_called()

    def _retired_rag_request_does_not_call_query_parser_even_when_frontend_mode_is_llm(self):
        with patch.dict("os.environ", {"QUERY_UNDERSTANDING_FRONTEND_MODE": "llm"}), patch(
            "document_ai.query_understanding.parser_service.parse_user_query_sync"
        ) as parse_user_query, patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async:
            apply_async.return_value = SimpleNamespace(id="search-task-id")
            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={
                    "question": "지난주 업로드한 pdf 계약서에서 해지 조항 알려줘",
                    "top_k": 5,
                    "language": "ko",
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        search_job = SearchJob.objects.get(pk=payload["search_job_id"])
        rag_job = RAGJob.objects.get(pk=payload["job_id"])

        parse_user_query.assert_not_called()
        self.assertEqual(search_job.query, "지난주 업로드한 pdf 계약서에서 해지 조항 알려줘")
        self.assertIsNone(search_job.query_log)
        self.assertEqual(rag_job.retrieval_query, "지난주 업로드한 pdf 계약서에서 해지 조항 알려줘")
        self.assertEqual(rag_job.query_intent, QueryIntent.DOCUMENT_QUESTION)
        self.assertEqual(rag_job.answer_mode, QueryAnswerMode.RAG)
        self.assertTrue(rag_job.retrieval_required)
        self.assertEqual(rag_job.query_confidence, 0.0)

    def _retired_rag_request_snapshots_selected_user_llm_endpoint(self):
        endpoint = LLMEndpoint.objects.create(
            owner=self.user,
            name="Local Ollama",
            endpoint_type=LLMEndpoint.ENDPOINT_OLLAMA,
            base_url="http://host.docker.internal:11434/v1",
            default_model="llama3.1:8b",
        )
        UserLLMPreference.objects.create(
            user=self.user,
            rag_endpoint=endpoint,
            rag_model="qwen2.5:7b",
        )

        with patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async:
            apply_async.return_value = SimpleNamespace(id="search-task-id")
            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={
                    "question": "선택 모델로 답변해줘",
                    "top_k": 3,
                    "language": "ko",
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        rag_job = RAGJob.objects.get(pk=response.json()["job_id"])
        self.assertEqual(rag_job.llm_endpoint, endpoint)
        self.assertEqual(rag_job.llm_endpoint_name, "Local Ollama")
        self.assertEqual(rag_job.llm_base_url, "http://host.docker.internal:11434")
        self.assertEqual(rag_job.llm_model, "qwen2.5:7b")

    @patch.dict("os.environ", {"RAG_LLM_URL": "http://server-rag:8080/v1"})
    def _retired_rag_request_snapshots_selected_server_model_without_external_endpoint(self):
        UserLLMPreference.objects.create(
            user=self.user,
            rag_model="server-qwen",
        )

        with patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async:
            apply_async.return_value = SimpleNamespace(id="search-task-id")
            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={
                    "question": "서버 모델로 답변해줘",
                    "top_k": 3,
                    "language": "ko",
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        rag_job = RAGJob.objects.get(pk=response.json()["job_id"])
        self.assertIsNone(rag_job.llm_endpoint)
        self.assertEqual(rag_job.llm_endpoint_name, "Server default")
        self.assertEqual(rag_job.llm_base_url, "http://server-rag:8080")
        self.assertEqual(rag_job.llm_model, "server-qwen")

    def _retired_rag_request_snapshots_server_auto_target_without_user_preference(self):
        server_target = SimpleNamespace(
            as_snapshot=lambda: {
                "llm_endpoint_name": "Server auto",
                "llm_base_url": "http://auto-rag:8080",
                "llm_model": "auto-model",
            }
        )

        with patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async, patch(
            "document_ai.services.llm_endpoint_service.get_cached_server_rag_target",
            return_value=server_target,
        ):
            apply_async.return_value = SimpleNamespace(id="search-task-id")
            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={
                    "question": "자동 서버 모델로 답변해줘",
                    "top_k": 3,
                    "language": "ko",
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        rag_job = RAGJob.objects.get(pk=response.json()["job_id"])
        self.assertIsNone(rag_job.llm_endpoint)
        self.assertEqual(rag_job.llm_endpoint_name, "Server auto")
        self.assertEqual(rag_job.llm_base_url, "http://auto-rag:8080")
        self.assertEqual(rag_job.llm_model, "auto-model")

    def _retired_rag_request_accepts_explicit_retrieval_threshold(self):
        with patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async:
            apply_async.return_value = SimpleNamespace(id="search-task-id")

            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={
                    "question": "근거를 좁혀서 답변해줘",
                    "top_k": 3,
                    "threshold": 0.45,
                    "language": "ko",
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        search_job = SearchJob.objects.get(pk=response.json()["search_job_id"])
        self.assertEqual(search_job.top_k, 3)
        self.assertEqual(search_job.threshold, 0.45)

    def _retired_rag_request_expands_selected_folder_to_descendant_files(self):
        folder = Node.objects.create(
            owner=self.user,
            name="reports",
            ext="",
            node_type=NodeType.FOLDER,
        )
        file_node = Node.objects.create(
            owner=self.user,
            name="report.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            parent=folder,
        )
        with patch("document_ai.signals.enqueue_parse"):
            FileBlob.objects.create(
                node=file_node,
                original_name="report.txt",
                file=SimpleUploadedFile("report.txt", b"hello", content_type="text/plain"),
                mime_type="text/plain",
                size=5,
                status="ready",
            )

        with patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async:
            apply_async.return_value = SimpleNamespace(id="search-task-id")
            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={
                    "question": "보고서 요약",
                    "top_k": 5,
                    "language": "ko",
                    "node_ids": [str(folder.uid)],
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        search_job = SearchJob.objects.get(pk=response.json()["search_job_id"])
        self.assertEqual(search_job.node_ids, [str(file_node.uid)])

    def _retired_rag_poll_only_returns_status_after_search_completes(self):
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
            status=AIStatus.COMPLETED,
            results=_search_results(),
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
        )

        response = self.client.get(f"/api/document-ai/v1/rag/jobs/{rag_job.id}/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rag_job.refresh_from_db()
        self.assertEqual(rag_job.task_id, "")
        self.assertEqual(payload["search_status"], AIStatus.COMPLETED)
        self.assertEqual(payload["search_results"][0]["node_name"], "policy.pdf")
        self.assertIn("stage", payload)
        self.assertIn("stage_message", payload)

    def _retired_search_completion_queues_linked_rag_generation(self):
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
            stage=RAGStage.SEARCHING,
        )

        fake_retriever = SimpleNamespace(retrieve=lambda **kwargs: _search_results())
        with patch("document_ai.search.retriever.VectorRetriever", return_value=fake_retriever), patch(
            "document_ai.tasks.generate_rag_response.apply_async"
        ) as apply_async:
            apply_async.return_value = SimpleNamespace(id="rag-task-id")
            result = perform_vector_search_sync(search_job.id)

        search_job.refresh_from_db()
        rag_job.refresh_from_db()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["queued_rag_jobs"], 1)
        self.assertEqual(search_job.status, AIStatus.COMPLETED)
        self.assertGreaterEqual(search_job.performance_metrics["queue_wait_ms"], 0)
        self.assertGreaterEqual(search_job.performance_metrics["worker_total_ms"], 0)
        self.assertGreaterEqual(search_job.performance_metrics["end_to_end_ms"], 0)
        self.assertEqual(search_job.performance_metrics["result_count"], 1)
        self.assertEqual(rag_job.task_id, "rag-task-id")
        self.assertEqual(rag_job.stage, RAGStage.GENERATING)
        self.assertIn("답변", rag_job.stage_message)
        apply_async.assert_called_once_with(args=[rag_job.id], queue="rag")

    def _retired_search_completion_fails_server_rag_job_when_runtime_became_unavailable(self):
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
            stage=RAGStage.SEARCHING,
        )
        fake_retriever = SimpleNamespace(retrieve=lambda **kwargs: _search_results())
        with patch(
            "document_ai.search.retriever.VectorRetriever",
            return_value=fake_retriever,
        ), patch(
            "document_ai.services.rag_runtime_config.server_rag_runtime_availability",
            return_value=(
                False,
                {"status": "unavailable", "reason_code": "LLM_UNAVAILABLE_OOM"},
            ),
        ), patch("document_ai.tasks.generate_rag_response.apply_async") as apply_async:
            result = perform_vector_search_sync(search_job.id)

        rag_job.refresh_from_db()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["queued_rag_jobs"], 0)
        self.assertEqual(rag_job.status, AIStatus.FAILED)
        self.assertEqual(rag_job.stage, RAGStage.FAILED)
        self.assertEqual(rag_job.error_message, "LLM_UNAVAILABLE_OOM")
        apply_async.assert_not_called()

    def test_generate_rag_response_uses_search_evidence_and_stores_citations(self):
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
            status=AIStatus.COMPLETED,
            results=_search_results(),
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
            language="ko",
        )

        class FakeResponse:
            status_code = 200
            text = ""

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=False):
                chunks = [
                    'event: response.output_text.delta',
                    'data: {"type":"response.output_text.delta","delta":"<|channel>final\\n핵심 답변\\n"}',
                    'event: response.output_text.delta',
                    'data: {"type":"response.output_text.delta","delta":"- 공급 확대와 할인 지원을 병행합니다 [1].\\n\\n"}',
                    'event: response.output_text.delta',
                    'data: {"type":"response.output_text.delta","delta":"주요 근거\\n- policy.pdf / section 정책 / page 1 [1]"}',
                    'event: response.completed',
                    'data: {"type":"response.completed","response":{"output":[]}}',
                ]
                return chunks

        streamed_tokens = []
        with patch("requests.post", return_value=FakeResponse()) as post, patch(
            "redis.Redis.from_url", return_value=FakeRedisNoCancel()
        ):
            result = generate_rag_response_sync(rag_job.id, on_token=streamed_tokens.append)

        rag_job.refresh_from_db()
        self.assertEqual(result["status"], "success")
        self.assertEqual(rag_job.status, AIStatus.COMPLETED)
        self.assertIn("공급 확대", rag_job.answer)
        self.assertEqual(len(rag_job.citations), 1)
        self.assertGreaterEqual(rag_job.performance_metrics["queue_wait_ms"], 0)
        self.assertGreaterEqual(rag_job.performance_metrics["context_build_ms"], 0)
        self.assertGreaterEqual(rag_job.performance_metrics["llm_ttft_ms"], 0)
        self.assertGreaterEqual(rag_job.performance_metrics["llm_total_ms"], 0)
        self.assertGreaterEqual(rag_job.performance_metrics["worker_total_ms"], 0)
        self.assertEqual(rag_job.performance_metrics["citation_count"], 1)
        self.assertGreater(rag_job.performance_metrics["output_chars"], 0)
        self.assertEqual(rag_job.citations[0]["node_name"], "policy.pdf")
        self.assertEqual(rag_job.citations[0]["text"], "압축 근거: 공급 확대와 할인 지원을 병행한다.")
        post.assert_called_once()
        request_payload = post.call_args.kwargs["json"]
        final_user_prompt = request_payload["messages"][-1]["content"]
        system_prompt = request_payload["messages"][0]["content"]
        self.assertIn("You are a helpful AI assistant", system_prompt)
        self.assertTrue(request_payload["stream"])
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertIn("압축 근거", final_user_prompt)
        self.assertNotIn("넓은 문맥입니다", final_user_prompt)
        self.assertIn("공급 확대", "".join(streamed_tokens))
        self.assertNotIn("<|channel>", "".join(streamed_tokens))

    def test_generate_rag_response_completes_with_insufficient_evidence_when_search_has_no_citations(self):
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
            threshold=0.25,
            status=AIStatus.COMPLETED,
            results=[],
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
            language="ko",
        )

        with patch("requests.post") as post:
            result = generate_rag_response_sync(rag_job.id)

        rag_job.refresh_from_db()
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(rag_job.status, AIStatus.COMPLETED)
        self.assertEqual(rag_job.stage, RAGStage.COMPLETED)
        self.assertIn("충분한 근거", rag_job.answer)
        self.assertEqual(rag_job.citations, [])
        self.assertEqual(rag_job.error_message, "")
        post.assert_not_called()

    def test_generate_rag_response_completes_with_insufficient_evidence_when_scores_are_below_threshold(self):
        weak_results = _search_results()
        weak_results[0]["doc_score"] = 0.1
        weak_results[0]["evidences"][0]["hybrid_score"] = 0.12
        weak_results[0]["evidences"][0]["dense_score"] = 0.11
        weak_results[0]["evidences"][0]["sparse_score"] = 0.13
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
            threshold=0.25,
            status=AIStatus.COMPLETED,
            results=weak_results,
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
            language="ko",
        )

        with patch("requests.post") as post:
            result = generate_rag_response_sync(rag_job.id)

        rag_job.refresh_from_db()
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(rag_job.status, AIStatus.COMPLETED)
        self.assertIn("충분한 근거", rag_job.answer)
        self.assertEqual(rag_job.citations, [])
        post.assert_not_called()

    def test_generate_rag_response_uses_snapshotted_llm_endpoint_and_model(self):
        endpoint = LLMEndpoint.objects.create(
            owner=self.user,
            name="Local llama.cpp",
            endpoint_type=LLMEndpoint.ENDPOINT_LLAMA_CPP,
            base_url="http://llm-runtime:8080",
            default_model="gemma-local",
            api_key="local-secret",
        )
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
            status=AIStatus.COMPLETED,
            results=_search_results(),
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
            language="ko",
            llm_endpoint=endpoint,
            llm_endpoint_name=endpoint.name,
            llm_base_url=endpoint.normalized_base_url,
            llm_model="gemma-selected",
        )

        class FakeResponse:
            status_code = 200
            text = ""

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=False):
                return [
                    'data: {"type":"response.output_text.delta","delta":"공급 확대와 "}',
                    'data: {"type":"response.output_text.delta","delta":"할인 지원입니다 [1]."}',
                    'data: {"type":"response.completed","response":{"output":[]}}',
                ]

        with patch("requests.post", return_value=FakeResponse()) as post, patch(
            "redis.Redis.from_url", return_value=FakeRedisNoCancel()
        ):
            result = generate_rag_response_sync(rag_job.id)

        self.assertEqual(result["status"], "success")
        self.assertEqual(post.call_args.args[0], "http://llm-runtime:8080/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "gemma-selected")
        self.assertGreater(post.call_args.kwargs["json"]["max_tokens"], 0)
        self.assertIn("messages", post.call_args.kwargs["json"])
        self.assertNotIn("input", post.call_args.kwargs["json"])
        self.assertNotIn("max_output_tokens", post.call_args.kwargs["json"])
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer local-secret")

    def test_generate_rag_response_uses_openai_responses_token_limit_field(self):
        endpoint = LLMEndpoint.objects.create(
            owner=self.user,
            name="OpenAI",
            endpoint_type=LLMEndpoint.ENDPOINT_OPENAI_COMPATIBLE,
            base_url="https://api.openai.com",
            default_model="gpt-4.1-mini",
            api_key="openai-secret",
        )
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
            status=AIStatus.COMPLETED,
            results=_search_results(),
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
            language="ko",
            llm_endpoint=endpoint,
            llm_endpoint_name=endpoint.name,
            llm_base_url=endpoint.normalized_base_url,
            llm_model="gpt-4.1-mini",
        )

        class FakeResponse:
            status_code = 200
            text = ""

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=False):
                return [
                    'data: {"type":"response.output_text.delta","delta":"공급 확대와 할인 지원입니다 [1]."}',
                    'data: {"type":"response.completed","response":{"output":[]}}',
                ]

        with patch("requests.post", return_value=FakeResponse()) as post, patch(
            "redis.Redis.from_url", return_value=FakeRedisNoCancel()
        ):
            result = generate_rag_response_sync(rag_job.id)

        self.assertEqual(result["status"], "success")
        request_payload = post.call_args.kwargs["json"]
        self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/responses")
        self.assertGreater(request_payload["max_output_tokens"], 0)
        self.assertNotIn("max_tokens", request_payload)
        self.assertIn("input", request_payload)
        self.assertNotIn("messages", request_payload)

    def test_generate_rag_response_without_retrieval_stores_uncited_answer(self):
        rag_job = RAGJob.objects.create(
            owner=self.user,
            question="안녕 너 뭐 할 수 있어?",
            retrieval_required=False,
            query_intent=QueryIntent.CASUAL_CHAT,
            answer_mode=QueryAnswerMode.CASUAL,
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
                    'data: {"type":"response.output_text.delta","delta":"안녕하세요. 문서 검색과 요약을 도와드릴 수 있습니다."}',
                    'data: {"type":"response.completed","response":{"output":[]}}',
                ]

        with patch("requests.post", return_value=FakeResponse()) as post, patch(
            "redis.Redis.from_url", return_value=FakeRedisNoCancel()
        ):
            result = generate_rag_response_sync(rag_job.id)

        rag_job.refresh_from_db()
        self.assertEqual(result["status"], "success")
        self.assertEqual(rag_job.status, AIStatus.COMPLETED)
        self.assertEqual(rag_job.stage, RAGStage.COMPLETED)
        self.assertIn("문서 검색", rag_job.answer)
        self.assertEqual(rag_job.citations, [])
        request_payload = post.call_args.kwargs["json"]
        final_user_prompt = request_payload["messages"][-1]["content"]
        system_prompt = request_payload["messages"][0]["content"]
        self.assertIn("without document citations", final_user_prompt)
        self.assertIn("document retrieval is not required", system_prompt)

    def _retired_cancel_rag_job_marks_job_and_search_canceled(self):
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
            status=AIStatus.PROCESSING,
            task_id="search-task-id",
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
            status=AIStatus.PROCESSING,
            stage=RAGStage.GENERATING,
            task_id="rag-task-id",
        )

        with patch("document_ai.search.views.set_rag_cancel_signal", return_value=True) as cancel_signal, patch(
            "document_ai.search.views.celery_app.control.revoke"
        ) as revoke:
            response = self.client.post(
                f"/api/document-ai/v1/rag/jobs/{rag_job.id}/cancel/",
                data={"reason": "stop"},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        rag_job.refresh_from_db()
        search_job.refresh_from_db()
        self.assertEqual(rag_job.status, AIStatus.CANCELED)
        self.assertEqual(rag_job.stage, RAGStage.CANCELED)
        self.assertEqual(rag_job.cancel_reason, "stop")
        self.assertIsNotNone(rag_job.canceled_at)
        self.assertEqual(search_job.status, AIStatus.CANCELED)
        cancel_signal.assert_called_once_with(rag_job.id)
        revoke.assert_any_call("rag-task-id", terminate=False)
        revoke.assert_any_call("search-task-id", terminate=False)
        self.assertFalse(response.json()["can_cancel"])

    def test_generate_rag_response_closes_stream_when_cancel_requested(self):
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
            status=AIStatus.COMPLETED,
            results=_search_results(),
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
            language="ko",
        )

        class FakeResponse:
            status_code = 200
            text = ""
            closed = False

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=False):
                return [
                    'data: {"type":"response.output_text.delta","delta":"생성 중입니다."}',
                    'data: {"type":"response.output_text.delta","delta":"더 생성됩니다."}',
                ]

            def close(self):
                self.closed = True

        class FakeRedis:
            def __init__(self):
                self.exists_calls = 0

            def exists(self, key):
                self.exists_calls += 1
                return self.exists_calls >= 2

            def delete(self, key):
                return 1

        fake_response = FakeResponse()
        fake_redis = FakeRedis()

        with patch("requests.post", return_value=fake_response), patch(
            "redis.Redis.from_url", return_value=fake_redis
        ), patch(
            "document_ai.rag.generation.time_module.monotonic", side_effect=[1.0, 2.0]
        ):
            result = generate_rag_response_sync(rag_job.id)

        rag_job.refresh_from_db()
        self.assertEqual(result["status"], "canceled")
        self.assertTrue(fake_response.closed)
        self.assertEqual(rag_job.status, AIStatus.CANCELED)
        self.assertEqual(rag_job.stage, RAGStage.CANCELED)
        self.assertEqual(rag_job.answer, "")

    def test_llm_endpoint_healthcheck_marks_endpoint_available(self):
        endpoint = LLMEndpoint.objects.create(
            owner=self.user,
            name="OpenAI compatible",
            endpoint_type=LLMEndpoint.ENDPOINT_OPENAI_COMPATIBLE,
            base_url="https://api.example.test/v1",
            default_model="qwen-test",
            api_key="secret-key",
        )

        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {"data": [{"id": "qwen-test"}]}

        with patch("requests.get", return_value=FakeResponse()) as get:
            checked = check_llm_endpoint(workspace=self.workspace, endpoint_id=str(endpoint.id))

        checked.refresh_from_db()
        self.assertEqual(checked.last_check_status, "ok")
        self.assertIn("기본 모델", checked.last_check_message)
        self.assertEqual(get.call_args.args[0], "https://api.example.test/v1/models")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer secret-key")

    def test_settings_create_llm_endpoint_runs_healthcheck(self):
        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {"data": [{"id": "qwen-test"}]}

        with patch("requests.get", return_value=FakeResponse()) as get:
            response = self.client.post(
                "/accounts/settings/",
                data={
                    "action": "create_llm_endpoint",
                    "endpoint_name": "External API",
                    "endpoint_type": LLMEndpoint.ENDPOINT_OPENAI_COMPATIBLE,
                    "base_url": "https://api.example.test/v1",
                    "default_model": "qwen-test",
                    "api_key": "secret-key",
                },
            )

        self.assertEqual(response.status_code, 302)
        endpoint = LLMEndpoint.objects.get(owner=self.user, name="External API")
        self.assertEqual(endpoint.last_check_status, "ok")
        self.assertEqual(get.call_args.args[0], "https://api.example.test/v1/models")

    def test_llm_endpoint_healthcheck_marks_endpoint_failed(self):
        endpoint = LLMEndpoint.objects.create(
            owner=self.user,
            name="Broken endpoint",
            endpoint_type=LLMEndpoint.ENDPOINT_OPENAI_COMPATIBLE,
            base_url="https://broken.example.test",
            default_model="qwen-test",
        )

        with patch("requests.get", side_effect=requests.RequestException("connection failed")):
            checked = check_llm_endpoint(workspace=self.workspace, endpoint_id=str(endpoint.id))

        checked.refresh_from_db()
        self.assertEqual(checked.last_check_status, "failed")
        self.assertIn("connection failed", checked.last_check_message)

    def test_query_frontend_passthrough_mode_skips_log(self):
        before_count = QueryUnderstandingLog.objects.count()

        plan = prepare_retrieval_query("지난주 pdf 계약 문서", mode="rag", owner=self.user)

        self.assertEqual(plan.source, "llm_query_frontend_disabled")
        self.assertEqual(plan.retrieval_query, "지난주 pdf 계약 문서")
        self.assertEqual(plan.intent, QueryIntent.DOCUMENT_QUESTION)
        self.assertEqual(plan.answer_mode, QueryAnswerMode.RAG)
        self.assertTrue(plan.retrieval_required)
        self.assertIsNone(plan.query_log)
        self.assertEqual(plan.warnings[0]["code"], "llm_query_frontend_disabled")
        self.assertEqual(QueryUnderstandingLog.objects.count(), before_count)

    def test_query_frontend_llm_mode_returns_intent_and_semantic_query(self):
        parsed_query = {
            "status": "success",
            "mode": "rag",
            "source": "llm_query_pipeline",
            "raw_query": "안녕 너 뭐 할 수 있어?",
            "normalized_query": "안녕 너 뭐 할 수 있어?",
            "semantic_query": "",
            "intent": QueryIntent.CASUAL_CHAT.value,
            "answer_mode": QueryAnswerMode.CASUAL.value,
            "retrieval_required": False,
            "confidence": 0.98,
            "classification": {
                "intent": QueryIntent.CASUAL_CHAT.value,
                "answer_mode": QueryAnswerMode.CASUAL.value,
                "retrieval_required": False,
                "confidence": 0.98,
                "reason": "일상 대화입니다.",
            },
            "analysis": {"warnings": []},
            "metadata": {"filters": [], "sorts": [], "target_scopes": []},
            "dsl": {"semantic_query": "", "filters": [], "sorts": [], "target_scopes": []},
            "orm": {"filter_kwargs": {}, "exclude_kwargs": {}, "order_by": []},
        }

        with patch.dict("os.environ", {"QUERY_UNDERSTANDING_FRONTEND_MODE": "llm"}), patch(
            "document_ai.query_understanding.parser_service.parse_user_query_sync", return_value=parsed_query
        ):
            plan = prepare_retrieval_query("안녕 너 뭐 할 수 있어?", mode="rag", owner=self.user)

        self.assertEqual(plan.source, "llm_query_pipeline")
        self.assertEqual(plan.retrieval_query, "")
        self.assertEqual(plan.intent, QueryIntent.CASUAL_CHAT)
        self.assertEqual(plan.answer_mode, QueryAnswerMode.CASUAL)
        self.assertFalse(plan.retrieval_required)
        self.assertEqual(plan.confidence, 0.98)
        self.assertEqual(plan.query_log.intent, QueryIntent.CASUAL_CHAT)
        self.assertEqual(plan.query_log.semantic_query, "")

    def test_scope_expansion_includes_files_under_selected_folder(self):
        folder = Node.objects.create(
            owner=self.user,
            name="reports",
            ext="",
            node_type=NodeType.FOLDER,
        )
        file_node = Node.objects.create(
            owner=self.user,
            name="report.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            parent=folder,
        )
        with patch("document_ai.signals.enqueue_parse"):
            FileBlob.objects.create(
                node=file_node,
                original_name="report.txt",
                file=SimpleUploadedFile("report.txt", b"hello", content_type="text/plain"),
                mime_type="text/plain",
                size=5,
                status="ready",
            )

        scoped_ids = _expand_scope_node_ids(self.user, [folder.uid])

        self.assertEqual(scoped_ids, [str(file_node.uid)])

    def test_scope_expansion_uses_sentinel_when_selection_has_no_files(self):
        empty_folder = Node.objects.create(
            owner=self.user,
            name="empty",
            ext="",
            node_type=NodeType.FOLDER,
        )

        scoped_ids = _expand_scope_node_ids(self.user, [empty_folder.uid])

        self.assertEqual(scoped_ids, [EMPTY_SCOPE_SENTINEL])
