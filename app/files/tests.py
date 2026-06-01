from datetime import timedelta
import json

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from unittest.mock import patch

from accounts.models import APIToken
from config.enums import AIStatus, NodeType
from document_ai.models import DocumentChunk, DocumentParseResult
from files.models import FileBlob, Node
from files.services import file_service
from files.services import storage as storage_service

User = get_user_model()


class NodeModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="test@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )

    def test_build_path(self):
        root = Node.objects.create(owner=self.user, name="root", ext="", node_type=NodeType.FOLDER)
        parent = Node.objects.create(owner=self.user, name="parent", ext="", node_type=NodeType.FOLDER, parent=root)
        file_node = Node.objects.create(owner=self.user, name="file.txt", ext=".txt", node_type=NodeType.FILE, parent=parent)

        self.assertEqual(root.path, "/root")
        self.assertEqual(parent.path, "/root/parent")
        self.assertEqual(file_node.path, "/root/parent/file.txt")

    def test_move_folder_updates_child_paths(self):
        root = Node.objects.create(owner=self.user, name="root", ext="", node_type=NodeType.FOLDER)
        parent = Node.objects.create(owner=self.user, name="parent", ext="", node_type=NodeType.FOLDER, parent=root)
        folder = Node.objects.create(owner=self.user, name="folder", ext="", node_type=NodeType.FOLDER, parent=root)
        child = Node.objects.create(owner=self.user, name="child.txt", ext=".txt", node_type=NodeType.FILE, parent=folder)

        folder.move(new_parent=parent)

        folder.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(folder.path, "/root/parent/folder")
        self.assertEqual(child.path, "/root/parent/folder/child.txt")

    def test_move_folder_to_descendant_raises_error(self):
        root = Node.objects.create(owner=self.user, name="root", ext="", node_type=NodeType.FOLDER)
        child = Node.objects.create(owner=self.user, name="child", ext="", node_type=NodeType.FOLDER, parent=root)

        with self.assertRaises(ValueError):
            root.move(new_parent=child)


class FileServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="test2@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.root = Node.objects.create(owner=self.user, name="root", ext="", node_type=NodeType.FOLDER)
        self.file_node = Node.objects.create(
            owner=self.user,
            name="doc.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            parent=self.root,
        )

    def test_create_folder_sets_empty_extension(self):
        folder = file_service.create_folder(self.user, "reports", parent=self.root)

        self.assertEqual(folder.ext, "")
        self.assertEqual(folder.node_type, NodeType.FOLDER)

    def test_move_to_trash_sets_deleted_at(self):
        file_service.move_to_trash(self.file_node)
        self.file_node.refresh_from_db()

        self.assertTrue(self.file_node.trashed)
        self.assertFalse(self.file_node.ai_processing_enabled)
        self.assertIsNotNone(self.file_node.deleted_at)

    def test_restore_clears_deleted_at(self):
        file_service.move_to_trash(self.file_node)
        file_service.restore_file(self.file_node)
        self.file_node.refresh_from_db()

        self.assertFalse(self.file_node.trashed)
        self.assertFalse(self.file_node.ai_processing_enabled)
        self.assertIsNone(self.file_node.deleted_at)

    def test_move_to_trash_marks_descendants(self):
        folder = Node.objects.create(owner=self.user, name="folder", ext="", node_type=NodeType.FOLDER, parent=self.root)
        child = Node.objects.create(owner=self.user, name="child.txt", ext=".txt", node_type=NodeType.FILE, parent=folder)

        file_service.move_to_trash(folder)
        folder.refresh_from_db()
        child.refresh_from_db()

        self.assertTrue(folder.trashed)
        self.assertTrue(child.trashed)
        self.assertFalse(folder.ai_processing_enabled)
        self.assertFalse(child.ai_processing_enabled)
        self.assertIsNotNone(folder.deleted_at)
        self.assertEqual(folder.deleted_at, child.deleted_at)

    def test_restore_fails_after_retention_window(self):
        file_service.move_to_trash(self.file_node)
        self.file_node.deleted_at = timezone.now() - timedelta(days=8)
        self.file_node.save(update_fields=["deleted_at"])

        with self.assertRaises(ValueError):
            file_service.restore_file(self.file_node)

        self.assertFalse(Node.objects.filter(pk=self.file_node.pk).exists())

    def test_get_trashed_files_purges_expired_items(self):
        expired = Node.objects.create(
            owner=self.user,
            name="expired.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            trashed=True,
            deleted_at=timezone.now() - timedelta(days=8),
        )
        active = Node.objects.create(
            owner=self.user,
            name="active.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            trashed=True,
            deleted_at=timezone.now() - timedelta(days=2),
        )

        trashed_files = list(file_service.get_trashed_files(self.user))

        self.assertIn(active, trashed_files)
        self.assertNotIn(expired, trashed_files)
        self.assertFalse(Node.objects.filter(pk=expired.pk).exists())


