import json
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import APIToken
from config.enums import NodeType
from files.models import Node


pytestmark = pytest.mark.unit

User = get_user_model()


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
class SyncContractTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="sync-contract@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.token = APIToken.objects.create(user=self.user, name="Folder sync")
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token.key}"}

    def _diff(self, root_name, entries=None):
        return self.client.post(
            reverse("sync_api:diff"),
            data=json.dumps({"root_name": root_name, "entries": entries or []}),
            content_type="application/json",
            **self.auth,
        )

    def _mkdir(self, root_name, rel_path="seed"):
        return self.client.post(
            reverse("sync_api:mkdir"),
            data=json.dumps({"root_name": root_name, "rel_path": rel_path}),
            content_type="application/json",
            **self.auth,
        ).json()

    def test_sync_identity_returns_account_and_sync_scope(self):
        response = self.client.get(reverse("sync_api:identity"), **self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account"]["id"], str(self.user.pk))
        self.assertEqual(response.json()["token"], {"type": "sync", "scopes": ["sync"]})
        self.assertIsNone(response.json()["workspace"])

    def test_diff_is_read_only_and_upload_uses_the_selected_exact_root(self):
        planned = self._diff("first").json()
        self.assertEqual(planned["root_uid"], "")
        self.assertFalse(Node.objects.filter(path="/sync/first").exists())

        self._mkdir("first")
        self._mkdir("second")
        first = self._diff("first").json()
        second = self._diff("second").json()

        self.assertNotEqual(first["root_uid"], second["root_uid"])
        with patch("document_ai.signals.enqueue_parse"):
            response = self.client.post(
                reverse("sync_api:upload"),
                data={
                    "file": SimpleUploadedFile("report.txt", b"sync", content_type="text/plain"),
                    "rel_path": "docs/report.txt",
                    "root_uid": first["root_uid"],
                    "root_name": "second",
                    "sync_id": first["sync_id"],
                    "content_hash": "hash",
                    "ai_processing_enabled": "0",
                },
                **self.auth,
            )

        self.assertEqual(response.status_code, 200)
        node = Node.objects.get(uid=response.json()["node_uid"])
        self.assertEqual(node.path, "/sync/first/docs/report.txt")

    def test_nested_mkdir_creates_the_requested_path_once(self):
        response = self.client.post(
            reverse("sync_api:mkdir"),
            data=json.dumps({
                "rel_path": "docs/reports",
                "root_name": "local",
            }),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        node = Node.objects.get(uid=response.json()["node_uid"])
        self.assertEqual(node.path, "/sync/local/docs/reports")
        self.assertFalse(Node.objects.filter(path="/sync/local/docs/reports/reports").exists())

    def test_delete_rejects_nodes_outside_selected_sync_root(self):
        root = self._mkdir("local")
        outside = Node.objects.create(
            owner=self.user,
            name="private.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )

        response = self.client.post(
            reverse("sync_api:delete"),
            data=json.dumps({
                "root_uid": root["root_uid"],
                "node_uids": [str(outside.uid)],
            }),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 400)
        outside.refresh_from_db()
        self.assertFalse(outside.trashed)

    def test_legacy_delete_without_root_is_limited_to_one_sync_root(self):
        root = self._mkdir("local")
        sync_root = Node.objects.get(uid=root["root_uid"])
        node = Node.objects.create(
            owner=self.user,
            name="old.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            parent=sync_root,
        )

        response = self.client.post(
            reverse("sync_api:delete"),
            data=json.dumps({"node_uids": [str(node.uid)]}),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        node.refresh_from_db()
        self.assertTrue(node.trashed)

    def test_mutations_reject_parent_traversal(self):
        root = self._mkdir("local")

        response = self.client.post(
            reverse("sync_api:mkdir"),
            data=json.dumps({
                "rel_path": "../escape",
                "root_uid": root["root_uid"],
            }),
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Node.objects.filter(owner=self.user, name="escape").exists())

    def test_diff_does_not_mix_roots_with_prefix_names(self):
        self._mkdir("docs")
        self._mkdir("docs2")
        first = self._diff("docs").json()
        second = self._diff("docs2").json()
        second_root = Node.objects.get(uid=second["root_uid"])
        Node.objects.create(
            owner=self.user,
            name="only-second.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            parent=second_root,
        )

        response = self._diff("docs")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["root_uid"], first["root_uid"])
        self.assertNotIn(
            "only-second.txt",
            {action["rel_path"] for action in response.json()["actions"]},
        )
