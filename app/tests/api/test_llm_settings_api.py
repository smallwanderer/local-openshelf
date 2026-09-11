import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from document_ai.models import LLMEndpoint
from workspaces.models import WorkspaceMembership
from workspaces.services import create_team_workspace

User = get_user_model()


class LLMSettingsApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="llm-admin@example.com",
            password="test-pass",
            email_verified=True,
            is_active=True,
        )
        self.member = User.objects.create_user(
            email="llm-member@example.com",
            password="test-pass",
            email_verified=True,
            is_active=True,
        )
        self.workspace = create_team_workspace(actor=self.admin, name="LLM Team")
        WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.member,
            role=WorkspaceMembership.ROLE_MEMBER,
            status=WorkspaceMembership.STATUS_ACTIVE,
        )
        self.server_target_patch = patch(
            "document_ai.services.llm_endpoint_service.get_cached_server_rag_target",
            return_value=SimpleNamespace(base_url="http://test-rag:8080", model="test-server-model"),
        )
        self.server_target_patch.start()
        self.addCleanup(self.server_target_patch.stop)

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

    def test_member_can_view_but_not_select(self):
        self.login_in_workspace(self.member)
        response = self.client.get("/api/workspaces/v1/current/llm-settings/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["can_edit"])
        self.assertEqual(body["active"]["source"], "server")
        self.assertEqual(body["server_default_model"], "test-server-model")
        self.assertEqual(body["endpoints"], [])

        select = self.request_json(
            "post", "/api/workspaces/v1/current/llm-settings/select/", {"endpoint_id": None},
        )
        self.assertEqual(select.status_code, 403)

    def test_admin_can_select_and_revert_endpoint(self):
        endpoint = LLMEndpoint.objects.create(
            owner=self.admin,
            workspace=self.workspace,
            name="Local vLLM",
            endpoint_type=LLMEndpoint.ENDPOINT_VLLM,
            base_url="http://vllm:8000",
            default_model="qwen-14b",
        )
        self.login_in_workspace(self.admin)

        listed = self.client.get("/api/workspaces/v1/current/llm-settings/")
        self.assertEqual(listed.status_code, 200)
        listed_body = listed.json()
        self.assertTrue(listed_body["can_edit"])
        self.assertEqual([item["id"] for item in listed_body["endpoints"]], [endpoint.id])

        selected = self.request_json(
            "post", "/api/workspaces/v1/current/llm-settings/select/", {"endpoint_id": endpoint.id},
        )
        self.assertEqual(selected.status_code, 200)
        body = selected.json()
        self.assertEqual(body["selected_endpoint_id"], endpoint.id)
        self.assertEqual(body["active"], {
            "source": "external",
            "label": "Local vLLM",
            "base_url": "http://vllm:8000",
            "model": "qwen-14b",
            "endpoint_type": "vLLM",
        })

        reverted = self.request_json(
            "post", "/api/workspaces/v1/current/llm-settings/select/", {"endpoint_id": None},
        )
        self.assertEqual(reverted.status_code, 200)
        reverted_body = reverted.json()
        self.assertIsNone(reverted_body["selected_endpoint_id"])
        self.assertEqual(reverted_body["active"]["source"], "server")
        self.assertEqual(reverted_body["active"]["model"], "test-server-model")

    def test_invalid_endpoint_id_is_rejected(self):
        self.login_in_workspace(self.admin)
        response = self.request_json(
            "post", "/api/workspaces/v1/current/llm-settings/select/", {"endpoint_id": "not-an-int"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")

    def test_endpoint_from_another_workspace_is_ignored(self):
        other_workspace = create_team_workspace(actor=self.member, name="Other Team")
        foreign_endpoint = LLMEndpoint.objects.create(
            owner=self.member,
            workspace=other_workspace,
            name="Foreign Endpoint",
            base_url="http://foreign:9000",
            default_model="foreign-model",
        )
        self.login_in_workspace(self.admin)
        response = self.request_json(
            "post", "/api/workspaces/v1/current/llm-settings/select/", {"endpoint_id": foreign_endpoint.id},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["selected_endpoint_id"])
        self.assertEqual(body["active"]["source"], "server")
