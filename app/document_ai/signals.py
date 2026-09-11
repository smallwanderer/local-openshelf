import logging
from django.db.models.signals import post_save
from django.dispatch import receiver
from files.models import FileBlob
from document_ai.orchestration import enqueue_parse
from document_ai.tracing_utils import enqueue_kwargs

logger = logging.getLogger(__name__)


@receiver(post_save, sender=FileBlob)
def trigger_document_parsing(sender, instance, created, **kwargs):
    """
    FileBlob(파일 인스턴스)가 새로 생성되었을 때, 
    자동으로 파싱(및 이어서 임베딩)을 수행하는 Celery 태스크를 호출합니다.
    """
    if created:
        if not instance.node.ai_processing_enabled:
            logger.info(
                "New file uploaded (Node ID: %s). AI processing disabled; skipping parse task.",
                instance.node_id,
            )
            return
        logger.info(f"New file uploaded (Node ID: {instance.node_id}). Triggering parse_document_with_docling task.")
        # 파싱 태스크 비동기 호출! 파싱이 끝나면 내부적으로 임베딩 큐잉 태스크를 또 부르게 됩니다.
        # 업로드 요청 스레드에서 발생하므로 여기서 얻는 trace_id가 "적재 trace"의 시작점이 됩니다.
        enqueue_parse(instance.node_id, **enqueue_kwargs())
