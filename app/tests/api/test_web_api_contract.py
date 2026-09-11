import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import StreamingHttpResponse
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import APIToken
from config.enums import AIStatus, NodeType
from document_ai.models import DocumentChunk, DocumentParseResult, RAGJob
from files.models import FileBlob, Node


pytestmark = pytest.mark.unit

User = get_user_model()


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"], LOGIN_REQUIRED=True)
class WebApiContractTests(TestCase):
    """Consumer-facing baseline for the Django-template to SPA migration."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="web-contract@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.other_user = User.objects.create_user(
            email="web-contract-other@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.client.force_login(self.user)

    @override_settings(CSRF_TRUSTED_ORIGINS=["http://127.0.0.1:4173"])
    def test_vite_dev_origin_can_mutate_with_csrf_but_unknown_origin_cannot(self):
        node = Node.objects.create(
            owner=self.user,
            name="csrf.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        bootstrap = csrf_client.get(reverse("accounts_api:session"))
        token = bootstrap.cookies["csrftoken"].value

        trusted = csrf_client.post(
            reverse("files:api_toggle_star", kwargs={"uid": node.uid}),
            data=json.dumps({}),
            content_type="application/json",
            HTTP_ORIGIN="http://127.0.0.1:4173",
            HTTP_X_CSRFTOKEN=token,
        )
        untrusted = csrf_client.post(
            reverse("files:api_toggle_star", kwargs={"uid": node.uid}),
            data=json.dumps({}),
            content_type="application/json",
            HTTP_ORIGIN="http://malicious.example",
            HTTP_X_CSRFTOKEN=token,
        )

        self.assertEqual(trusted.status_code, 200)
        self.assertEqual(trusted.json()["starred"], True)
        self.assertEqual(untrusted.status_code, 403)
        self.assertEqual(untrusted.json()["error"]["code"], "CSRF_FAILED")

    def test_registered_route_catalog_matches_web_and_dedicated_clients(self):
        static_routes = {
            "files:api_list": "/files/api/v1/files/",
            "files:api_upload": "/files/api/v1/upload/",
            "files:api_create_folder": "/files/api/v1/create_folder/",
            "files:api_all_folders": "/files/api/v1/folders/",
            "files:api_rag_scope_nodes": "/files/api/v1/rag/scope-nodes/",
            "files:api_storage_usage": "/files/api/v1/storage/",
            "files:api_ai_readiness": "/files/api/v1/ai/readiness/",
            "files:api_search_history": "/files/api/v1/ai/search-history/",
            "files:api_bulk_delete": "/files/api/v1/bulk/delete/",
            "files:api_bulk_restore": "/files/api/v1/bulk/restore/",
            "files:api_bulk_move": "/files/api/v1/bulk/move/",
            "files:api_recent": "/files/api/v1/recent/",
            "files:api_starred": "/files/api/v1/starred/",
            "files:api_trash": "/files/api/v1/trash/",
            "files:api_empty_trash": "/files/api/v1/trash/empty/",
            "files:healthcheck": "/files/healthcheck/",
            "accounts_api:session": "/api/accounts/v1/session/",
            "accounts_api:login": "/api/accounts/v1/login/",
            "accounts_api:logout": "/api/accounts/v1/logout/",
            "accounts_api:cli-tokens": "/api/accounts/v1/cli-tokens/",
            "document_ai:vector-search": "/api/document-ai/v1/search/",
            "document_ai:rag-stream": "/api/document-ai/v1/rag/stream/",
            "document_ai:server-policy": "/api/document-ai/v1/server-policy/",
            "document_ai:vector-tuning": "/api/document-ai/v1/tuning/",
            "cli_api:identity": "/api/cli/v1/identity/",
            "sync_api:identity": "/api/sync/v1/identity/",
            "sync_api:ping": "/api/sync/v1/ping/",
            "sync_api:diff": "/api/sync/v1/diff/",
            "sync_api:upload": "/api/sync/v1/upload/",
            "sync_api:mkdir": "/api/sync/v1/mkdir/",
            "sync_api:delete": "/api/sync/v1/delete/",
            "sync_api:confirm": "/api/sync/v1/confirm/",
        }
        for route_name, expected_path in static_routes.items():
            with self.subTest(route_name=route_name):
                self.assertEqual(reverse(route_name), expected_path)

        sample_uid = "11111111-1111-1111-1111-111111111111"
        uid_routes = {
            "files:api_detail": f"/files/api/v1/{sample_uid}/",
            "files:api_parsed_text": f"/files/api/v1/{sample_uid}/parsed_text/",
            "files:api_update_meta": f"/files/api/v1/{sample_uid}/meta/",
            "files:api_rename": f"/files/api/v1/{sample_uid}/rename/",
            "files:api_move": f"/files/api/v1/{sample_uid}/move/",
            "files:api_set_ai_processing": f"/files/api/v1/{sample_uid}/ai/enabled/",
            "files:api_retry_ai": f"/files/api/v1/{sample_uid}/ai/retry/",
            "files:api_download": f"/files/api/v1/{sample_uid}/download/",
            "files:api_delete": f"/files/api/v1/{sample_uid}/delete/",
            "files:api_restore": f"/files/api/v1/{sample_uid}/restore/",
            "files:api_permanent_delete": f"/files/api/v1/{sample_uid}/permanent_delete/",
            "files:api_toggle_star": f"/files/api/v1/toggle_star/{sample_uid}/",
        }
        for route_name, expected_path in uid_routes.items():
            with self.subTest(route_name=route_name):
                self.assertEqual(reverse(route_name, kwargs={"uid": sample_uid}), expected_path)

    def test_file_list_returns_stable_fields_and_hides_other_owners(self):
        folder = Node.objects.create(
            owner=self.user,
            name="reports",
            ext="",
            node_type=NodeType.FOLDER,
        )
        own_file = Node.objects.create(
            owner=self.user,
            name="own.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            parent=folder,
        )
        Node.objects.create(
            owner=self.other_user,
            name="private.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )

        response = self.client.get(
            reverse("files:api_list"),
            {"parent_id": str(folder.uid), "page": 1, "limit": 10},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["page"], 1)
        self.assertFalse(payload["has_next"])
        self.assertEqual([item["uid"] for item in payload["files"]], [str(own_file.uid)])
        self.assertTrue(
            {
                "uid",
                "name",
                "ext",
                "node_type",
                "path",
                "starred",
                "trashed",
                "ai_processing_enabled",
                "created_at",
                "updated_at",
            }.issubset(payload["files"][0])
        )

    @patch("document_ai.signals.enqueue_parse")
    def test_multipart_upload_returns_owned_file_contract(self, parse_delay):
        upload = SimpleUploadedFile(
            "contract.txt",
            b"contract body",
            content_type="text/plain",
        )

        response = self.client.post(
            reverse("files:api_upload"),
            data={"file": upload, "description": "SPA upload", "ai_processing_enabled": "1"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn(payload["status"], {"done", "duplicate"})
        self.assertEqual(payload["file"]["name"], "contract.txt")
        node = Node.objects.get(uid=payload["file"]["uid"])
        self.assertEqual(node.owner, self.user)
        self.assertTrue(node.ai_processing_enabled)
        parse_delay.assert_called_once()

    def test_detail_and_parsed_text_are_scoped_to_the_owner(self):
        own_file = Node.objects.create(
            owner=self.user,
            name="notes.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )
        parse_result = DocumentParseResult.objects.create(
            node=own_file,
            status=AIStatus.COMPLETED,
            chunk_count=2,
        )
        DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=1,
            text="second",
            status=AIStatus.COMPLETED,
        )
        DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="first",
            status=AIStatus.COMPLETED,
        )
        other_file = Node.objects.create(
            owner=self.other_user,
            name="private.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )

        detail = self.client.get(reverse("files:api_detail", kwargs={"uid": own_file.uid}))
        parsed = self.client.get(reverse("files:api_parsed_text", kwargs={"uid": own_file.uid}))
        forbidden_detail = self.client.get(
            reverse("files:api_detail", kwargs={"uid": other_file.uid})
        )

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["file"]["uid"], str(own_file.uid))
        self.assertEqual(parsed.status_code, 200)
        self.assertEqual(parsed.json(), {"ok": True, "text": "first\n\nsecond"})
        self.assertEqual(forbidden_detail.status_code, 404)

    def test_scope_nodes_returns_only_owned_nodes_with_stable_shape(self):
        folder = Node.objects.create(
            owner=self.user,
            name="scope",
            ext="",
            node_type=NodeType.FOLDER,
        )
        own_file = Node.objects.create(
            owner=self.user,
            name="scope.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            parent=folder,
        )
        with patch("document_ai.signals.enqueue_parse"):
            FileBlob.objects.create(
                node=own_file,
                original_name="scope.txt",
                file=SimpleUploadedFile("scope.txt", b"scope", content_type="text/plain"),
                mime_type="text/plain",
                size=5,
                status="ready",
            )
        Node.objects.create(
            owner=self.other_user,
            name="scope-private",
            ext="",
            node_type=NodeType.FOLDER,
        )

        response = self.client.get(reverse("files:api_rag_scope_nodes"), {"q": "scope"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            {item["uid"] for item in payload["nodes"]},
            {str(folder.uid), str(own_file.uid)},
        )
        for item in payload["nodes"]:
            self.assertEqual(
                set(item),
                {"uid", "name", "path", "node_type", "depth", "ext", "file_count"},
            )

    def test_search_returns_synchronous_result_contract_and_owner_scope(self):
        result = {
            "node_id": "22222222-2222-2222-2222-222222222222",
            "node_name": "policy.pdf",
            "file_ext": ".pdf",
            "doc_score": 0.91,
            "evidences": [
                {
                    "chunk_id": 10,
                    "text": "policy evidence",
                    "context_text": "wider policy evidence",
                    "section": "policy",
                    "pages": "1",
                    "distance": -0.91,
                }
            ],
        }
        with patch(
            "document_ai.search.views.search_documents_sync",
            return_value=([result], {"request_search_ms": 12.5}),
        ) as search:
            response = self.client.post(
                reverse("document_ai:vector-search"),
                data=json.dumps({"query": "policy", "top_k": 3}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "results": [result],
            "performance_metrics": {"request_search_ms": 12.5},
            "query_plan": {
                "mode": "advanced",
                "source": "direct",
                "retrieval_query": "policy",
                "intent": "",
                "confidence": None,
                "warnings": [],
                "filters": [],
                "sorts": [],
            },
        })
        self.assertEqual(search.call_args.kwargs["owner"], self.user)
        self.assertEqual(search.call_args.kwargs["query"], "policy")
        self.assertEqual(search.call_args.kwargs["top_k"], 3)

    def test_basic_search_bypasses_query_understanding(self):
        with patch(
            "document_ai.search.views.search_documents_sync",
            return_value=([], {"request_search_ms": 3.0}),
        ) as search:
            response = self.client.post(
                reverse("document_ai:vector-search"),
                data=json.dumps({"mode": "basic", "query": "  direct policy  "}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(search.call_args.kwargs["query"], "direct policy")
        self.assertEqual(search.call_args.kwargs["orm_constraints"], {
            "filter_kwargs": {},
            "exclude_kwargs": {},
            "order_by": [],
        })
        self.assertEqual(response.json()["query_plan"], {
            "mode": "basic",
            "source": "direct",
            "retrieval_query": "direct policy",
            "intent": "",
            "confidence": None,
            "warnings": [],
            "filters": [],
            "sorts": [],
        })

    def test_search_returns_embedding_busy_as_retryable_503(self):
        from document_ai.embedding.providers.base import EmbeddingBusyError

        with patch(
            "document_ai.search.views.search_documents_sync",
            side_effect=EmbeddingBusyError("embedding-executor is busy", retry_after_seconds=7.0),
        ):
            response = self.client.post(
                reverse("document_ai:vector-search"),
                data=json.dumps({"mode": "basic", "query": "policy"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "EMBEDDING_BUSY")
        self.assertEqual(response["Retry-After"], "7.0")

    def test_rag_stream_uses_ndjson_event_order_without_polling(self):
        stream_response = StreamingHttpResponse(
            [
                b'{"type":"started","job_id":7}\n',
                b'{"type":"sources","citations":[]}\n',
                b'{"type":"token","text":"answer"}\n',
                b'{"type":"completed","job_id":7,"answer":"answer"}\n',
            ],
            content_type="application/x-ndjson",
        )
        admission_token = SimpleNamespace(release=Mock(), release_async=AsyncMock())
        with patch(
            "document_ai.search.views.build_rag_llm_snapshot",
            return_value={
                "llm_endpoint_name": "Server runtime",
                "llm_base_url": "http://rag-runtime:8080",
                "llm_model": "test-model",
            },
        ), patch(
            "document_ai.search.views.server_rag_runtime_availability",
            return_value=(True, {}),
        ), patch(
            "document_ai.search.views.acquire_rag_admission_token_async",
            new=AsyncMock(return_value=admission_token),
        ), patch(
            "document_ai.rag.streaming.create_rag_streaming_response_async",
            new=AsyncMock(return_value=stream_response),
        ):
            response = self.client.post(
                reverse("document_ai:rag-stream"),
                data=json.dumps({"question": "summary", "top_k": 3, "language": "ko"}),
                content_type="application/json",
            )

        events = [
            json.loads(line)
            for line in b"".join(response.streaming_content).decode("utf-8").splitlines()
        ]
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/x-ndjson"))
        self.assertEqual(
            [event["type"] for event in events],
            ["started", "sources", "token", "completed"],
        )
        self.assertNotIn("poll_url", events[0])

    def test_rag_stream_capacity_error_is_json_with_retry_after(self):
        with patch(
            "document_ai.search.views.build_rag_llm_snapshot",
            return_value={
                "llm_endpoint_name": "Server runtime",
                "llm_base_url": "http://rag-runtime:8080",
                "llm_model": "test-model",
            },
        ), patch(
            "document_ai.search.views.server_rag_runtime_availability",
            return_value=(True, {}),
        ), patch(
            "document_ai.search.views.acquire_rag_admission_token_async",
            new=AsyncMock(return_value=None),
        ):
            response = self.client.post(
                reverse("document_ai:rag-stream"),
                data=json.dumps({"question": "summary", "language": "ko"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "RAG_CAPACITY_EXCEEDED")
        self.assertGreater(int(response["Retry-After"]), 0)

    def test_rag_stream_embedding_busy_is_retryable_503_not_generic_500(self):
        from document_ai.rag.streaming import RAGSearchBusyError

        admission_token = SimpleNamespace(release=Mock(), release_async=AsyncMock())
        with patch(
            "document_ai.search.views.build_rag_llm_snapshot",
            return_value={
                "llm_endpoint_name": "Server runtime",
                "llm_base_url": "http://rag-runtime:8080",
                "llm_model": "test-model",
            },
        ), patch(
            "document_ai.search.views.server_rag_runtime_availability",
            return_value=(True, {}),
        ), patch(
            "document_ai.search.views.acquire_rag_admission_token_async",
            new=AsyncMock(return_value=admission_token),
        ), patch(
            "document_ai.rag.streaming.create_rag_streaming_response_async",
            new=AsyncMock(
                side_effect=RAGSearchBusyError(
                    "embedding-executor is busy (EMBEDDING_BUSY), retry after 6.0s",
                    retry_after_seconds=6.0,
                )
            ),
        ):
            response = self.client.post(
                reverse("document_ai:rag-stream"),
                data=json.dumps({"question": "summary", "language": "ko"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "EMBEDDING_BUSY")
        self.assertEqual(response["Retry-After"], "6.0")

    def test_rag_history_returns_only_completed_jobs_owned_by_current_user(self):
        own_job = RAGJob.objects.create(
            owner=self.user,
            question="own question",
            answer="own answer",
            citations=[{"id": 1, "node_id": "node-1", "node_name": "policy.pdf"}],
            language="ko",
            node_ids=["node-1"],
            llm_model="test-model",
            performance_metrics={"total_ms": 123},
            status=AIStatus.COMPLETED,
            completed_at=timezone.now(),
        )
        RAGJob.objects.create(
            workspace=own_job.workspace,
            owner=self.other_user,
            question="private question in same workspace",
            answer="private answer",
            status=AIStatus.COMPLETED,
            completed_at=timezone.now(),
        )
        RAGJob.objects.create(
            workspace=own_job.workspace,
            owner=self.user,
            question="failed question",
            status=AIStatus.FAILED,
        )

        response = self.client.get(reverse("files:api_search_history"), {"limit": 10})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["history"]], [own_job.id])
        self.assertEqual(payload["history"][0]["answer"], "own answer")
        self.assertEqual(payload["history"][0]["llm_model"], "test-model")
        self.assertEqual(payload["history"][0]["performance_metrics"], {"total_ms": 123})

    @override_settings(RAG_SEARCH_TOP_K=4, RAG_RETRIEVAL_THRESHOLD=0.3)
    def test_server_policy_exposes_safe_read_only_runtime_summary(self):
        target = SimpleNamespace(
            model="test-model",
            runtime="llama.cpp",
            priority_preset="balanced",
            selection_mode="automatic",
            serving_profile={"serving_concurrency": 2},
        )
        embedding = SimpleNamespace(
            model_id="BAAI/bge-m3",
            provider="bgem3_hybrid",
            dimension=1024,
            supports_sparse=True,
            distance_strategy="inner_product",
        )
        with patch(
            "document_ai.status_views.target_from_persisted_config",
            return_value=target,
        ), patch(
            "document_ai.status_views.load_llm_runtime_status",
            return_value={
                "status": "healthy",
                "reason_code": "",
                "updated_at": "2026-08-26T00:00:00+00:00",
            },
        ), patch(
            "document_ai.status_views.load_embedding_runtime",
            return_value=embedding,
        ), patch(
            "document_ai.status_views.probe_server_rag_runtime",
            return_value=True,
        ):
            response = self.client.get(reverse("document_ai:server-policy"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["policy"]["search_top_k"], 4)
        self.assertEqual(payload["policy"]["retrieval_threshold"], 0.3)
        self.assertTrue(payload["rag"]["available"])
        self.assertEqual(payload["rag"]["serving_concurrency"], 2)
        self.assertEqual(payload["embedding"]["model"], "BAAI/bge-m3")
        self.assertNotIn("base_url", payload["rag"])
        self.assertNotIn("runtime_fingerprint", payload["embedding"])

    def test_sync_bearer_token_boundary_is_separate_from_spa_session(self):
        missing_token = self.client.get(reverse("sync_api:ping"))
        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(missing_token.json()["ok"], False)

        token = APIToken.objects.create(user=self.user, name="contract token")
        authorized = self.client.get(
            reverse("sync_api:ping"),
            HTTP_AUTHORIZATION=f"Bearer {token.key}",
        )
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.json(), {"ok": True, "message": "pong"})
