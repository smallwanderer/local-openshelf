"""
[세션 부트스트랩 및 인증 계약(Auth Contract) 검증 테스트]

1. GET /api/auth/session/ (부트스트랩): SPA 초기화 시 로그인 여부 판별 및 csrftoken 쿠키 발급.
2. LOGIN_REQUIRED=True(멀티유저/인증필요) vs False(로컬 단일사용자) 모드별 응답 분기.
3. POST /api/auth/login/, /logout/ 시 CSRF 헤더(X-CSRFToken) 검증 및 에러 포맷 표준.
"""

import json
from uuid import uuid4

import pytest
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import User


pytestmark = pytest.mark.unit


def assert_api_error(test_case, response, *, status, code):
    """표준 에러 응답 포맷 ({"ok": false, "error": {"code": ..., "message": ...}}) 검증 헬퍼"""
    test_case.assertEqual(response.status_code, status)
    payload = response.json()
    test_case.assertFalse(payload["ok"])
    test_case.assertEqual(payload["error"]["code"], code)
    test_case.assertIsInstance(payload["error"]["message"], str)
    test_case.assertIn("details", payload["error"])


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
class SessionApiContractTests(TestCase):
    """SPA 세션 부트스트랩 API 계약 검증"""

    @override_settings(LOGIN_REQUIRED=True)
    def test_bootstrap_returns_anonymous_state_and_issues_csrf_cookie(self):
        """[검증 1] 로그인 필수 모드: 비로그인 상태 반환 및 csrftoken 쿠키 즉시 발급"""
        response = self.client.get(reverse("accounts_api:session"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "auth": {
                    "mode": "required",
                    "login_required": True,
                    "authenticated": False,
                },
                "user": None,
            },
        )
        self.assertIn("csrftoken", response.cookies)

    @override_settings(LOGIN_REQUIRED=False)
    def test_bootstrap_uses_the_server_local_profile_when_login_is_disabled(self):
        response = self.client.get(reverse("accounts_api:session"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["auth"]["mode"], "local")
        self.assertFalse(payload["auth"]["login_required"])
        self.assertTrue(payload["auth"]["authenticated"])
        self.assertEqual(payload["user"]["email"], "local-admin@dotori.local")
        self.assertTrue(payload["user"]["email_verified"])
        self.assertTrue(payload["user"]["is_staff"])
        self.assertIn("csrftoken", response.cookies)

    @override_settings(LOGIN_REQUIRED=True)
    def test_json_login_rotates_session_and_returns_current_user(self):
        user = User.objects.create_user(
            email="spa-login@example.com",
            password="password123",
            display_name="SPA User",
            is_active=True,
            email_verified=True,
        )
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("accounts_api:session"))
        csrf_token = client.cookies["csrftoken"].value

        response = client.post(
            reverse("accounts_api:login"),
            data=json.dumps({"email": user.email, "password": "password123"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["auth"]["authenticated"])
        self.assertEqual(payload["user"]["email"], user.email)
        self.assertEqual(payload["user"]["display_name"], "SPA User")
        self.assertIn("sessionid", client.cookies)
        self.assertIn("csrftoken", client.cookies)

    @override_settings(LOGIN_REQUIRED=True)
    def test_json_login_rejects_invalid_credentials_with_common_error(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("accounts_api:session"))

        response = client.post(
            reverse("accounts_api:login"),
            data=json.dumps({"email": "missing@example.com", "password": "wrong"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )

        assert_api_error(self, response, status=401, code="INVALID_CREDENTIALS")

    @override_settings(LOGIN_REQUIRED=True)
    def test_json_logout_clears_the_authenticated_session(self):
        user = User.objects.create_user(
            email="spa-logout@example.com",
            password="password123",
            is_active=True,
            email_verified=True,
        )
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        client.get(reverse("accounts_api:session"))

        response = client.post(
            reverse("accounts_api:logout"),
            data="{}",
            content_type="application/json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["auth"]["authenticated"])
        self.assertIsNone(response.json()["user"])

    @override_settings(LOGIN_REQUIRED=False)
    def test_local_mode_rejects_password_login_and_logout(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("accounts_api:session"))
        csrf_token = client.cookies["csrftoken"].value

        login_response = client.post(
            reverse("accounts_api:login"),
            data=json.dumps({"email": "user@example.com", "password": "password"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        logout_response = client.post(
            reverse("accounts_api:logout"),
            data="{}",
            content_type="application/json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )

        assert_api_error(self, login_response, status=409, code="LOGIN_NOT_REQUIRED")
        assert_api_error(self, logout_response, status=409, code="LOGOUT_NOT_AVAILABLE")


@override_settings(
    LOGIN_REQUIRED=True,
    DEBUG=False,
    ALLOWED_HOSTS=["testserver", "localhost"],
)
class ProtectedApiErrorContractTests(TestCase):
    def setUp(self):
        self.verified_user = User.objects.create_user(
            email="api-auth@example.com",
            password="password123",
            is_active=True,
            email_verified=True,
        )

    def test_file_api_returns_json_401_instead_of_login_redirect(self):
        response = self.client.get(reverse("files:api_list"))

        assert_api_error(self, response, status=401, code="AUTHENTICATION_REQUIRED")
        self.assertIsNone(response.get("Location"))

    def test_file_api_returns_json_403_for_unverified_user(self):
        user = User.objects.create_user(
            email="unverified@example.com",
            password="password123",
            is_active=True,
            email_verified=False,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("files:api_list"))

        assert_api_error(self, response, status=403, code="EMAIL_VERIFICATION_REQUIRED")

    def test_document_ai_apis_return_the_same_unauthenticated_error(self):
        search_response = self.client.post(
            reverse("document_ai:vector-search"),
            data=json.dumps({"query": "test"}),
            content_type="application/json",
        )

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.get(reverse("accounts_api:session"))
        rag_response = csrf_client.post(
            reverse("document_ai:rag-stream"),
            data=json.dumps({"question": "test"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_client.cookies["csrftoken"].value,
        )
        policy_response = self.client.get(reverse("document_ai:server-policy"))

        assert_api_error(self, search_response, status=401, code="AUTHENTICATION_REQUIRED")
        assert_api_error(self, rag_response, status=401, code="AUTHENTICATION_REQUIRED")
        assert_api_error(self, policy_response, status=401, code="AUTHENTICATION_REQUIRED")

    def test_django_and_drf_csrf_failures_use_the_common_json_error(self):
        django_client = Client(enforce_csrf_checks=True)
        django_client.force_login(self.verified_user)
        file_response = django_client.post(
            reverse("files:api_create_folder"),
            data={"name": "blocked"},
        )

        drf_client = Client(enforce_csrf_checks=True)
        drf_client.force_login(self.verified_user)
        search_response = drf_client.post(
            reverse("document_ai:vector-search"),
            data=json.dumps({"query": "blocked"}),
            content_type="application/json",
        )

        assert_api_error(self, file_response, status=403, code="CSRF_FAILED")
        assert_api_error(self, search_response, status=403, code="CSRF_FAILED")

    def test_missing_owned_resource_uses_json_404(self):
        self.client.force_login(self.verified_user)

        response = self.client.get(
            reverse("files:api_detail", kwargs={"uid": uuid4()})
        )

        assert_api_error(self, response, status=404, code="NOT_FOUND")
