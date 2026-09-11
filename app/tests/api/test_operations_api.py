from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from config.enums import AIStatus, FileOperation, NodeType
from document_ai.models import (
    DocumentChunk,
    DocumentParseResult,
    RAGJob,
    ResourceSnapshot,
    SearchJob,
)
from files.models import FileOperationLog, Node


pytestmark = pytest.mark.integration
User = get_user_model()


def assert_api_error(testcase, response, *, status, code):
    testcase.assertEqual(response.status_code, status)
    testcase.assertEqual(response.json()["error"]["code"], code)


class OperationsApiTests(TestCase):
    def setUp(self):
        self.operator = User.objects.create_user(
            email="operator-metrics@example.com",
            password="password",
            is_active=True,
            email_verified=True,
            is_staff=True,
        )
        self.member = User.objects.create_user(
            email="member-metrics@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.unverified_operator = User.objects.create_user(
            email="unverified-operator@example.com",
            password="password",
            is_active=True,
            email_verified=False,
            is_staff=True,
        )

    def _seed_metrics(self):
        shared_trace = "trace-shared-001"
        FileOperationLog.objects.create(
            owner=self.member,
            operation=FileOperation.UPLOAD,
            status=AIStatus.COMPLETED,
            detail={"original_name": "private-upload.txt"},
            performance_metrics={"trace_id": shared_trace, "total_ms": 100},
        )
        FileOperationLog.objects.create(
            owner=self.member,
            operation=FileOperation.UPLOAD,
            status=AIStatus.FAILED,
            error_message="token=secret-value upload failed",
            performance_metrics={"trace_id": "trace-upload-failed", "total_ms": 300},
        )

        node = Node.objects.create(
            owner=self.member,
            name="failed-private-document.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )
        parse_result = DocumentParseResult.objects.create(
            node=node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
            performance_metrics={
                "trace_id": "trace-ingest-001",
                "queue_wait_ms": 10,
                "parse_processing_ms": 90,
            },
        )
        DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="private document text",
            status=AIStatus.FAILED,
            error_message={"message": "password=hunter2 embedding failed"},
            performance_metrics={
                "trace_id": "trace-ingest-001",
                "queue_wait_ms": 20,
                "embedding_processing_ms": 180,
            },
        )

        SearchJob.objects.create(
            owner=self.member,
            query="PRIVATE SEARCH QUERY MUST NOT LEAK",
            status=AIStatus.COMPLETED,
            results=[{"private": "result"}],
            performance_metrics={
                "trace_id": shared_trace,
                "query_embedding_ms": 120,
                "vector_query_ms": 80,
                "end_to_end_ms": 500,
                "result_count": 1,
            },
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        RAGJob.objects.create(
            owner=self.member,
            question="PRIVATE RAG QUESTION MUST NOT LEAK",
            status=AIStatus.FAILED,
            error_message="api_key=private-key runtime timeout",
            performance_metrics={
                "trace_id": "trace-rag-timeout",
                "context_build_ms": 200,
                "llm_ttft_ms": 800,
                "end_to_end_ms": 1000,
                "timeout": True,
            },
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        return shared_trace

    @override_settings(LOGIN_REQUIRED=True)
    def test_operator_endpoints_reject_anonymous_member_and_unverified_staff(self):
        url = reverse("document_ai:operation-metrics")
        assert_api_error(self, self.client.get(url), status=401, code="AUTHENTICATION_REQUIRED")

        self.client.force_login(self.member)
        assert_api_error(self, self.client.get(url), status=403, code="PERMISSION_DENIED")

        self.client.force_login(self.unverified_operator)
        assert_api_error(
            self,
            self.client.get(url),
            status=403,
            code="EMAIL_VERIFICATION_REQUIRED",
        )

    def test_metrics_aggregate_server_wide_records_and_report_measurement_counts(self):
        self._seed_metrics()
        self.client.force_login(self.operator)

        response = self.client.get(
            reverse("document_ai:operation-metrics"), {"window": "24h"}
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary"]["upload"]["total_count"], 2)
        self.assertEqual(payload["summary"]["upload"]["success_rate"], 50.0)
        self.assertEqual(payload["summary"]["upload"]["duration"]["average"], 200.0)
        self.assertEqual(payload["summary"]["search"]["duration"]["maximum"], 500.0)
        self.assertEqual(payload["summary"]["rag"]["timeout_count"], 1)

        pipelines = {item["name"]: item for item in payload["pipelines"]}
        embedding = pipelines["embedding"]["metrics"]["embedding_processing_ms"]
        self.assertEqual(embedding["average"], 180.0)
        self.assertEqual(embedding["measured_count"], 1)
        self.assertEqual(embedding["total_count"], 1)

        body = response.content.decode()
        self.assertNotIn("PRIVATE SEARCH QUERY", body)
        self.assertNotIn("PRIVATE RAG QUESTION", body)
        self.assertNotIn("private document text", body)

    def test_events_and_trace_expose_structure_without_queries_or_secret_values(self):
        shared_trace = self._seed_metrics()
        self.client.force_login(self.operator)

        events = self.client.get(
            reverse("document_ai:operation-events"), {"window": "24h", "limit": 10}
        )
        trace = self.client.get(
            reverse("document_ai:operation-trace", kwargs={"trace_id": shared_trace})
        )

        self.assertEqual(events.status_code, 200)
        self.assertEqual(trace.status_code, 200)
        self.assertTrue(any(item["pipeline"] == "rag" for item in events.json()["events"]))
        trace_payload = trace.json()
        self.assertEqual(trace_payload["trace_id"], shared_trace)
        self.assertEqual(
            {row["pipeline"] for row in trace_payload["records"]},
            {"upload", "search"},
        )
        combined = events.content.decode() + trace.content.decode()
        self.assertNotIn("PRIVATE SEARCH QUERY", combined)
        self.assertNotIn("PRIVATE RAG QUESTION", combined)
        self.assertNotIn("secret-value", combined)
        self.assertNotIn("private-key", combined)

    def test_invalid_window_and_unknown_trace_use_common_errors(self):
        self.client.force_login(self.operator)
        invalid_window = self.client.get(
            reverse("document_ai:operation-metrics"), {"window": "30d"}
        )
        missing_trace = self.client.get(
            reverse("document_ai:operation-trace", kwargs={"trace_id": "missing-trace"})
        )

        assert_api_error(self, invalid_window, status=400, code="INVALID_REQUEST")
        assert_api_error(self, missing_trace, status=404, code="NOT_FOUND")

    @patch("document_ai.operation_views.build_operation_status")
    def test_status_is_operator_only_and_returns_service_sections(self, build_status):
        build_status.return_value = {
            "generated_at": timezone.now().isoformat(),
            "services": {"app": {"available": True, "status": "healthy"}},
            "processing": {"parse": {}, "embedding": {}, "recent_failures": []},
            "admission": {"available": False, "active": None, "limit": 1, "rejected_count": None},
            "server": {"operation_mode": "search"},
        }
        self.client.force_login(self.operator)

        response = self.client.get(reverse("document_ai:operation-status"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["services"]["app"]["available"])

    def test_resource_snapshot_read_and_manual_collection(self):
        snapshot = ResourceSnapshot.objects.create(service="disk:uploads", disk_free_mb=2048)
        self.client.force_login(self.operator)

        response = self.client.get(reverse("document_ai:operation-resources"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["snapshots"][0]["disk_free_mb"], 2048)

        collection = SimpleNamespace(rows=[snapshot], skipped=[])
        with patch(
            "document_ai.operation_views.collect_resource_snapshots",
            return_value=collection,
        ):
            collected = self.client.post(
                reverse("document_ai:collect-operation-resources"), data={}
            )
        self.assertEqual(collected.status_code, 200)
        self.assertEqual(collected.json()["snapshots"][0]["service"], "disk:uploads")

    def test_operator_route_contract(self):
        self.assertEqual(
            reverse("document_ai:operation-status"),
            "/api/document-ai/v1/operations/status/",
        )
        self.assertEqual(
            reverse("document_ai:operation-metrics"),
            "/api/document-ai/v1/operations/metrics/",
        )
        self.assertEqual(
            reverse("document_ai:operation-events"),
            "/api/document-ai/v1/operations/events/",
        )
        self.assertEqual(
            reverse("document_ai:operation-resources"),
            "/api/document-ai/v1/operations/resources/",
        )
