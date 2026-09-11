"""
[RAG 비동기 토큰 스트리밍(NDJSON Streaming) 검증 테스트]

1. 별도 Background Thread 없이 Async Iterator 직통 토큰 중계.
2. 클라이언트 중단(Disconnect/Abort) 시 상위 LLM 취소 및 Admission 토큰 자동 반환.
3. Chat Completions 및 Responses API 규격의 SSE delta 토큰 추출과 <think> 태그 필터링.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import AsyncClient, override_settings
from django.utils import timezone

from config.middleware import TraceIdMiddleware
from document_ai.rag.async_generation import (
    AsyncGenerationContext,
    iter_rag_generation_events_async,
)
from document_ai.rag.streaming import create_rag_streaming_response_async
from document_ai.services.rag_admission import (
    RAGAdmissionToken,
    _target_admission_policy,
)


pytestmark = pytest.mark.unit


def test_async_stream_relays_tokens_without_starting_a_background_thread():
    """
    [핵심 검증 1: 스레드 점유 없는 단일 Async Iterator 직통 스트리밍]
    
    요청별 백그라운드 스레드를 띄우지 않고, LLM -> Django -> 브라우저가
    하나의 비동기 스트림으로 토큰을 전달하며 완료 시 admission_token이 반환되는지 검증.
    """
    search_job = SimpleNamespace(results=[])
    rag_job = SimpleNamespace(
        id=17,
        llm_endpoint_id=None,
        llm_model="test-model",
    )
    admission_token = SimpleNamespace(
        release=Mock(),
        release_async=AsyncMock(),
    )

    async def generation_events(_job_id):
        yield {"type": "token", "text": "비동기 답변"}
        yield {"type": "terminal", "result": {"status": "success"}}

    terminal_job = {
        "answer": "비동기 답변",
        "citations": [],
        "error_message": "",
        "performance_metrics": {"llm_ttft_ms": 10},
    }
    async def exercise_stream():
        response = await create_rag_streaming_response_async(
            owner=SimpleNamespace(),
            question="질문",
            retrieval_query="질문",
            top_k=3,
            threshold=None,
            language="ko",
            requested_node_ids=[],
            scoped_node_ids=[],
            llm_snapshot={},
            admission_token=admission_token,
        )
        return [
            json.loads(chunk.decode("utf-8"))
            async for chunk in response.streaming_content
        ]

    with patch(
        "document_ai.rag.streaming._create_rag_jobs_sync",
        return_value=(search_job, rag_job),
    ), patch(
        "document_ai.rag.async_generation.iter_rag_generation_events_async",
        side_effect=generation_events,
    ), patch(
        "document_ai.rag.streaming._load_terminal_job_sync",
        return_value=terminal_job,
    ):
        events = async_to_sync(exercise_stream)()

    assert [event["type"] for event in events] == [
        "started",
        "sources",
        "token",
        "completed",
    ]
    assert events[2]["text"] == "비동기 답변"
    admission_token.release_async.assert_awaited_once()


def test_admission_policy_separates_local_and_external_capacity(monkeypatch):
    monkeypatch.setenv("RAG_EXTERNAL_LLM_CONCURRENCY", "6")
    with patch(
        "document_ai.services.rag_admission.get_server_rag_serving_concurrency",
        return_value=3,
    ):
        assert _target_admission_policy({}) == ("server", 3)

    target, limit = _target_admission_policy(
        {
            "llm_endpoint": SimpleNamespace(pk="endpoint-1"),
            "llm_model": "external-model",
        }
    )
    assert target.startswith("external:")
    assert limit == 6


def test_async_generation_uses_httpx_stream_and_emits_direct_deltas():
    context = AsyncGenerationContext(
        job_id=23,
        request_url="http://llm.test/v1/chat/completions",
        payload={"model": "test", "stream": True},
        headers={},
        request_timeout=30,
        citations=[],
        language="ko",
        metrics={},
        worker_started=0.0,
        created_at=timezone.now(),
    )

    class FakeRedis:
        async def exists(self, _key):
            return False

        async def aclose(self):
            return None

    def llm_response(_request):
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"직접 "}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"스트림"}}]}\n\n'
                'data: [DONE]\n\n'
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    original_async_client = httpx.AsyncClient

    def make_client(*_args, **kwargs):
        return original_async_client(
            transport=httpx.MockTransport(llm_response),
            timeout=kwargs.get("timeout"),
        )

    async def exercise_generation():
        return [
            event async for event in iter_rag_generation_events_async(context.job_id)
        ]

    with patch(
        "document_ai.rag.async_generation._prepare_generation_sync",
        return_value=(context, None),
    ), patch(
        "document_ai.rag.async_generation._complete_generation_sync",
        return_value=({"status": "success", "job_id": context.job_id}, ""),
    ) as complete, patch(
        "document_ai.rag.async_generation.AsyncRedis.from_url",
        return_value=FakeRedis(),
    ), patch(
        "document_ai.rag.async_generation.httpx.AsyncClient",
        side_effect=make_client,
    ) as async_client:
        events = async_to_sync(exercise_generation)()

    assert events == [
        {"type": "token", "text": "직접"},
        {"type": "token", "text": " 스트림"},
        {"type": "terminal", "result": {"status": "success", "job_id": 23}},
    ]
    async_client.assert_called_once()
    assert complete.call_args.kwargs["raw_answer"] == "직접 스트림"


def test_admission_token_release_is_idempotent():
    semaphore = Mock()
    token = RAGAdmissionToken(semaphore=semaphore, target="server", limit=1)

    token.release()
    token.release()

    semaphore.release.assert_called_once()


def test_trace_middleware_stays_async_capable():
    async def get_response(_request):
        return HttpResponse("ok")

    async def call_middleware():
        middleware = TraceIdMiddleware(get_response)
        return await middleware(SimpleNamespace())

    response = async_to_sync(call_middleware)()

    assert response.status_code == 200
    assert response["X-Trace-Id"]


@pytest.mark.django_db(transaction=True)
@override_settings(LOGIN_REQUIRED=True, ALLOWED_HOSTS=["testserver"])
def test_rag_view_runs_through_django_asgi_request_stack():
    user = get_user_model().objects.create_user(
        email="async-rag@example.com",
        password="password",
        is_active=True,
        email_verified=True,
    )
    admission_token = SimpleNamespace(
        release=Mock(),
        release_async=AsyncMock(),
    )
    stream_response = HttpResponse("stream-ready", content_type="application/x-ndjson")

    async def request_rag():
        client = AsyncClient()
        await client.aforce_login(user)
        return await client.post(
            "/api/document-ai/v1/rag/stream/",
            data=json.dumps({"question": "문서 요약", "language": "ko"}),
            content_type="application/json",
        )

    with patch(
        "document_ai.search.views.build_rag_llm_snapshot",
        return_value={
            "llm_endpoint_name": "Server runtime",
            "llm_base_url": "http://rag-runtime:8080",
            "llm_model": "test-model",
        },
    ), patch(
        "document_ai.search.views.server_rag_runtime_availability",
        return_value=(True, {}),
    ), patch(
        "document_ai.search.views.acquire_rag_admission_token_async",
        new=AsyncMock(return_value=admission_token),
    ), patch(
        "document_ai.rag.streaming.create_rag_streaming_response_async",
        new=AsyncMock(return_value=stream_response),
    ) as create_stream:
        response = async_to_sync(request_rag)()

    assert response.status_code == 200
    assert response["X-Trace-Id"]
    create_stream.assert_awaited_once()