class FileBlobModelTests(TestCase):
    def test_size_mb_handles_none(self):
        blob = FileBlob(size=None)
        self.assertIsNone(blob.size_mb())

    def test_size_mb_rounds_megabytes(self):
        blob = FileBlob(size=2 * 1024 * 1024)
        self.assertEqual(blob.size_mb(), 2.0)


class FileAIStatusTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="ai-status@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.file_node = Node.objects.create(
            owner=self.user,
            name="notes.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )
        with patch("document_ai.signals.parse_document_with_docling.delay"):
            FileBlob.objects.create(
                node=self.file_node,
                original_name="notes.txt",
                file=SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain"),
                mime_type="text/plain",
                size=5,
                status="ready",
            )

    def test_ai_status_distinguishes_parse_and_embedding_progress(self):
        parse_result = DocumentParseResult.objects.create(
            node=self.file_node,
            status=AIStatus.COMPLETED,
            chunk_count=2,
            metadata={"embedding_backend": "bgem3_hybrid"},
        )
        DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="first",
            status=AIStatus.COMPLETED,
        )
        DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=1,
            text="second",
            status=AIStatus.PROCESSING,
        )

        ai_status = self.file_node.get_ai_status()

        self.assertEqual(ai_status["parse_status"], AIStatus.COMPLETED)
        self.assertEqual(ai_status["embedding_status"], AIStatus.PROCESSING)
        self.assertFalse(ai_status["embedding_completed"])
        self.assertEqual(ai_status["completed_chunks"], 1)
        self.assertEqual(ai_status["processing_chunks"], 1)
        self.assertEqual(self.file_node.get_status_display, "Ready (Embedding in progress)")

    def test_to_dict_exposes_embedding_completion_summary(self):
        parse_result = DocumentParseResult.objects.create(
            node=self.file_node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
            metadata={"embedding_backend": "bgem3_hybrid"},
        )
        DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="done",
            status=AIStatus.COMPLETED,
        )

        payload = self.file_node.to_dict()

        self.assertIn("ai_status", payload)
        self.assertTrue(payload["ai_status"]["embedding_completed"])
        self.assertEqual(payload["ai_status"]["embedding_status"], AIStatus.COMPLETED)


class StorageServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="storage@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        upload = SimpleUploadedFile("report.txt", b"hello storage", content_type="text/plain")
        with patch("document_ai.signals.parse_document_with_docling.delay"):
            self.node = storage_service.save_file(
                owner=self.user,
                file=upload,
                description="storage test",
            )

    def test_get_file_returns_file_node(self):
        resolved = storage_service.get_file(self.node)
        self.assertEqual(resolved.id, self.node.id)

    def test_get_files_returns_only_active_file_nodes(self):
        folder = Node.objects.create(owner=self.user, name="folder", ext="", node_type=NodeType.FOLDER)
        Node.objects.create(owner=self.user, name="old.txt", ext=".txt", node_type=NodeType.FILE, trashed=True)

        files = list(storage_service.get_files(self.user))

        self.assertIn(self.node, files)
        self.assertNotIn(folder, files)
        self.assertEqual(sum(1 for item in files if item.node_type == NodeType.FILE), len(files))

    def test_get_download_response_uses_original_filename(self):
        response = storage_service.get_download_response(self.node)
        self.assertEqual(response.filename, "report.txt")

    def test_delete_file_removes_node_and_stored_blob(self):
        file_name = self.node.blob.file.name

        storage_service.delete_file(self.node)

        self.assertFalse(Node.objects.filter(pk=self.node.pk).exists())
        self.assertFalse(default_storage.exists(file_name))


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
class SyncApiUploadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="sync-api@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.token = APIToken.objects.create(user=self.user, name="test token")

    def post_upload(self, file_obj, data=None):
        payload = {
            "file": file_obj,
            "rel_path": "docs/report.txt",
            "folder_name": "local",
            "content_hash": "hash",
        }
        if data:
            payload.update(data)
        return self.client.post(
            "/api/sync/v1/upload/",
            data=payload,
            HTTP_AUTHORIZATION=f"Bearer {self.token.key}",
        )

    def test_sync_upload_can_disable_ai_processing(self):
        upload = SimpleUploadedFile("report.txt", b"sync", content_type="text/plain")

        with patch("document_ai.signals.parse_document_with_docling.delay") as delay:
            response = self.post_upload(upload, {"ai_processing_enabled": "0"})

        self.assertEqual(response.status_code, 200)
        node = Node.objects.get(uid=response.json()["node_uid"])
        self.assertFalse(node.ai_processing_enabled)
        delay.assert_not_called()

    def test_sync_update_preserves_existing_ai_setting_when_omitted(self):
        sync_root = Node.objects.create(owner=self.user, name="sync", ext="", node_type=NodeType.FOLDER)
        sync_folder = Node.objects.create(owner=self.user, name="local", ext="", node_type=NodeType.FOLDER, parent=sync_root)
        parent = Node.objects.create(owner=self.user, name="docs", ext="", node_type=NodeType.FOLDER, parent=sync_folder)
        node = Node.objects.create(
            owner=self.user,
            name="report.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            parent=parent,
            ai_processing_enabled=False,
        )
        FileBlob.objects.create(
            node=node,
            file=SimpleUploadedFile("old.txt", b"old", content_type="text/plain"),
            original_name="report.txt",
            size=3,
            mime_type="text/plain",
            sha256="old",
        )

        response = self.post_upload(SimpleUploadedFile("report.txt", b"new", content_type="text/plain"))

        self.assertEqual(response.status_code, 200)
        node.refresh_from_db()
        self.assertFalse(node.ai_processing_enabled)


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
class FileBulkApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="bulk-api@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.client.force_login(self.user)
        self.folder = Node.objects.create(owner=self.user, name="folder", ext="", node_type=NodeType.FOLDER)
        self.target = Node.objects.create(owner=self.user, name="target", ext="", node_type=NodeType.FOLDER)
        self.file_node = Node.objects.create(
            owner=self.user,
            name="doc.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            parent=self.folder,
        )

    def post_json(self, url, payload):
        return self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def create_file_with_blob(self, name):
        node = Node.objects.create(
            owner=self.user,
            name=name,
            ext=".txt",
            node_type=NodeType.FILE,
            parent=self.folder,
        )
        with patch("document_ai.signals.parse_document_with_docling.delay"):
            FileBlob.objects.create(
                node=node,
                original_name=name,
                file=SimpleUploadedFile(name, b"hello", content_type="text/plain"),
                mime_type="text/plain",
                size=5,
                status="ready",
            )
        return node

    def test_bulk_delete_moves_selected_nodes_to_trash(self):
        response = self.post_json("/files/api/v1/bulk/delete/", {"uids": [str(self.file_node.uid)]})

        self.assertEqual(response.status_code, 200)
        self.file_node.refresh_from_db()
        self.assertTrue(self.file_node.trashed)

    def test_bulk_restore_restores_selected_nodes(self):
        file_service.move_to_trash(self.file_node)

        response = self.post_json("/files/api/v1/bulk/restore/", {"uids": [str(self.file_node.uid)]})

        self.assertEqual(response.status_code, 200)
        self.file_node.refresh_from_db()
        self.assertFalse(self.file_node.trashed)

    def test_bulk_move_moves_selected_nodes(self):
        response = self.post_json(
            "/files/api/v1/bulk/move/",
            {"uids": [str(self.file_node.uid)], "parent_id": str(self.target.uid)},
        )

        self.assertEqual(response.status_code, 200)
        self.file_node.refresh_from_db()
        self.assertEqual(self.file_node.parent_id, self.target.id)

    def test_ai_readiness_summarizes_searchable_and_incomplete_files(self):
        ready_node = self.create_file_with_blob("ready.txt")
        ready_parse = DocumentParseResult.objects.create(
            node=ready_node,
            status=AIStatus.COMPLETED,
            chunk_count=2,
        )
        DocumentChunk.objects.create(
            parse_result=ready_parse,
            chunk_index=0,
            text="ready one",
            status=AIStatus.COMPLETED,
        )
        DocumentChunk.objects.create(
            parse_result=ready_parse,
            chunk_index=1,
            text="ready two",
            status=AIStatus.COMPLETED,
        )

        processing_node = self.create_file_with_blob("processing.txt")
        DocumentParseResult.objects.create(
            node=processing_node,
            status=AIStatus.PROCESSING,
            chunk_count=0,
        )

        failed_node = self.create_file_with_blob("failed.txt")
        DocumentParseResult.objects.create(
            node=failed_node,
            status=AIStatus.FAILED,
            chunk_count=0,
        )

        self.create_file_with_blob("queued.txt")

        response = self.client.get("/files/api/v1/ai/readiness/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total_files"], 4)
        self.assertEqual(payload["searchable_files"], 1)
        self.assertEqual(payload["ready_percent"], 25.0)
        self.assertEqual(payload["parse"]["completed"], 1)
        self.assertEqual(payload["parse"]["processing"], 1)
        self.assertEqual(payload["parse"]["failed"], 1)
        self.assertEqual(payload["parse"]["pending"], 1)
        self.assertEqual(payload["embedding"]["completed"], 1)

    def test_rag_scope_nodes_returns_files_and_folders(self):
        file_node = self.create_file_with_blob("scope-doc.txt")
        folder = Node.objects.create(
            owner=self.user,
            name="scope-folder",
            ext="",
            node_type=NodeType.FOLDER,
            parent=self.folder,
        )
        child_file = self.create_file_with_blob("scope-child.txt")
        child_file.move(new_parent=folder)

        response = self.client.get("/files/api/v1/rag/scope-nodes/?q=scope")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        returned = {item["uid"]: item for item in payload["nodes"]}
        self.assertIn(str(file_node.uid), returned)
        self.assertIn(str(folder.uid), returned)
        self.assertIn(str(child_file.uid), returned)
        self.assertEqual(returned[str(folder.uid)]["node_type"], NodeType.FOLDER)
        self.assertEqual(returned[str(folder.uid)]["file_count"], 1)
        self.assertEqual(returned[str(folder.uid)]["depth"], 1)
        self.assertNotIn("ai_ready", returned[str(folder.uid)])
        returned_uids = [item["uid"] for item in payload["nodes"]]
        self.assertLess(returned_uids.index(str(folder.uid)), returned_uids.index(str(child_file.uid)))

    @patch("files.api_v1.file_views.parse_document_with_docling.delay")
    def test_retry_ai_processing_requeues_failed_parse(self, delay_mock):
        DocumentParseResult.objects.create(
            node=self.file_node,
            status=AIStatus.FAILED,
            errors=[{"message": "parse failed"}],
        )

        response = self.post_json(f"/files/api/v1/{self.file_node.uid}/ai/retry/", {})

        self.assertEqual(response.status_code, 200)
        delay_mock.assert_called_once_with(self.file_node.id)
        self.file_node.parse_result.refresh_from_db()
        self.assertEqual(self.file_node.parse_result.status, AIStatus.PENDING)

    @patch("files.api_v1.file_views.enqueue_embedding_tasks.delay")
    def test_retry_ai_processing_requeues_failed_embedding(self, delay_mock):
        parse_result = DocumentParseResult.objects.create(
            node=self.file_node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
        )
        chunk = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="failed chunk",
            status=AIStatus.FAILED,
            error_message={"message": "embedding failed"},
        )

        response = self.post_json(f"/files/api/v1/{self.file_node.uid}/ai/retry/", {})

        self.assertEqual(response.status_code, 200)
        delay_mock.assert_called_once_with(self.file_node.id)
        chunk.refresh_from_db()
        self.assertEqual(chunk.status, AIStatus.PENDING)
        self.assertEqual(chunk.error_message, {})
