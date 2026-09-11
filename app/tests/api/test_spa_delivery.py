import json
import tempfile
from pathlib import Path
from uuid import UUID

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import resolve, reverse

from files.views.spa import _load_spa_entry


pytestmark = pytest.mark.unit

User = get_user_model()

TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    LOGIN_REQUIRED=True,
    STORAGES=TEST_STORAGES,
)
class SPADeliveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="spa-delivery@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.client.force_login(self.user)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.manifest_path = Path(self.temp_dir.name) / "manifest.json"
        self.manifest_path.write_text(
            json.dumps(
                {
                    "index.html": {
                        "file": "assets/index-example.js",
                        "css": ["assets/index-example.css"],
                    }
                }
            ),
            encoding="utf-8",
        )
        _load_spa_entry.cache_clear()
        self.addCleanup(_load_spa_entry.cache_clear)

    def test_root_and_workspace_routes_render_the_same_built_shell(self):
        with override_settings(SPA_ENABLED=True, SPA_MANIFEST_PATH=self.manifest_path):
            for path in ["/", "/workspace/", "/workspace/chat/", "/workspace/settings/"]:
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, "/static/spa/assets/index-example.js")
                    self.assertContains(response, "/static/spa/assets/index-example.css")
                    self.assertEqual(response.headers["Cache-Control"], "max-age=0, no-cache, no-store, must-revalidate, private")

    def test_anonymous_shell_redirects_to_login_without_hiding_api_routes(self):
        self.client.logout()
        with override_settings(SPA_ENABLED=True, SPA_MANIFEST_PATH=self.manifest_path):
            shell_response = self.client.get("/workspace/chat/")
        self.assertEqual(shell_response.status_code, 302)
        self.assertEqual(shell_response.url, "/accounts/login/?next=/workspace/chat/")
        self.assertEqual(resolve("/api/document-ai/v1/rag/stream/").url_name, "rag-stream")

    def test_operator_can_disable_spa_and_use_retained_template_ui(self):
        with override_settings(SPA_ENABLED=False, SPA_MANIFEST_PATH=self.manifest_path):
            response = self.client.get("/")
        self.assertRedirects(response, reverse("files:index"), fetch_redirect_response=False)

    def test_missing_build_returns_recoverable_503(self):
        missing = Path(self.temp_dir.name) / "missing.json"
        with override_settings(SPA_ENABLED=True, SPA_MANIFEST_PATH=missing):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, "DOTORI_SPA_ENABLED=0", status_code=503)

    def test_spa_fallback_is_scoped_away_from_legacy_and_download_routes(self):
        sample_uid = UUID("11111111-1111-1111-1111-111111111111")
        self.assertEqual(resolve("/files/").view_name, "files:index")
        self.assertEqual(resolve("/admin/").url_name, "index")
        self.assertEqual(
            resolve(f"/files/api/v1/{sample_uid}/download/").view_name,
            "files:api_download",
        )
        self.assertEqual(resolve("/workspace/unknown/client/route/").url_name, "spa-route")
