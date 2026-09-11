import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import APIToken, CLIToken


pytestmark = pytest.mark.unit

User = get_user_model()


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"], LOGIN_REQUIRED=True)
class AccessTokenApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="access-token@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.other_user = User.objects.create_user(
            email="other-access-token@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.client.force_login(self.user)

    def _issue(self, token_type, name="Client token", access_level=None):
        payload = {"name": name, "token_type": token_type}
        if access_level is not None:
            payload["access_level"] = access_level
        return self.client.post(
            reverse("accounts_api:tokens"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_lists_cli_and_sync_tokens_without_secrets(self):
        cli_response = self._issue("cli", "Laptop", "read_only")
        sync_response = self._issue("sync", "Folder connector")
        cli_secret = cli_response.json()["secret"]
        sync_secret = sync_response.json()["secret"]

        response = self.client.get(reverse("accounts_api:tokens"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            {token["token_type"] for token in payload["tokens"]},
            {"cli", "sync"},
        )
        self.assertEqual(
            {token["access_level"] for token in payload["tokens"]},
            {"read_only", "sync"},
        )
        self.assertNotIn(cli_secret, response.content.decode("utf-8"))
        self.assertNotIn(sync_secret, response.content.decode("utf-8"))
        for token in payload["tokens"]:
            self.assertNotIn("secret", token)
            self.assertNotIn("key_hash", token)

    def test_issues_purpose_specific_tokens(self):
        read_only_response = self._issue("cli", access_level="read_only")
        self.assertEqual(read_only_response.status_code, 201)
        read_only_payload = read_only_response.json()
        self.assertEqual(read_only_payload["token"]["token_type"], "cli")
        self.assertEqual(read_only_payload["token"]["access_level"], "read_only")
        self.assertTrue(read_only_payload["secret"].startswith("dtr_cli_"))
        self.assertIn("documents:read", read_only_payload["token"]["scopes"])
        self.assertNotIn("documents:write", read_only_payload["token"]["scopes"])
        self.assertNotIn("sync", read_only_payload["token"]["scopes"])
        self.assertEqual(read_only_response["Cache-Control"], "no-store")

        read_write_response = self._issue("cli", access_level="read_write")
        self.assertEqual(read_write_response.status_code, 201)
        read_write_payload = read_write_response.json()
        self.assertEqual(read_write_payload["token"]["access_level"], "read_write")
        self.assertIn("documents:write", read_write_payload["token"]["scopes"])
        self.assertNotIn("sync", read_write_payload["token"]["scopes"])
        self.assertEqual(CLIToken.objects.filter(user=self.user).count(), 2)

        sync_response = self._issue("sync")
        self.assertEqual(sync_response.status_code, 201)
        sync_payload = sync_response.json()
        self.assertEqual(sync_payload["token"]["token_type"], "sync")
        self.assertEqual(sync_payload["token"]["access_level"], "sync")
        self.assertEqual(sync_payload["token"]["scopes"], ["sync"])
        self.assertEqual(sync_response["Cache-Control"], "no-store")
        self.assertTrue(
            APIToken.objects.filter(user=self.user, key=sync_payload["secret"]).exists()
        )

        ping = self.client.get(
            reverse("sync_api:ping"),
            HTTP_AUTHORIZATION=f"Bearer {sync_payload['secret']}",
        )
        self.assertEqual(ping.status_code, 200)

    def test_rejects_unknown_token_type(self):
        response = self._issue("mcp")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")
        self.assertFalse(CLIToken.objects.filter(user=self.user).exists())
        self.assertFalse(APIToken.objects.filter(user=self.user).exists())

    def test_rejects_unknown_cli_access_level(self):
        response = self._issue("cli", access_level="full_access")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")
        self.assertFalse(CLIToken.objects.filter(user=self.user).exists())

    def test_rejects_cli_access_level_on_sync_token(self):
        response = self._issue("sync", access_level="read_write")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")
        self.assertFalse(APIToken.objects.filter(user=self.user).exists())

    def test_revoke_is_owner_scoped_and_permanent_for_sync_token(self):
        sync_payload = self._issue("sync").json()
        token = sync_payload["token"]
        revoke_url = reverse(
            "accounts_api:token-revoke",
            kwargs={"token_type": token["token_type"], "identifier": token["id"]},
        )

        self.client.force_login(self.other_user)
        forbidden = self.client.delete(revoke_url)
        self.assertEqual(forbidden.status_code, 404)

        self.client.force_login(self.user)
        revoked = self.client.delete(revoke_url)
        self.assertEqual(revoked.status_code, 200)
        self.assertFalse(APIToken.objects.filter(pk=token["id"]).exists())

        ping = self.client.get(
            reverse("sync_api:ping"),
            HTTP_AUTHORIZATION=f"Bearer {sync_payload['secret']}",
        )
        self.assertEqual(ping.status_code, 401)

    def test_cli_revoke_keeps_audit_record_inactive(self):
        cli_payload = self._issue("cli").json()
        token = cli_payload["token"]

        response = self.client.delete(reverse(
            "accounts_api:token-revoke",
            kwargs={"token_type": "cli", "identifier": token["id"]},
        ))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(CLIToken.objects.get(uid=token["id"]).is_active)

    def test_issue_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        url = reverse("accounts_api:tokens")

        rejected = csrf_client.post(
            url,
            data=json.dumps({"name": "Protected", "token_type": "cli"}),
            content_type="application/json",
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.json()["error"]["code"], "CSRF_FAILED")

        bootstrap = csrf_client.get(reverse("accounts_api:session"))
        issued = csrf_client.post(
            url,
            data=json.dumps({"name": "Protected", "token_type": "cli"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=bootstrap.cookies["csrftoken"].value,
        )
        self.assertEqual(issued.status_code, 201)
        self.assertEqual(issued.json()["token"]["access_level"], "read_only")
