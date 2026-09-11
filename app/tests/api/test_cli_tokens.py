import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import APIToken, CLIToken


pytestmark = pytest.mark.unit

User = get_user_model()


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"], LOGIN_REQUIRED=True)
class CLITokenApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="cli-token@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.other_user = User.objects.create_user(
            email="other-cli-token@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.client.force_login(self.user)

    def _create_token(self, *, name="Laptop CLI", access_level=None, enable_sync=None):
        payload = {"name": name}
        if access_level is not None:
            payload["access_level"] = access_level
        if enable_sync is not None:
            payload["enable_sync"] = enable_sync
        return self.client.post(
            reverse("accounts_api:cli-tokens"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_cli_token_secret_is_returned_once_and_stored_as_hash(self):
        response = self._create_token()

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        secret = payload["secret"]
        token = CLIToken.objects.get(uid=payload["token"]["uid"])
        self.assertTrue(secret.startswith("dtr_cli_"))
        self.assertNotEqual(token.key_hash, secret)
        self.assertEqual(token.prefix, secret[:16])
        self.assertNotIn("sync", token.scopes)
        self.assertIn("documents:write", token.scopes)
        self.assertEqual(payload["token"]["access_level"], "read_write")
        self.assertEqual(payload["secret_display"], "once")
        self.assertEqual(response["Cache-Control"], "no-store")

        listed = self.client.get(reverse("accounts_api:cli-tokens"))
        self.assertEqual(listed.status_code, 200)
        listed_payload = listed.json()
        self.assertEqual(len(listed_payload["tokens"]), 1)
        self.assertNotIn("secret", listed_payload["tokens"][0])
        self.assertNotIn("key_hash", listed_payload["tokens"][0])
        self.assertNotIn(secret, listed.content.decode("utf-8"))

    def test_cli_token_endpoint_rejects_combined_sync_scope(self):
        response = self._create_token(enable_sync=True)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")
        self.assertFalse(CLIToken.objects.filter(user=self.user).exists())

    def test_read_only_cli_token_omits_write_and_sync_scopes(self):
        response = self._create_token(access_level="read_only")

        self.assertEqual(response.status_code, 201)
        token = response.json()["token"]
        self.assertEqual(token["access_level"], "read_only")
        self.assertIn("documents:read", token["scopes"])
        self.assertNotIn("documents:write", token["scopes"])
        self.assertNotIn("sync", token["scopes"])

    def test_cli_token_without_sync_scope_cannot_use_sync_api(self):
        response = self._create_token(access_level="read_write")
        secret = response.json()["secret"]

        ping = self.client.get(
            reverse("sync_api:ping"),
            HTTP_AUTHORIZATION=f"Bearer {secret}",
        )

        self.assertEqual(ping.status_code, 403)
        self.assertEqual(ping.json()["code"], "CLI_TOKEN_SCOPE_REQUIRED")

    def test_previously_issued_cli_token_with_sync_scope_remains_valid(self):
        response = self._create_token(access_level="read_write")
        payload = response.json()
        token = CLIToken.objects.get(uid=payload["token"]["uid"])
        token.scopes = [*token.scopes, "sync"]
        token.save(update_fields=["scopes"])

        ping = self.client.get(
            reverse("sync_api:ping"),
            HTTP_AUTHORIZATION=f"Bearer {payload['secret']}",
        )

        self.assertEqual(ping.status_code, 200)

    def test_revoked_cli_token_is_rejected_and_other_user_cannot_revoke(self):
        response = self._create_token(access_level="read_write")
        payload = response.json()
        token_uid = payload["token"]["uid"]

        self.client.force_login(self.other_user)
        forbidden = self.client.delete(
            reverse("accounts_api:cli-token-revoke", kwargs={"uid": token_uid})
        )
        self.assertEqual(forbidden.status_code, 404)

        self.client.force_login(self.user)
        revoked = self.client.delete(
            reverse("accounts_api:cli-token-revoke", kwargs={"uid": token_uid})
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertFalse(CLIToken.objects.get(uid=token_uid).is_active)

        ping = self.client.get(
            reverse("sync_api:ping"),
            HTTP_AUTHORIZATION=f"Bearer {payload['secret']}",
        )
        self.assertEqual(ping.status_code, 401)

    def test_legacy_sync_token_remains_valid_and_separate(self):
        legacy = APIToken.objects.create(user=self.user, name="Folder connector")

        ping = self.client.get(
            reverse("sync_api:ping"),
            HTTP_AUTHORIZATION=f"Bearer {legacy.key}",
        )

        self.assertEqual(ping.status_code, 200)
        self.assertFalse(CLIToken.objects.filter(user=self.user).exists())

    def test_token_api_requires_verified_session_and_valid_boolean(self):
        invalid = self.client.post(
            reverse("accounts_api:cli-tokens"),
            data=json.dumps({"name": "CLI", "enable_sync": "yes"}),
            content_type="application/json",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_REQUEST")

        self.user.email_verified = False
        self.user.save(update_fields=["email_verified"])
        unverified = self.client.get(reverse("accounts_api:cli-tokens"))
        self.assertEqual(unverified.status_code, 403)
        self.assertEqual(
            unverified.json()["error"]["code"],
            "EMAIL_VERIFICATION_REQUIRED",
        )

    def test_cli_token_issuance_requires_session_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)

        rejected = csrf_client.post(
            reverse("accounts_api:cli-tokens"),
            data=json.dumps({"name": "Protected CLI", "enable_sync": False}),
            content_type="application/json",
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.json()["error"]["code"], "CSRF_FAILED")

        bootstrap = csrf_client.get(reverse("accounts_api:session"))
        csrf_token = bootstrap.cookies["csrftoken"].value
        issued = csrf_client.post(
            reverse("accounts_api:cli-tokens"),
            data=json.dumps({"name": "Protected CLI", "enable_sync": False}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(issued.status_code, 201)
