"""
[문서 처리 파이프라인 장애 복구(Recovery) 검증 테스트]

워커 크래시, Redis 재부팅, 일시적 OOM 등으로 인해 파싱(parse) 또는 임베딩(embed)
중간에 멈추거나 누락된 백로그(Backlog)를 주기적 복구 태스크가 찾아내어
안전하게 재처리(Recovery)하는지 검증합니다.
"""

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from celery.exceptions import Retry
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from config.enums import AIStatus, NodeType
from document_ai.models import ChunkEmbedding, DocumentChunk, DocumentParseResult
from document_ai.embedding.executor import (
    EmbeddingDispatchError,
    embed_document_chunks_batch_sync,
    enqueue_embedding_tasks_sync,
)
from document_ai.processing.recovery import (
    get_embedding_recovery_chunk_ids,
    get_node_ids_for_chunks,
    get_parse_recovery_node_ids,
)
from document_ai.tasks import (
    _embedding_queue_backpressure,
    embedding_document_with_bge,
    embedding_document_batch_with_bge,
    enqueue_embedding_tasks,
    recover_document_pipeline_backlog,
    parse_document_with_docling,
)
from document_ai.tracing_utils import enqueue_kwargs
from files.models import FileBlob, Node

pytestmark = pytest.mark.integration

User = get_user_model()


