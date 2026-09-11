import json
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import JsonResponse
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.cli_tokens import issue_cli_token
from config.enums import NodeType
from files.models import Node


pytestmark = pytest.mark.unit

User = get_user_model()


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"], LOGIN_REQUIRED=True)
class CLIHttpApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="cli-api@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.other_user = User.objects.create_user(
            email="other-cli-api@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.token, self.secret = issue_cli_token(user=self.user, name="CLI API")
        self.client = Client(enforce_csrf_checks=True)

    @property
    def authorization(self):
        return f"Bearer {self.secret}"

    def test_cli_routes_are_stable(self):
        self.assertEqual(reverse("cli_api:identity"), "/api/cli/v1/identity/")
        self.assertEqual(reverse("cli_api:status"), "/api/cli/v1/status/")
        self.assertEqual(reverse("cli_api:documents"), "/api/cli/v1/documents/")
        self.assertEqual(reverse("cli_api:upload"), "/api/cli/v1/upload/")
        self.assertEqual(reverse("cli_api:search"), "/api/cli/v1/search/")
        self.assertEqual(reverse("cli_api:ask-stream"), "/api/cli/v1/ask/stream/")

    def test_identity_returns_verified_account_token_and_reserved_workspace(self):
        response = self.client.get(
            reverse("cli_api:identity"),
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account"], {
            "id": str(self.user.pk),
            "email": self.user.email,
            "display_name": self.user.display_name,
        })
        self.assertEqual(response.json()["token"]["type"], "cli")
        self.assertEqual(response.json()["token"]["scopes"], self.token.scopes)
        self.assertIsNone(response.json()["workspace"])

    def test_identity_does_not_require_an_unrelated_capability_scope(self):
        self.token.scopes = []
        self.token.save(update_fields=["scopes"])

        response = self.client.get(
            reverse("cli_api:identity"),
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token"]["scopes"], [])

    def test_document_list_uses_token_owner_and_needs_no_session_csrf(self):
        own = Node.objects.create(
            owner=self.user,
            name="own.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )
        Node.objects.create(
            owner=self.other_user,
            name="private.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )

        response = self.client.get(
            reverse("cli_api:documents"),
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["uid"] for item in response.json()["files"]],
            [str(own.uid)],
        )

    @patch("document_ai.signals.enqueue_parse")
    def test_upload_uses_cli_token_owner_without_csrf(self, parse_delay):
        response = self.client.post(
            reverse("cli_api:upload"),
            data={
                "file": SimpleUploadedFile(
                    "cli.txt",
                    b"from cli",
                    content_type="text/plain",
                )
            },
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        node = Node.objects.get(uid=response.json()["file"]["uid"])
        self.assertEqual(node.owner, self.user)
        parse_delay.assert_called_once()

    def test_read_only_token_cannot_upload(self):
        _, read_only_secret = issue_cli_token(
            user=self.user,
            name="MCP read only",
            access_level="read_only",
        )

        response = self.client.post(
            reverse("cli_api:upload"),
            data={
                "file": SimpleUploadedFile(
                    "denied.txt",
                    b"must not upload",
                    content_type="text/plain",
                )
            },
            HTTP_AUTHORIZATION=f"Bearer {read_only_secret}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "CLI_TOKEN_SCOPE_REQUIRED")
        self.assertFalse(Node.objects.filter(owner=self.user, name="denied.txt").exists())

    def test_search_uses_cli_token_owner(self):
        with patch(
            "document_ai.search.views.search_documents_sync",
            return_value=([], {"request_search_ms": 1.5}),
        ) as search:
            response = self.client.post(
                reverse("cli_api:search"),
                data=json.dumps({"mode": "basic", "query": "policy"}),
                content_type="application/json",
                HTTP_AUTHORIZATION=self.authorization,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(search.call_args.kwargs["owner"], self.user)

    def test_status_uses_cli_token_owner(self):
        def status_response(request):
            return JsonResponse({"ok": True, "user": request.user.email})

        with patch("document_ai.cli_views.server_policy", side_effect=status_response):
            response = self.client.get(
                reverse("cli_api:status"),
                HTTP_AUTHORIZATION=self.authorization,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"], self.user.email)

    def test_ask_authenticates_before_rag_validation(self):
        response = self.client.post(
            reverse("cli_api:ask-stream"),
            data=json.dumps({}),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")

    def test_missing_scope_and_legacy_sync_token_are_rejected(self):
        self.token.scopes = ["status:read"]
        self.token.save(update_fields=["scopes"])
        denied = self.client.get(
            reverse("cli_api:documents"),
            HTTP_AUTHORIZATION=self.authorization,
        )
        missing = self.client.get(reverse("cli_api:documents"))

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "CLI_TOKEN_SCOPE_REQUIRED")
        self.assertEqual(missing.status_code, 401)
