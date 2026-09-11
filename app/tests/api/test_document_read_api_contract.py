import pytest
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from document_ai.models import DocumentParseResult
from files.models import Node, NodeType


pytestmark = pytest.mark.unit


@override_settings(
    ALLOWED_HOSTS=["testserver", "localhost"],
    LOGIN_REQUIRED=True,
)
class DocumentReadApiContractTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="reader@example.com",
            password="password123",
            is_active=True,
            email_verified=True,
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            password="password123",
            is_active=True,
            email_verified=True,
        )
        self.client.force_login(self.user)
        self.folder = Node.objects.create(
            owner=self.user,
            name="Reports",
            ext="",
            node_type=NodeType.FOLDER,
        )
        self.document = Node.objects.create(
            owner=self.user,
            parent=self.folder,
            name="strategy.pdf",
            ext=".pdf",
            node_type=NodeType.FILE,
        )
        DocumentParseResult.objects.create(
            node=self.document,
            summary="전략 요약",
            auto_tags=["전략", "운영"],
        )

    def test_list_exposes_stable_pagination_and_parent_uuid(self):
        response = self.client.get(
            reverse("files:api_list"),
            {"parent_id": str(self.folder.uid), "page": 1, "limit": 20},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["limit"], 20)
        self.assertEqual(payload["total"], 1)
        self.assertFalse(payload["has_next"])
        self.assertEqual(payload["files"][0]["uid"], str(self.document.uid))
        self.assertEqual(payload["files"][0]["parent_uid"], str(self.folder.uid))

    def test_detail_includes_summary_and_tags(self):
        response = self.client.get(
            reverse("files:api_detail", kwargs={"uid": self.document.uid})
        )

        self.assertEqual(response.status_code, 200)
        document = response.json()["file"]
        self.assertEqual(document["summary"], "전략 요약")
        self.assertEqual(document["auto_tags"], ["전략", "운영"])

    def test_detail_does_not_expose_another_owners_document(self):
        hidden = Node.objects.create(
            owner=self.other_user,
            name="private.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )

        response = self.client.get(
            reverse("files:api_detail", kwargs={"uid": hidden.uid})
        )

        self.assertEqual(response.status_code, 404)