class DocumentPipelineRecoveryTests(TestCase):
    """파싱 및 임베딩 비동기 파이프라인 복구 로직 검증"""
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

        node_ids = get_parse_recovery_node_ids(limit=1000)

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
            sparse_vector={"1": 1.0},
            status=AIStatus.COMPLETED,
        )

        old_time = timezone.now() - timedelta(hours=1)
        DocumentChunk.objects.filter(id__in=[stale_pending.id, stale_failed.id]).update(created_at=old_time)

        chunk_ids = get_embedding_recovery_chunk_ids(limit=1000)

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

        with patch("document_ai.orchestration.enqueue_embedding_batch") as dispatch_batch:
            result = enqueue_embedding_tasks_sync(node.id)

        chunk.refresh_from_db()
        dispatch_batch.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(chunk.status, AIStatus.PENDING)

    def test_enqueue_embedding_tasks_accepts_common_trace_envelope(self):
        envelope = enqueue_kwargs(trace_id="trace-envelope-test")

        with patch(
            "document_ai.services.executor_transport.dispatch_executor_job",
            return_value={"result": {"status": "success", "node_id": 123, "chunk_count": 0}},
        ) as dispatch:
            result = enqueue_embedding_tasks.run(123, **envelope)

        self.assertEqual(result["status"], "success")
        self.assertEqual(dispatch.call_args.kwargs["payload"], {"node_id": 123})
        self.assertEqual(dispatch.call_args.kwargs["envelope"]["trace_id"], "trace-envelope-test")

    def test_enqueue_embedding_task_retries_batch_publish_failure(self):
        failure = EmbeddingDispatchError(
            "broker unavailable",
            unpublished_chunk_ids=[10, 11],
        )
        from document_ai.services.executor_transport import ExecutorDispatchError
        with patch(
            "document_ai.services.executor_transport.dispatch_executor_job",
            side_effect=ExecutorDispatchError(str(failure)),
        ), patch.object(
            enqueue_embedding_tasks,
            "retry",
            side_effect=Retry(),
        ) as retry:
            with self.assertRaises(Retry):
                enqueue_embedding_tasks.run(123, trace_id="trace-retry")

        self.assertIn("broker unavailable", str(retry.call_args.kwargs["exc"]))

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
            result = embed_document_chunks_batch_sync([chunk.id])

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

        with patch("document_ai.processing.recovery.get_parse_recovery_node_ids", return_value=[parse_node.id]), patch(
            "document_ai.processing.recovery.get_embedding_recovery_chunk_ids",
            return_value=[failed_chunk.id],
        ), patch("document_ai.processing.recovery.enqueue_parse") as parse_delay, patch(
            "document_ai.processing.recovery.enqueue_embedding"
        ) as enqueue_delay, patch("document_ai.processing.recovery.recovery_redis_client") as mock_redis_ctor:
            # Redis 락 항상 성공 (중복 없음)
            mock_redis_ctor.return_value.set.return_value = True
            result = recover_document_pipeline_backlog()

        failed_chunk.refresh_from_db()

        parse_delay.assert_called_once()
        self.assertEqual(parse_delay.call_args.args, (parse_node.id,))
        self.assertIn("trace_id", parse_delay.call_args.kwargs)
        self.assertIn("enqueued_at", parse_delay.call_args.kwargs)
        # 복구 흐름: chunk → node 단위로 묶어 enqueue_embedding_tasks 경유
        # enqueue_embedding_tasks 가 PENDING→PROCESSING 전환 후 임베딩 큐잉을 담당함
        enqueue_delay.assert_called_once()
        self.assertEqual(enqueue_delay.call_args.args, (embed_node.id,))
        self.assertIn("trace_id", enqueue_delay.call_args.kwargs)
        self.assertIn("enqueued_at", enqueue_delay.call_args.kwargs)
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

        node_ids = get_node_ids_for_chunks([chunk_a.id, chunk_b.id])

        self.assertEqual(node_ids, [node.id])

    def test_get_node_ids_for_chunks_empty_input(self):
        node_ids = get_node_ids_for_chunks([])
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

        with patch("document_ai.processing.recovery.get_parse_recovery_node_ids", return_value=[parse_node.id]), patch(
            "document_ai.processing.recovery.get_embedding_recovery_chunk_ids",
            return_value=[stale_chunk.id],
        ), patch("document_ai.processing.recovery.enqueue_parse") as parse_delay, patch(
            "document_ai.processing.recovery.enqueue_embedding"
        ) as enqueue_delay, patch("document_ai.processing.recovery.recovery_redis_client") as mock_redis_ctor:
            # Redis SET NX 실패 → 락이 이미 존재 (중복 큐잉 방지)
            mock_redis_ctor.return_value.set.return_value = None
            result = recover_document_pipeline_backlog()

        parse_delay.assert_not_called()
        enqueue_delay.assert_not_called()
        stale_chunk.refresh_from_db()
        self.assertEqual(stale_chunk.status, AIStatus.FAILED)
        self.assertEqual(result["chunks_reset_to_pending"], 0)
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

        with patch("document_ai.processing.recovery.get_parse_recovery_node_ids", return_value=[parse_node.id]), patch(
            "document_ai.processing.recovery.get_embedding_recovery_chunk_ids",
            return_value=[stale_chunk.id],
        ), patch("document_ai.processing.recovery.enqueue_parse") as parse_delay, patch(
            "document_ai.processing.recovery.enqueue_embedding"
        ) as enqueue_delay, patch(
            "document_ai.processing.recovery.recovery_redis_client",
            side_effect=Exception("connection refused"),
        ):
            result = recover_document_pipeline_backlog()

        # Redis 없이도 정상 큐잉
        parse_delay.assert_called_once()
        self.assertEqual(parse_delay.call_args.args, (parse_node.id,))
        enqueue_delay.assert_called_once()
        self.assertEqual(enqueue_delay.call_args.args, (embed_node.id,))
        self.assertEqual(result["parse_requeued"], 1)
        self.assertEqual(result["embedding_nodes_requeued"], 1)

    def test_recovery_task_falls_back_when_redis_set_fails_lazily(self):
        """Redis clients connect lazily, so SET failures must also fail open."""
        parse_node = self._create_file_node("lazy-redis-failure.txt")

        with patch(
            "document_ai.processing.recovery.get_parse_recovery_node_ids",
            return_value=[parse_node.id],
        ), patch(
            "document_ai.processing.recovery.get_embedding_recovery_chunk_ids",
            return_value=[],
        ), patch("document_ai.processing.recovery.enqueue_parse") as enqueue_parse, patch(
            "document_ai.processing.recovery.recovery_redis_client"
        ) as redis_client:
            redis_client.return_value.set.side_effect = ConnectionError("redis down")
            result = recover_document_pipeline_backlog()

        enqueue_parse.assert_called_once()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["parse_requeued"], 1)

    def test_recovery_publish_failure_releases_lock_and_does_not_count_attempt(self):
        parse_node = self._create_file_node("publish-failure.txt")
        parse_result = DocumentParseResult.objects.create(
            node=parse_node,
            status=AIStatus.FAILED,
            chunk_count=0,
        )

        with patch(
            "document_ai.processing.recovery.get_parse_recovery_node_ids",
            return_value=[parse_node.id],
        ), patch(
            "document_ai.processing.recovery.get_embedding_recovery_chunk_ids",
            return_value=[],
        ), patch(
            "document_ai.processing.recovery.enqueue_parse",
            side_effect=ConnectionError("broker publish failed"),
        ), patch("document_ai.processing.recovery.recovery_redis_client") as redis_client:
            redis_client.return_value.set.return_value = True
            result = recover_document_pipeline_backlog()

        parse_result.refresh_from_db()
        redis_client.return_value.delete.assert_called_once_with(
            f"recovery:parse:{parse_node.id}"
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["parse_requeued"], 0)
        self.assertEqual(result["parse_publish_failed"], 1)
        self.assertEqual(parse_result.recovery_attempts, 0)

    def test_parse_and_embedding_tasks_are_pinned_to_separate_queues(self):
        self.assertEqual(parse_document_with_docling.queue, "parse")
        self.assertEqual(enqueue_embedding_tasks.queue, "embed")
        self.assertEqual(embedding_document_with_bge.queue, "embed")
        self.assertEqual(embedding_document_batch_with_bge.queue, "embed")

    @patch.dict(
        "os.environ",
        {"DOCUMENT_AI_EMBED_QUEUE_BACKPRESSURE_LIMIT": "3"},
    )
    def test_embedding_queue_depth_controls_parse_backpressure(self):
        with patch("document_ai.orchestration.broker_redis_client") as redis_client:
            redis_client.return_value.llen.return_value = 2
            self.assertEqual(_embedding_queue_backpressure(), (False, 2, 3))

            redis_client.return_value.llen.return_value = 3
            self.assertEqual(_embedding_queue_backpressure(), (True, 3, 3))

    def test_parse_backpressure_retries_without_consuming_failure_budget(self):
        with patch(
            "document_ai.tasks._embedding_queue_backpressure",
            return_value=(True, 32, 32),
        ), patch.object(
            parse_document_with_docling,
            "retry",
            side_effect=Retry(),
        ) as retry:
            with self.assertRaises(Retry):
                parse_document_with_docling.run(123)

        self.assertEqual(retry.call_args.kwargs["max_retries"], None)
        self.assertEqual(retry.call_args.kwargs["countdown"], 5)

    def test_enqueue_embedding_tasks_sync_dispatches_grouped_batch_tasks(self):
        node = self._create_file_node("batch-group.txt")
        parse_result = DocumentParseResult.objects.create(
            node=node, status=AIStatus.COMPLETED, chunk_count=3,
        )
        chunks = [
            DocumentChunk.objects.create(
                parse_result=parse_result,
                chunk_index=i,
                text=f"chunk {i}",
                status=AIStatus.PENDING,
                token_count=10,
            )
            for i in range(3)
        ]

        with self.settings(
            EMBEDDING_DOCUMENT_BATCH_MAX_CHUNKS=2, EMBEDDING_DOCUMENT_BATCH_MAX_TOKENS=1000
        ):
            dispatch_batch = Mock()
            result = enqueue_embedding_tasks_sync(
                node.id,
                dispatch_batch=dispatch_batch,
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["chunk_count"], 3)
        # count cap of 2 -> chunks [0,1] in one task, [2] in a second task
        # instead of one task per chunk.
        dispatched_batches = sorted(
            (call.args[0] for call in dispatch_batch.call_args_list), key=lambda b: b[0]
        )
        self.assertEqual(
            dispatched_batches,
            [[chunks[0].id, chunks[1].id], [chunks[2].id]],
        )
        for chunk in chunks:
            chunk.refresh_from_db()
            self.assertEqual(chunk.status, AIStatus.PROCESSING)

    def test_embedding_batch_publish_failure_releases_only_unpublished_chunks(self):
        node = self._create_file_node("partial-publish.txt")
        parse_result = DocumentParseResult.objects.create(
            node=node, status=AIStatus.COMPLETED, chunk_count=3,
        )
        chunks = [
            DocumentChunk.objects.create(
                parse_result=parse_result,
                chunk_index=index,
                text=f"chunk {index}",
                status=AIStatus.PENDING,
                token_count=10,
            )
            for index in range(3)
        ]
        dispatch_batch = Mock(side_effect=[None, ConnectionError("broker down")])

        with self.settings(
            EMBEDDING_DOCUMENT_BATCH_MAX_CHUNKS=2,
            EMBEDDING_DOCUMENT_BATCH_MAX_TOKENS=1000,
        ), self.assertRaises(EmbeddingDispatchError) as raised:
            enqueue_embedding_tasks_sync(
                node.id,
                dispatch_batch=dispatch_batch,
            )

        for chunk in chunks:
            chunk.refresh_from_db()
        self.assertEqual(chunks[0].status, AIStatus.PROCESSING)
        self.assertEqual(chunks[1].status, AIStatus.PROCESSING)
        self.assertEqual(chunks[2].status, AIStatus.PENDING)
        self.assertEqual(raised.exception.unpublished_chunk_ids, [chunks[2].id])
