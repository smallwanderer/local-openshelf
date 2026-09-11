import json
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase

from config.enums import AIStatus, NodeType
from document_ai.models import DocumentChunk, DocumentParseResult
from document_ai.search.evaluation import execute_quality_evaluation_run
from files.models import Node
from workspaces.models import (
    WorkspaceEvaluationDataset,
    WorkspaceMembership,
    WorkspaceQualityEvaluationRun,
)
from workspaces.services import create_team_workspace


User = get_user_model()

pytestmark = pytest.mark.unit


class RetrievalEvaluationApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="eval-admin@example.com",
            password="test-pass",
            email_verified=True,
            is_active=True,
        )
        self.member = User.objects.create_user(
            email="eval-member@example.com",
            password="test-pass",
            email_verified=True,
            is_active=True,
        )
        self.workspace = create_team_workspace(actor=self.admin, name="Eval Team")
        WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.member,
            role=WorkspaceMembership.ROLE_MEMBER,
            status=WorkspaceMembership.STATUS_ACTIVE,
        )

        self.node = Node.objects.create(
            owner=self.admin, workspace=self.workspace, name="matching-doc.txt", ext=".txt", node_type=NodeType.FILE,
        )
        parse_result = DocumentParseResult.objects.create(node=self.node, status=AIStatus.COMPLETED, chunk_count=1)
        DocumentChunk.objects.create(
            parse_result=parse_result, chunk_index=0, text="this chunk should match", status=AIStatus.COMPLETED,
        )

    def request_json(self, method, path, payload=None):
        return getattr(self.client, method)(
            path,
            data=json.dumps(payload or {}),
            content_type="application/json",
        )

    def login_in_workspace(self, user):
        self.client.force_login(user)
        response = self.request_json(
            "post", "/api/workspaces/v1/switch/", {"workspace_uid": str(self.workspace.uid)},
        )
        self.assertEqual(response.status_code, 200)

    def create_dataset(self):
        return self.request_json(
            "post",
            "/api/workspaces/v1/current/evaluation-datasets/",
            {
                "axis": "retrieval",
                "name": "smoke dataset",
                "items": [{"query": "match please", "expected_node_ids": [str(self.node.uid)]}],
            },
        )

    def test_member_cannot_create_dataset(self):
        self.login_in_workspace(self.member)
        response = self.create_dataset()
        self.assertEqual(response.status_code, 403)

    def test_invalid_dataset_items_are_rejected(self):
        self.login_in_workspace(self.admin)
        response = self.request_json(
            "post",
            "/api/workspaces/v1/current/evaluation-datasets/",
            {"axis": "retrieval", "name": "bad", "items": [{"query": ""}]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "DATASET_VALIDATION_FAILED")

    @patch("document_ai.search.execution.search_documents_sync")
    @patch("document_ai.orchestration.enqueue_retrieval_evaluation")
    def test_evaluation_run_end_to_end_and_verified_apply(self, mock_enqueue, mock_search):
        # The evaluation scores whatever the production retrieval path returns.
        mock_search.return_value = (
            [{"node_id": str(self.node.uid), "node_name": self.node.name, "doc_score": 0.9}],
            {"request_search_ms": 5.0},
        )
        self.login_in_workspace(self.admin)
        created = self.create_dataset()
        self.assertEqual(created.status_code, 201)
        dataset_uid = created.json()["dataset"]["uid"]
        self.assertEqual(WorkspaceEvaluationDataset.objects.count(), 1)

        self.client.get("/api/workspaces/v1/current/quality-profile/")
        draft = self.request_json(
            "patch",
            "/api/workspaces/v1/current/quality-profile/draft/",
            {
                "expected_revision": 0,
                "retrieval": {"overrides": {"dense_weight": 0.4, "sparse_weight": 0.6, "search_top_k": 5}, "reset_fields": []},
                "note": "candidate",
            },
        )
        self.assertEqual(draft.status_code, 200)

        started = self.request_json(
            "post",
            "/api/workspaces/v1/current/quality-profile/evaluate/",
            {"expected_revision": 1, "dataset_uid": dataset_uid},
        )
        self.assertEqual(started.status_code, 202)
        run_uid = started.json()["run"]["uid"]
        mock_enqueue.assert_called_once_with(run_uid)

        # Run the executor handler synchronously; the Celery task itself only
        # dispatches this work kind to the executor transport.
        execute_quality_evaluation_run(run_uid)

        run_response = self.client.get(f"/api/workspaces/v1/current/evaluation-runs/{run_uid}/")
        self.assertEqual(run_response.status_code, 200)
        run_body = run_response.json()["run"]
        self.assertEqual(run_body["status"], WorkspaceQualityEvaluationRun.STATUS_SUCCEEDED)
        self.assertEqual(run_body["metrics"]["hit_rate_at_1"], 1.0)

        # The draft is auto-verified once its evaluation run succeeds, and the
        # settings UI reads last_run_uid straight off the draft to pass at apply time.
        refreshed = self.client.get("/api/workspaces/v1/current/quality-profile/")
        refreshed_draft = refreshed.json()["draft"]
        self.assertEqual(refreshed_draft["validation"]["state"], "verified")
        self.assertEqual(refreshed_draft["validation"]["last_run_uid"], run_uid)

        applied = self.request_json(
            "post",
            "/api/workspaces/v1/current/quality-profile/apply/",
            {"expected_revision": 1, "evaluation_run_uid": refreshed_draft["validation"]["last_run_uid"], "note": "verified via dataset"},
        )
        self.assertEqual(applied.status_code, 200)
        body = applied.json()
        self.assertEqual(body["active"]["validation"]["state"], "verified")
        self.assertEqual(body["active"]["validation"]["last_run_uid"], run_uid)

    def test_stale_run_after_draft_edit_is_rejected_on_apply(self):
        self.login_in_workspace(self.admin)
        created = self.create_dataset()
        dataset_uid = created.json()["dataset"]["uid"]

        self.client.get("/api/workspaces/v1/current/quality-profile/")
        self.request_json(
            "patch",
            "/api/workspaces/v1/current/quality-profile/draft/",
            {"expected_revision": 0, "retrieval": {"overrides": {"search_top_k": 9}, "reset_fields": []}, "note": "v1"},
        )
        with patch("document_ai.orchestration.enqueue_retrieval_evaluation") as mock_enqueue:
            started = self.request_json(
                "post",
                "/api/workspaces/v1/current/quality-profile/evaluate/",
                {"expected_revision": 1, "dataset_uid": dataset_uid},
            )
            run_uid = started.json()["run"]["uid"]
            mock_enqueue.assert_called_once_with(run_uid)
        run = WorkspaceQualityEvaluationRun.objects.get(uid=run_uid)
        run.status = WorkspaceQualityEvaluationRun.STATUS_SUCCEEDED
        run.metrics = {"hit_rate_at_1": 1.0}
        run.save(update_fields=["status", "metrics"])

        # Edit the draft again after the run was captured -> the run is now stale.
        self.request_json(
            "patch",
            "/api/workspaces/v1/current/quality-profile/draft/",
            {"expected_revision": 1, "retrieval": {"overrides": {"search_top_k": 10}, "reset_fields": []}, "note": "v2"},
        )

        applied = self.request_json(
            "post",
            "/api/workspaces/v1/current/quality-profile/apply/",
            {"expected_revision": 2, "evaluation_run_uid": run_uid, "note": "stale attempt"},
        )
        self.assertEqual(applied.status_code, 409)
        self.assertEqual(applied.json()["error"]["code"], "EVALUATION_STALE")
