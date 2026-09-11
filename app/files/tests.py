from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from unittest.mock import patch

from accounts.models import APIToken
from config.enums import AIStatus, FileOperation, NodeType
from document_ai.models import (
    ChunkEmbedding,
    DocumentChunk,
    DocumentParseResult,
    EmbeddingGeneration,
)
from files.models import FileBlob, FileOperationLog, Node
from files.services import file_service
from files.services import storage as storage_service

pytestmark = pytest.mark.unit

User = get_user_model()

TEST_ACTIVE_GENERATION_ID = "test-active-embedding-generation"


def active_embedding_runtime():
    return SimpleNamespace(
        generation_id=TEST_ACTIVE_GENERATION_ID,
        runtime_fingerprint="test-active-runtime-fingerprint",
    )


def create_chunk_embedding(
    chunk,
    *,
    generation_id=TEST_ACTIVE_GENERATION_ID,
    status=AIStatus.COMPLETED,
):
    generation, _ = EmbeddingGeneration.objects.get_or_create(
        generation_id=generation_id,
        defaults={
            "scope": "production",
            "runtime_fingerprint": f"fingerprint-{generation_id}",
            "model_id": "BAAI/bge-m3",
            "model_revision": "test-revision",
            "provider": "bgem3_hybrid",
            "store": "pgvector_chunk_1024",
            "dimension": 1024,
            "supports_sparse": True,
            "status": "ACTIVE" if generation_id == TEST_ACTIVE_GENERATION_ID else "RETIRED",
        },
    )
    chunk.parse_result.embedding_generation_id = generation_id
    chunk.parse_result.embedding_runtime_fingerprint = (
        "test-active-runtime-fingerprint"
        if generation_id == TEST_ACTIVE_GENERATION_ID
        else f"fingerprint-{generation_id}"
    )
    chunk.parse_result.save(update_fields=[
        "embedding_generation_id",
        "embedding_runtime_fingerprint",
    ])
    return ChunkEmbedding.objects.create(
        chunk=chunk,
        status=status,
    )


class NodeModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="test@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.workspace = self.user.workspace_memberships.get(workspace__kind="personal").workspace

    def test_build_path(self):
        root = Node.objects.create(owner=self.user, workspace=self.workspace, name="root", ext="", node_type=NodeType.FOLDER)
        parent = Node.objects.create(owner=self.user, workspace=self.workspace, name="parent", ext="", node_type=NodeType.FOLDER, parent=root)
        file_node = Node.objects.create(owner=self.user, workspace=self.workspace, name="file.txt", ext=".txt", node_type=NodeType.FILE, parent=parent)

        self.assertEqual(root.path, "/root")
        self.assertEqual(parent.path, "/root/parent")
        self.assertEqual(file_node.path, "/root/parent/file.txt")

    def test_move_folder_updates_child_paths(self):
        root = Node.objects.create(owner=self.user, workspace=self.workspace, name="root", ext="", node_type=NodeType.FOLDER)
        parent = Node.objects.create(owner=self.user, workspace=self.workspace, name="parent", ext="", node_type=NodeType.FOLDER, parent=root)
        folder = Node.objects.create(owner=self.user, workspace=self.workspace, name="folder", ext="", node_type=NodeType.FOLDER, parent=root)
        child = Node.objects.create(owner=self.user, workspace=self.workspace, name="child.txt", ext=".txt", node_type=NodeType.FILE, parent=folder)

        folder.move(new_parent=parent)

        folder.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(folder.path, "/root/parent/folder")
        self.assertEqual(child.path, "/root/parent/folder/child.txt")

    def test_move_folder_to_descendant_raises_error(self):
        root = Node.objects.create(owner=self.user, workspace=self.workspace, name="root", ext="", node_type=NodeType.FOLDER)
        child = Node.objects.create(owner=self.user, workspace=self.workspace, name="child", ext="", node_type=NodeType.FOLDER, parent=root)

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
        self.workspace = self.user.workspace_memberships.get(workspace__kind="personal").workspace
        self.root = Node.objects.create(owner=self.user, workspace=self.workspace, name="root", ext="", node_type=NodeType.FOLDER)
        self.file_node = Node.objects.create(
            owner=self.user, workspace=self.workspace,
            name="doc.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            parent=self.root,
        )

    def test_create_folder_sets_empty_extension(self):
        folder = file_service.create_folder(self.workspace, self.user, "reports", parent=self.root)

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
        folder = Node.objects.create(owner=self.user, workspace=self.workspace, name="folder", ext="", node_type=NodeType.FOLDER, parent=self.root)
        child = Node.objects.create(owner=self.user, workspace=self.workspace, name="child.txt", ext=".txt", node_type=NodeType.FILE, parent=folder)

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
            owner=self.user, workspace=self.workspace,
            name="expired.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            trashed=True,
            deleted_at=timezone.now() - timedelta(days=8),
        )
        active = Node.objects.create(
            owner=self.user, workspace=self.workspace,
            name="active.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            trashed=True,
            deleted_at=timezone.now() - timedelta(days=2),
        )

        trashed_files = list(file_service.get_trashed_files(self.workspace))

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
        self.workspace = self.user.workspace_memberships.get(workspace__kind="personal").workspace
        self.file_node = Node.objects.create(
            owner=self.user, workspace=self.workspace,
            name="notes.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )
        with patch("document_ai.signals.enqueue_parse"):
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
        completed_chunk = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="first",
            status=AIStatus.COMPLETED,
        )
        create_chunk_embedding(completed_chunk)
        DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=1,
            text="second",
            status=AIStatus.PROCESSING,
        )

        with patch(
            "document_ai.services.embedding_runtime_config.get_active_embedding_runtime",
            side_effect=active_embedding_runtime,
        ):
            ai_status = self.file_node.get_ai_status()
            status_display = self.file_node.get_status_display

        self.assertEqual(ai_status["parse_status"], AIStatus.COMPLETED)
        self.assertEqual(ai_status["embedding_status"], AIStatus.PROCESSING)
        self.assertFalse(ai_status["embedding_completed"])
        self.assertEqual(ai_status["completed_chunks"], 1)
        self.assertEqual(ai_status["processing_chunks"], 1)
        self.assertEqual(status_display, "Ready (Embedding in progress)")

    def test_to_dict_exposes_embedding_completion_summary(self):
        parse_result = DocumentParseResult.objects.create(
            node=self.file_node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
            metadata={"embedding_backend": "bgem3_hybrid"},
        )
        chunk = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="done",
            status=AIStatus.COMPLETED,
        )
        create_chunk_embedding(chunk)

        with patch(
            "document_ai.services.embedding_runtime_config.get_active_embedding_runtime",
            side_effect=active_embedding_runtime,
        ):
            payload = self.file_node.to_dict()

        self.assertIn("ai_status", payload)
        self.assertTrue(payload["ai_status"]["embedding_completed"])
        self.assertEqual(payload["ai_status"]["embedding_status"], AIStatus.COMPLETED)
        self.assertEqual(payload["ai_status"]["embedding_contract_status"], "current")
        self.assertTrue(payload["ai_status"]["searchable"])

    def test_ai_status_marks_an_old_generation_as_stale(self):
        parse_result = DocumentParseResult.objects.create(
            node=self.file_node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
        )
        chunk = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="old embedding",
            status=AIStatus.COMPLETED,
        )
        create_chunk_embedding(chunk, generation_id="retired-generation")

        with patch(
            "document_ai.services.embedding_runtime_config.get_active_embedding_runtime",
            side_effect=active_embedding_runtime,
        ):
            status = self.file_node.get_ai_status()

        self.assertEqual(status["embedding_contract_status"], "stale_contract")
        self.assertEqual(status["embedding_status"], AIStatus.FAILED)
        self.assertEqual(status["completed_chunks"], 0)
        self.assertEqual(status["embedded_generation_ids"], ["retired-generation"])
        self.assertTrue(status["reembedding_required"])
        self.assertFalse(status["searchable"])


class StorageServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="storage@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.workspace = self.user.workspace_memberships.get(workspace__kind="personal").workspace
        upload = SimpleUploadedFile("report.txt", b"hello storage", content_type="text/plain")
        with patch("document_ai.signals.enqueue_parse"):
            self.node = storage_service.save_file(
                workspace=self.workspace,
                owner=self.user,
                file=upload,
                description="storage test",
            )

    def test_get_file_returns_file_node(self):
        resolved = storage_service.get_file(self.node)
        self.assertEqual(resolved.id, self.node.id)

    def test_get_files_returns_only_active_file_nodes(self):
        folder = Node.objects.create(owner=self.user, workspace=self.workspace, name="folder", ext="", node_type=NodeType.FOLDER)
        Node.objects.create(owner=self.user, workspace=self.workspace, name="old.txt", ext=".txt", node_type=NodeType.FILE, trashed=True)

        files = list(storage_service.get_files(self.workspace))

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

    def test_save_file_auto_renames_on_name_collision(self):
        upload = SimpleUploadedFile("report.txt", b"a different body", content_type="text/plain")
        with patch("document_ai.signals.enqueue_parse"):
            second_node = storage_service.save_file(
                workspace=self.workspace,
                owner=self.user,
                file=upload,
                description="second upload",
            )

        self.assertEqual(second_node.name, "report (1).txt")
        self.assertEqual(second_node.blob.original_name, "report.txt")

    def test_save_file_increments_past_multiple_collisions(self):
        with patch("document_ai.signals.enqueue_parse"):
            third_node = storage_service.save_file(
                workspace=self.workspace,
                owner=self.user,
                file=SimpleUploadedFile("report.txt", b"body two", content_type="text/plain"),
                description="second upload",
            )
            fourth_node = storage_service.save_file(
                workspace=self.workspace,
                owner=self.user,
                file=SimpleUploadedFile("report.txt", b"body three", content_type="text/plain"),
                description="third upload",
            )

        self.assertEqual(third_node.name, "report (1).txt")
        self.assertEqual(fourth_node.name, "report (2).txt")


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
class SyncApiUploadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="sync-api@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.workspace = self.user.workspace_memberships.get(workspace__kind="personal").workspace
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

        with patch("document_ai.signals.enqueue_parse") as delay:
            response = self.post_upload(upload, {"ai_processing_enabled": "0"})

        self.assertEqual(response.status_code, 200)
        node = Node.objects.get(uid=response.json()["node_uid"])
        self.assertFalse(node.ai_processing_enabled)
        delay.assert_not_called()

    def test_sync_update_preserves_existing_ai_setting_when_omitted(self):
        sync_root = Node.objects.create(owner=self.user, workspace=self.workspace, name="sync", ext="", node_type=NodeType.FOLDER)
        sync_folder = Node.objects.create(owner=self.user, workspace=self.workspace, name="local", ext="", node_type=NodeType.FOLDER, parent=sync_root)
        parent = Node.objects.create(owner=self.user, workspace=self.workspace, name="docs", ext="", node_type=NodeType.FOLDER, parent=sync_folder)
        node = Node.objects.create(
            owner=self.user, workspace=self.workspace,
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

    def test_sync_update_disables_ai_before_new_blob_signal(self):
        sync_root = Node.objects.create(owner=self.user, workspace=self.workspace, name="sync", ext="", node_type=NodeType.FOLDER)
        sync_folder = Node.objects.create(owner=self.user, workspace=self.workspace, name="local", ext="", node_type=NodeType.FOLDER, parent=sync_root)
        parent = Node.objects.create(owner=self.user, workspace=self.workspace, name="docs", ext="", node_type=NodeType.FOLDER, parent=sync_folder)
        node = Node.objects.create(
            owner=self.user, workspace=self.workspace,
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
        node.ai_processing_enabled = True
        node.save(update_fields=["ai_processing_enabled", "updated_at"])

        with patch("document_ai.signals.enqueue_parse") as delay:
            response = self.post_upload(
                SimpleUploadedFile("report.txt", b"new", content_type="text/plain"),
                {"ai_processing_enabled": "0"},
            )

        self.assertEqual(response.status_code, 200)
        node.refresh_from_db()
        self.assertFalse(node.ai_processing_enabled)
        self.assertEqual(
            node.blob.sha256,
            "11507a0e2f5e69d5dfa40a62a1bd7b6ee57e6bcd85c67c9b8431b36fff21c437",
        )
        delay.assert_not_called()


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
class FileOperationLogInstrumentationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="upload-metrics@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.workspace = self.user.workspace_memberships.get(workspace__kind="personal").workspace
        self.client.force_login(self.user)

    def _last_log(self, operation):
        return FileOperationLog.objects.filter(owner=self.user, operation=operation).latest("created_at")

    def test_successful_upload_records_completed_log_with_timing(self):
        upload = SimpleUploadedFile("report.txt", b"hello", content_type="text/plain")
        with patch("document_ai.signals.enqueue_parse"):
            response = self.client.post("/files/api/v1/upload/", data={"file": upload})

        self.assertEqual(response.status_code, 200)
        log = self._last_log(FileOperation.UPLOAD)
        self.assertEqual(log.status, AIStatus.COMPLETED)
        self.assertEqual(log.detail["original_name"], "report.txt")
        self.assertEqual(log.detail["size_bytes"], len(b"hello"))
        self.assertIsNotNone(log.node)
        self.assertIn("total_ms", log.performance_metrics)
        self.assertGreaterEqual(log.performance_metrics["total_ms"], 0)

    def test_uploading_same_name_twice_auto_renames_second_file(self):
        with patch("document_ai.signals.enqueue_parse"):
            first = self.client.post("/files/api/v1/upload/", data={"file": SimpleUploadedFile("report.txt", b"first", content_type="text/plain")})
            second = self.client.post("/files/api/v1/upload/", data={"file": SimpleUploadedFile("report.txt", b"second", content_type="text/plain")})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["file"]["name"], "report.txt")
        self.assertEqual(second.json()["file"]["name"], "report (1).txt")

    def test_missing_file_records_failed_upload_log(self):
        response = self.client.post("/files/api/v1/upload/", data={})

        self.assertEqual(response.status_code, 400)
        log = self._last_log(FileOperation.UPLOAD)
        self.assertEqual(log.status, AIStatus.FAILED)
        self.assertEqual(log.error_message, "No file provided.")
        self.assertIsNone(log.node)

    def test_validation_failure_records_failed_upload_log_with_reason(self):
        upload = SimpleUploadedFile("virus.exe", b"x", content_type="application/octet-stream")
        response = self.client.post("/files/api/v1/upload/", data={"file": upload})

        self.assertEqual(response.status_code, 400)
        log = self._last_log(FileOperation.UPLOAD)
        self.assertEqual(log.status, AIStatus.FAILED)
        self.assertIn("not supported", log.error_message)
        self.assertIsNone(log.node)

    def test_rename_records_completed_log_with_old_and_new_name(self):
        node = Node.objects.create(owner=self.user, workspace=self.workspace, name="old.txt", ext=".txt", node_type=NodeType.FILE)

        response = self.client.post(f"/files/api/v1/{node.uid}/rename/", data=json.dumps({"name": "new.txt"}), content_type="application/json")

        self.assertEqual(response.status_code, 200)
        log = self._last_log(FileOperation.RENAME)
        self.assertEqual(log.status, AIStatus.COMPLETED)
        self.assertEqual(log.node_id, node.id)
        self.assertEqual(log.detail, {"old_name": "old.txt", "new_name": "new.txt"})

    def test_move_records_completed_log_with_target_parent(self):
        folder = Node.objects.create(owner=self.user, workspace=self.workspace, name="folder", ext="", node_type=NodeType.FOLDER)
        node = Node.objects.create(owner=self.user, workspace=self.workspace, name="file.txt", ext=".txt", node_type=NodeType.FILE)

        response = self.client.post(f"/files/api/v1/{node.uid}/move/", data=json.dumps({"parent_id": str(folder.uid)}), content_type="application/json")

        self.assertEqual(response.status_code, 200)
        log = self._last_log(FileOperation.MOVE)
        self.assertEqual(log.status, AIStatus.COMPLETED)
        self.assertEqual(log.node_id, node.id)
        self.assertEqual(log.detail["target_parent_uid"], str(folder.uid))

    def test_single_delete_records_completed_log(self):
        node = Node.objects.create(owner=self.user, workspace=self.workspace, name="file.txt", ext=".txt", node_type=NodeType.FILE)

        response = self.client.post(f"/files/api/v1/{node.uid}/delete/")

        self.assertEqual(response.status_code, 200)
        log = self._last_log(FileOperation.DELETE)
        self.assertEqual(log.status, AIStatus.COMPLETED)
        self.assertEqual(log.node_id, node.id)

    def test_bulk_delete_records_one_log_with_count(self):
        nodes = [
            Node.objects.create(owner=self.user, workspace=self.workspace, name=f"file{i}.txt", ext=".txt", node_type=NodeType.FILE)
            for i in range(3)
        ]

        response = self.client.post(
            "/files/api/v1/bulk/delete/",
            data=json.dumps({"uids": [str(n.uid) for n in nodes]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        log = self._last_log(FileOperation.DELETE)
        self.assertEqual(log.status, AIStatus.COMPLETED)
        self.assertIsNone(log.node)
        self.assertEqual(log.detail["count"], 3)

    def test_permanent_delete_records_log_with_original_name(self):
        node = Node.objects.create(
            owner=self.user, workspace=self.workspace, name="gone.txt", ext=".txt", node_type=NodeType.FILE, trashed=True,
        )

        response = self.client.post(f"/files/api/v1/{node.uid}/permanent_delete/")

        self.assertEqual(response.status_code, 200)
        log = self._last_log(FileOperation.PERMANENT_DELETE)
        self.assertEqual(log.status, AIStatus.COMPLETED)
        self.assertEqual(log.detail["original_name"], "gone.txt")


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
class FileBulkApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="bulk-api@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.workspace = self.user.workspace_memberships.get(workspace__kind="personal").workspace
        self.client.force_login(self.user)
        self.folder = Node.objects.create(owner=self.user, workspace=self.workspace, name="folder", ext="", node_type=NodeType.FOLDER)
        self.target = Node.objects.create(owner=self.user, workspace=self.workspace, name="target", ext="", node_type=NodeType.FOLDER)
        self.file_node = Node.objects.create(
            owner=self.user, workspace=self.workspace,
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
            owner=self.user, workspace=self.workspace,
            name=name,
            ext=".txt",
            node_type=NodeType.FILE,
            parent=self.folder,
        )
        with patch("document_ai.signals.enqueue_parse"):
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
        ready_chunk_one = DocumentChunk.objects.create(
            parse_result=ready_parse,
            chunk_index=0,
            text="ready one",
            status=AIStatus.COMPLETED,
        )
        ready_chunk_two = DocumentChunk.objects.create(
            parse_result=ready_parse,
            chunk_index=1,
            text="ready two",
            status=AIStatus.COMPLETED,
        )
        create_chunk_embedding(ready_chunk_one)
        create_chunk_embedding(ready_chunk_two)

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

        with patch(
            "files.api_v1.file_views.get_active_embedding_runtime",
            side_effect=active_embedding_runtime,
        ):
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
        self.assertEqual(payload["embedding"]["stale"], 0)
        self.assertEqual(
            payload["active_embedding_generation_id"],
            TEST_ACTIVE_GENERATION_ID,
        )

    def test_ai_readiness_and_retry_expose_stale_contract(self):
        stale_node = self.create_file_with_blob("stale.txt")
        parse_result = DocumentParseResult.objects.create(
            node=stale_node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
        )
        chunk = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="stale",
            status=AIStatus.COMPLETED,
        )
        create_chunk_embedding(chunk, generation_id="retired-generation")

        with patch(
            "files.api_v1.file_views.get_active_embedding_runtime",
            side_effect=active_embedding_runtime,
        ):
            readiness = self.client.get("/files/api/v1/ai/readiness/")

        self.assertEqual(readiness.status_code, 200)
        self.assertEqual(readiness.json()["embedding"]["stale"], 1)
        self.assertEqual(readiness.json()["searchable_files"], 0)

        with patch(
            "document_ai.services.embedding_runtime_config.get_active_embedding_runtime",
            side_effect=active_embedding_runtime,
        ), patch("files.api_v1.file_views.enqueue_embedding") as enqueue:
            response = self.client.post(
                f"/files/api/v1/{stale_node.uid}/ai/retry/"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["action"], "embedding_requeued")
        chunk.refresh_from_db()
        self.assertEqual(chunk.status, AIStatus.PENDING)
        enqueue.assert_called_once()

    def test_operation_status_counts_embedding_contract_mismatches(self):
        stale_node = self.create_file_with_blob("operator-stale.txt")
        parse_result = DocumentParseResult.objects.create(
            node=stale_node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
        )
        chunk = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="operator stale",
            status=AIStatus.COMPLETED,
        )
        create_chunk_embedding(chunk, generation_id="retired-generation")

        from document_ai.services.operation_status import _processing_status

        with patch(
            "document_ai.services.operation_status.get_active_embedding_runtime",
            side_effect=active_embedding_runtime,
        ):
            status = _processing_status()

        self.assertEqual(status["embedding"]["contract_mismatch_count"], 1)
        self.assertEqual(
            status["embedding"]["active_generation_id"],
            TEST_ACTIVE_GENERATION_ID,
        )

    def test_rag_scope_nodes_returns_files_and_folders(self):
        file_node = self.create_file_with_blob("scope-doc.txt")
        folder = Node.objects.create(
            owner=self.user, workspace=self.workspace,
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
        self.assertEqual(
            set(returned[str(folder.uid)].keys()),
            {"uid", "name", "path", "node_type", "depth", "ext", "file_count"},
        )
        returned_uids = [item["uid"] for item in payload["nodes"]]
        self.assertLess(returned_uids.index(str(folder.uid)), returned_uids.index(str(child_file.uid)))

    @patch("files.api_v1.file_views.enqueue_parse")
    def test_retry_ai_processing_requeues_failed_parse(self, delay_mock):
        DocumentParseResult.objects.create(
            node=self.file_node,
            status=AIStatus.FAILED,
            errors=[{"message": "parse failed"}],
        )

        response = self.post_json(f"/files/api/v1/{self.file_node.uid}/ai/retry/", {})

        self.assertEqual(response.status_code, 200)
        delay_mock.assert_called_once()
        self.assertEqual(delay_mock.call_args.args, (self.file_node.id,))
        self.file_node.parse_result.refresh_from_db()
        self.assertEqual(self.file_node.parse_result.status, AIStatus.PENDING)

    @patch("files.api_v1.file_views.enqueue_embedding")
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
        delay_mock.assert_called_once()
        self.assertEqual(delay_mock.call_args.args, (self.file_node.id,))
        chunk.refresh_from_db()
        self.assertEqual(chunk.status, AIStatus.PENDING)
        self.assertEqual(chunk.error_message, {})
