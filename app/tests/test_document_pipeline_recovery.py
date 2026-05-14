from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from config.enums import AIStatus, NodeType
from document_ai.models import ChunkEmbedding, DocumentChunk, DocumentParseResult
from document_ai.tasks import (
    _get_embedding_recovery_chunk_ids,
    _get_parse_recovery_node_ids,
    recover_document_pipeline_backlog,
)
from files.models import FileBlob, Node

pytestmark = pytest.mark.integration

User = get_user_model()


class DocumentPipelineRecoveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="recovery@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )

    def _create_file_node(self, name: str) -> Node:
        node = Node.objects.create(
            owner=self.user,
            name=name,
            ext=".txt",
            node_type=NodeType.FILE,
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

    def test_parse_recovery_identifies_missing_stale_and_chunking_gaps(self):
        missing_parse_node = self._create_file_node("missing.txt")

        stale_failed_node = self._create_file_node("failed.txt")
        stale_failed_parse = DocumentParseResult.objects.create(
            node=stale_failed_node,
            status=AIStatus.FAILED,
            chunk_count=0,
        )
        DocumentParseResult.objects.filter(pk=stale_failed_parse.pk).update(
            updated_at=timezone.now() - timedelta(hours=1)
        )

        chunk_gap_node = self._create_file_node("gap.txt")
        DocumentParseResult.objects.create(
            node=chunk_gap_node,
            status=AIStatus.COMPLETED,
            chunk_count=2,
        )
        DocumentChunk.objects.create(
            parse_result=chunk_gap_node.parse_result,
            chunk_index=0,
            text="one",
            status=AIStatus.COMPLETED,
        )

        healthy_node = self._create_file_node("healthy.txt")
        healthy_parse = DocumentParseResult.objects.create(
            node=healthy_node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
        )
        DocumentChunk.objects.create(
            parse_result=healthy_parse,
            chunk_index=0,
            text="ok",
            status=AIStatus.COMPLETED,
        )

        recent_pending_node = self._create_file_node("recent.txt")
        DocumentParseResult.objects.create(
            node=recent_pending_node,
            status=AIStatus.PENDING,
            chunk_count=0,
        )

        node_ids = _get_parse_recovery_node_ids(limit=1000)

        self.assertIn(missing_parse_node.id, node_ids)
        self.assertIn(stale_failed_node.id, node_ids)
        self.assertIn(chunk_gap_node.id, node_ids)
        self.assertNotIn(healthy_node.id, node_ids)
        self.assertNotIn(recent_pending_node.id, node_ids)

    def test_embedding_recovery_identifies_stale_incomplete_chunks(self):
        parse_node = self._create_file_node("embed.txt")
        parse_result = DocumentParseResult.objects.create(
            node=parse_node,
            status=AIStatus.COMPLETED,
            chunk_count=3,
            metadata={"embedding_backend": "bgem3_hybrid"},
        )

        stale_pending = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="pending chunk",
            status=AIStatus.PENDING,
        )
        stale_failed = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=1,
            text="failed chunk",
            status=AIStatus.FAILED,
        )
        completed_chunk = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=2,
            text="done chunk",
            status=AIStatus.COMPLETED,
        )
        ChunkEmbedding.objects.create(
            chunk=completed_chunk,
            model_name="BAAI/bge-m3",
            model_version="bgem3_hybrid",
            sparse_vector={"1": 1.0},
            status=AIStatus.COMPLETED,
        )

        old_time = timezone.now() - timedelta(hours=1)
        DocumentChunk.objects.filter(id__in=[stale_pending.id, stale_failed.id]).update(created_at=old_time)

        chunk_ids = _get_embedding_recovery_chunk_ids(limit=1000)

        self.assertIn(stale_pending.id, chunk_ids)
        self.assertIn(stale_failed.id, chunk_ids)
        self.assertNotIn(completed_chunk.id, chunk_ids)

    def test_recovery_task_requeues_parse_and_embedding_work(self):
        parse_node = self._create_file_node("parse-me.txt")

        embed_node = self._create_file_node("embed-me.txt")
        parse_result = DocumentParseResult.objects.create(
            node=embed_node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
            metadata={"embedding_backend": "bgem3_hybrid"},
        )
        failed_chunk = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="retry chunk",
            status=AIStatus.FAILED,
            error_message={"message": "boom"},
        )
        DocumentChunk.objects.filter(pk=failed_chunk.pk).update(created_at=timezone.now() - timedelta(hours=1))

        with patch("document_ai.tasks._get_parse_recovery_node_ids", return_value=[parse_node.id]), patch(
            "document_ai.tasks._get_embedding_recovery_chunk_ids",
            return_value=[failed_chunk.id],
        ), patch("document_ai.tasks.parse_document_with_docling.delay") as parse_delay, patch(
            "document_ai.tasks.embedding_document_with_bge.apply_async"
        ) as embed_apply_async:
            result = recover_document_pipeline_backlog()

        failed_chunk.refresh_from_db()

        parse_delay.assert_called_once_with(parse_node.id)
        embed_apply_async.assert_called_once_with(args=[failed_chunk.id], queue="embed")
        self.assertEqual(failed_chunk.status, AIStatus.PENDING)
        self.assertEqual(failed_chunk.error_message, {})
        self.assertEqual(result["parse_requeued"], 1)
        self.assertEqual(result["embedding_requeued"], 1)
