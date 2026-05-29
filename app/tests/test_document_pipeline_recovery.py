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
    embedding_document_with_bge,
    enqueue_embedding_tasks,
    _get_embedding_recovery_chunk_ids,
    _get_node_ids_for_chunks,
    _get_parse_recovery_node_ids,
    _try_acquire_recovery_lock,
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

    def test_enqueue_embedding_tasks_skips_ai_disabled_node(self):
        node = self._create_file_node("ai-disabled-queue.txt")
        node.ai_processing_enabled = False
        node.save(update_fields=["ai_processing_enabled"])
        parse_result = DocumentParseResult.objects.create(
            node=node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
        )
        chunk = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="pending chunk",
            status=AIStatus.PENDING,
        )

        with patch("document_ai.tasks.embedding_document_with_bge.apply_async") as apply_async:
            result = enqueue_embedding_tasks(node.id)

        chunk.refresh_from_db()
        apply_async.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(chunk.status, AIStatus.PENDING)

    def test_embedding_task_skips_ai_disabled_node_before_model_call(self):
        node = self._create_file_node("ai-disabled-embed.txt")
        node.ai_processing_enabled = False
        node.save(update_fields=["ai_processing_enabled"])
        parse_result = DocumentParseResult.objects.create(
            node=node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
        )
        chunk = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="processing chunk",
            status=AIStatus.PROCESSING,
        )

        with patch("document_ai.embedding.embeding_models.embed_document") as embed_document:
            result = embedding_document_with_bge.run(chunk.id)

        chunk.refresh_from_db()
        embed_document.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(chunk.status, AIStatus.PENDING)

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
            "document_ai.tasks.enqueue_embedding_tasks.delay"
        ) as enqueue_delay, patch("document_ai.tasks._redis_client") as mock_redis_ctor:
            # Redis 락 항상 성공 (중복 없음)
            mock_redis_ctor.return_value.set.return_value = True
            result = recover_document_pipeline_backlog()

        failed_chunk.refresh_from_db()

        parse_delay.assert_called_once_with(parse_node.id)
        # 복구 흐름: chunk → node 단위로 묶어 enqueue_embedding_tasks 경유
        # enqueue_embedding_tasks 가 PENDING→PROCESSING 전환 후 임베딩 큐잉을 담당함
        enqueue_delay.assert_called_once_with(embed_node.id)
        self.assertEqual(failed_chunk.status, AIStatus.PENDING)
        self.assertEqual(failed_chunk.error_message, {})
        self.assertEqual(result["parse_requeued"], 1)
        self.assertEqual(result["parse_skipped_dedup"], 0)
        self.assertEqual(result["embedding_nodes_requeued"], 1)
        self.assertEqual(result["embedding_nodes_skipped_dedup"], 0)

    def test_get_node_ids_for_chunks_returns_distinct_node_ids(self):
        """_get_node_ids_for_chunks는 여러 첩크가 같은 node에 속해도 node_id를 중복 없이 반환합니다."""
        node = self._create_file_node("multi-chunk.txt")
        parse_result = DocumentParseResult.objects.create(
            node=node,
            status=AIStatus.COMPLETED,
            chunk_count=2,
        )
        chunk_a = DocumentChunk.objects.create(
            parse_result=parse_result, chunk_index=0, text="a", status=AIStatus.FAILED
        )
        chunk_b = DocumentChunk.objects.create(
            parse_result=parse_result, chunk_index=1, text="b", status=AIStatus.FAILED
        )

        node_ids = _get_node_ids_for_chunks([chunk_a.id, chunk_b.id])

        self.assertEqual(node_ids, [node.id])

    def test_get_node_ids_for_chunks_empty_input(self):
        node_ids = _get_node_ids_for_chunks([])
        self.assertEqual(node_ids, [])

    def test_recovery_task_skips_nodes_when_redis_lock_already_held(self):
        """Redis 락이 이미 존재하면 해당 node 에 대한 재큐잉을 건너뜁니다."""
        parse_node = self._create_file_node("dedup-parse.txt")

        embed_node = self._create_file_node("dedup-embed.txt")
        parse_result = DocumentParseResult.objects.create(
            node=embed_node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
            metadata={"embedding_backend": "bgem3_hybrid"},
        )
        stale_chunk = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="dedup chunk",
            status=AIStatus.FAILED,
            error_message={"message": "boom"},
        )
        DocumentChunk.objects.filter(pk=stale_chunk.pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )

        with patch("document_ai.tasks._get_parse_recovery_node_ids", return_value=[parse_node.id]), patch(
            "document_ai.tasks._get_embedding_recovery_chunk_ids",
            return_value=[stale_chunk.id],
        ), patch("document_ai.tasks.parse_document_with_docling.delay") as parse_delay, patch(
            "document_ai.tasks.enqueue_embedding_tasks.delay"
        ) as enqueue_delay, patch("document_ai.tasks._redis_client") as mock_redis_ctor:
            # Redis SET NX 실패 → 락이 이미 존재 (중복 큐잉 방지)
            mock_redis_ctor.return_value.set.return_value = None
            result = recover_document_pipeline_backlog()

        parse_delay.assert_not_called()
        enqueue_delay.assert_not_called()
        self.assertEqual(result["parse_requeued"], 0)
        self.assertEqual(result["parse_skipped_dedup"], 1)
        self.assertEqual(result["embedding_nodes_requeued"], 0)
        self.assertEqual(result["embedding_nodes_skipped_dedup"], 1)

    def test_recovery_task_proceeds_without_dedup_when_redis_unavailable(self):
        """Redis 연결 실패 시 dedup 없이 복구 작업을 정상 수행합니다."""
        parse_node = self._create_file_node("redis-down-parse.txt")

        embed_node = self._create_file_node("redis-down-embed.txt")
        parse_result = DocumentParseResult.objects.create(
            node=embed_node,
            status=AIStatus.COMPLETED,
            chunk_count=1,
            metadata={"embedding_backend": "bgem3_hybrid"},
        )
        stale_chunk = DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="redis down chunk",
            status=AIStatus.FAILED,
            error_message={"message": "boom"},
        )
        DocumentChunk.objects.filter(pk=stale_chunk.pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )

        with patch("document_ai.tasks._get_parse_recovery_node_ids", return_value=[parse_node.id]), patch(
            "document_ai.tasks._get_embedding_recovery_chunk_ids",
            return_value=[stale_chunk.id],
        ), patch("document_ai.tasks.parse_document_with_docling.delay") as parse_delay, patch(
            "document_ai.tasks.enqueue_embedding_tasks.delay"
        ) as enqueue_delay, patch(
            "document_ai.tasks._redis_client",
            side_effect=Exception("connection refused"),
        ):
            result = recover_document_pipeline_backlog()

        # Redis 없이도 정상 큐잉
        parse_delay.assert_called_once_with(parse_node.id)
        enqueue_delay.assert_called_once_with(embed_node.id)
        self.assertEqual(result["parse_requeued"], 1)
        self.assertEqual(result["embedding_nodes_requeued"], 1)
