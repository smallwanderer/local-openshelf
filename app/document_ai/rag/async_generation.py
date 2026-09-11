from __future__ import annotations

import asyncio
import json
import logging
import os
import time as time_module
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
from asgiref.sync import sync_to_async
from django.utils import timezone
from redis.asyncio import Redis as AsyncRedis

from config.enums import AIStatus, QueryIntent, RAGStage
from config.tracing import get_trace_id
from document_ai.db_span import capture_db_spans
from document_ai.performance import datetime_delta_ms, elapsed_ms, put_metric
from document_ai.rag.generation import (
    _build_generation_prompts,
    _build_llm_request,
    _build_rag_context,
    _evidence_is_sufficient,
    _extract_llm_final_content,
    _fallback_rag_answer,
    _get_chat_completions_stream_delta_content,
    _get_positive_int_env,
    _get_responses_stream_delta_content,
    _get_stream_usage,
    _insufficient_evidence_answer,
    _normalize_rag_answer,
)
from document_ai.services.rag_cancel_service import (
    get_rag_cancel_key,
    get_rag_cancel_redis_url,
)

logger = logging.getLogger(__name__)


@dataclass
class AsyncGenerationContext:
    job_id: int
    request_url: str
    payload: dict
    headers: dict[str, str]
    request_timeout: int
    citations: list[dict]
    language: str
    metrics: dict
    worker_started: float
    created_at: object


def _finish_metrics(context: AsyncGenerationContext, *, completed_at=None, **extra) -> dict:
    metrics = context.metrics
    for name, value in extra.items():
        put_metric(metrics, name, value)
    put_metric(metrics, "worker_total_ms", elapsed_ms(context.worker_started))
    put_metric(
        metrics,
        "end_to_end_ms",
        datetime_delta_ms(context.created_at, completed_at or timezone.now()),
    )
    return metrics


def _prepare_generation_sync(
    rag_job_id: int,
) -> tuple[AsyncGenerationContext | None, dict | None]:
    from document_ai.models import RAGJob
    from document_ai.search.profiles import get_effective_generation_config
    from document_ai.services.llm_endpoint_service import resolve_rag_llm_request_config

    worker_started = time_module.perf_counter()
    with capture_db_spans():
        try:
            rag_job = RAGJob.objects.select_related("search_job", "llm_endpoint").get(
                pk=rag_job_id
            )
        except RAGJob.DoesNotExist:
            logger.error("RAG job %s not found", rag_job_id)
            return None, {
                "status": "failed",
                "job_id": rag_job_id,
                "error": f"RAG job {rag_job_id} not found",
            }

        metrics = dict(rag_job.performance_metrics or {})
        put_metric(metrics, "trace_id", get_trace_id())
        context_stub = AsyncGenerationContext(
            job_id=rag_job_id,
            request_url="",
            payload={},
            headers={},
            request_timeout=_get_positive_int_env("RAG_REQUEST_TIMEOUT", 300),
            citations=[],
            language=rag_job.language,
            metrics=metrics,
            worker_started=worker_started,
            created_at=rag_job.created_at,
        )

        skip_retrieval = not rag_job.search_job and (
            not rag_job.retrieval_required
            and rag_job.query_intent != QueryIntent.INFORMATION
        )
        if not skip_retrieval and (
            not rag_job.search_job or rag_job.search_job.status != AIStatus.COMPLETED
        ):
            rag_job.status = AIStatus.FAILED
            rag_job.stage = RAGStage.FAILED
            rag_job.stage_message = "검색 결과가 완료되지 않아 답변을 생성할 수 없습니다."
            rag_job.completed_at = timezone.now()
            rag_job.error_message = "Search job is not completed."
            rag_job.performance_metrics = _finish_metrics(
                context_stub, completed_at=rag_job.completed_at, failed=True
            )
            rag_job.save(
                update_fields=[
                    "status",
                    "stage",
                    "stage_message",
                    "completed_at",
                    "error_message",
                    "performance_metrics",
                ]
            )
            return None, {
                "status": "failed",
                "job_id": rag_job_id,
                "error": rag_job.error_message,
            }

        rag_job.status = AIStatus.PROCESSING
        rag_job.stage = RAGStage.GENERATING
        rag_job.stage_message = "검색된 근거를 바탕으로 답변을 생성하고 있습니다."
        rag_job.started_at = timezone.now()
        rag_job.error_message = ""
        if "queue_wait_ms" not in metrics:
            put_metric(
                metrics,
                "queue_wait_ms",
                datetime_delta_ms(rag_job.created_at, rag_job.started_at),
            )
        if rag_job.search_job_id:
            put_metric(
                metrics,
                "generation_queue_wait_ms",
                datetime_delta_ms(rag_job.search_job.completed_at, rag_job.started_at),
            )
        rag_job.performance_metrics = metrics
        rag_job.save(
            update_fields=[
                "status",
                "stage",
                "stage_message",
                "started_at",
                "error_message",
                "performance_metrics",
            ]
        )

        context_started = time_module.perf_counter()
        if skip_retrieval:
            context_text, citations = "", []
        else:
            context_text, citations = _build_rag_context(
                rag_job.search_job.results,
                evidence_limit=_get_positive_int_env("RAG_EVIDENCE_LIMIT", 5),
                context_max_chars=_get_positive_int_env("RAG_CONTEXT_MAX_CHARS", 3000),
                evidence_text_max_chars=_get_positive_int_env(
                    "RAG_EVIDENCE_TEXT_MAX_CHARS", 500
                ),
            )
        put_metric(metrics, "context_build_ms", elapsed_ms(context_started))
        metrics["citation_count"] = len(citations)
        metrics["context_chars"] = len(context_text)
        context_stub.citations = citations

        if not skip_retrieval and not _evidence_is_sufficient(
            citations, rag_job.search_job.threshold
        ):
            rag_job.answer = _insufficient_evidence_answer(
                rag_job.language, scoped=bool(rag_job.node_ids)
            )
            rag_job.status = AIStatus.COMPLETED
            rag_job.stage = RAGStage.COMPLETED
            rag_job.stage_message = "문서에서 충분한 근거를 찾지 못했습니다."
            rag_job.completed_at = timezone.now()
            rag_job.error_message = ""
            rag_job.citations = []
            rag_job.performance_metrics = _finish_metrics(
                context_stub,
                completed_at=rag_job.completed_at,
                skipped_llm=True,
                output_chars=len(rag_job.answer),
            )
            rag_job.save(
                update_fields=[
                    "answer",
                    "status",
                    "stage",
                    "stage_message",
                    "completed_at",
                    "error_message",
                    "citations",
                    "performance_metrics",
                ]
            )
            return None, {
                "status": "insufficient_evidence",
                "job_id": rag_job_id,
                "citation_count": 0,
            }

        llm_config = resolve_rag_llm_request_config(rag_job)
        generation_config = get_effective_generation_config(rag_job.workspace)
        system_prompt, user_prompt, fewshot_user, fewshot_assistant = (
            _build_generation_prompts(
                rag_job,
                skip_retrieval=skip_retrieval,
                context_text=context_text,
            )
        )
        request_url, payload = _build_llm_request(
            llm_config=llm_config,
            system_prompt=system_prompt,
            fewshot_user=fewshot_user,
            fewshot_assistant=fewshot_assistant,
            user_prompt=user_prompt,
            max_tokens=generation_config["max_output_tokens"],
            temperature=generation_config["temperature"],
            top_p=generation_config["top_p"],
        )
        context_stub.request_url = request_url
        context_stub.payload = payload
        context_stub.headers = llm_config.headers
        return context_stub, None


def _complete_generation_sync(
    context: AsyncGenerationContext,
    *,
    raw_answer: str,
    emitted_answer: str,
) -> tuple[dict, str]:
    from document_ai.models import RAGJob
    from document_ai.services.rag_cancel_service import clear_rag_cancel_signal

    with capture_db_spans():
        rag_job = RAGJob.objects.get(pk=context.job_id)
        if rag_job.status == AIStatus.CANCELED:
            rag_job.performance_metrics = _finish_metrics(
                context, completed_at=rag_job.completed_at, canceled=True
            )
            rag_job.save(update_fields=["performance_metrics"])
            clear_rag_cancel_signal(rag_job.id)
            return {"status": "canceled", "job_id": rag_job.id}, ""

        answer = _normalize_rag_answer(raw_answer)
        if not answer:
            logger.warning(
                "RAG answer normalization returned empty output: job_id=%s raw_preview=%r",
                rag_job.id,
                raw_answer[:500],
            )
            answer = _fallback_rag_answer(context.citations, context.language)
            rag_job.error_message = (
                "LLM returned no final answer after normalization. "
                f"raw_preview={raw_answer[:1000]}"
            )
        else:
            rag_job.error_message = ""

        rag_job.answer = answer
        rag_job.citations = context.citations
        rag_job.status = AIStatus.COMPLETED
        rag_job.stage = RAGStage.COMPLETED
        rag_job.stage_message = "답변 생성이 완료되었습니다."
        rag_job.completed_at = timezone.now()
        rag_job.performance_metrics = _finish_metrics(
            context,
            completed_at=rag_job.completed_at,
            output_chars=len(answer),
        )
        rag_job.save(
            update_fields=[
                "answer",
                "citations",
                "status",
                "stage",
                "stage_message",
                "completed_at",
                "error_message",
                "performance_metrics",
            ]
        )
        logger.info(
            "Async RAG generation completed: job_id=%s performance=%s",
            rag_job.id,
            rag_job.performance_metrics,
        )
        final_delta = answer[len(emitted_answer) :] if answer.startswith(emitted_answer) else ""
        return {
            "status": "success",
            "job_id": rag_job.id,
            "citation_count": len(context.citations),
        }, final_delta


def _mark_generation_canceled_sync(context: AsyncGenerationContext) -> dict:
    from document_ai.models import RAGJob
    from document_ai.services.rag_cancel_service import clear_rag_cancel_signal

    with capture_db_spans():
        rag_job = RAGJob.objects.get(pk=context.job_id)
        rag_job.status = AIStatus.CANCELED
        rag_job.stage = RAGStage.CANCELED
        rag_job.stage_message = "사용자 요청으로 RAG 작업을 중단했습니다."
        rag_job.canceled_at = rag_job.canceled_at or timezone.now()
        rag_job.completed_at = rag_job.completed_at or timezone.now()
        rag_job.performance_metrics = _finish_metrics(
            context, completed_at=rag_job.completed_at, canceled=True
        )
        rag_job.save(
            update_fields=[
                "status",
                "stage",
                "stage_message",
                "canceled_at",
                "completed_at",
                "performance_metrics",
            ]
        )
        clear_rag_cancel_signal(rag_job.id)
        return {"status": "canceled", "job_id": rag_job.id}


def _mark_generation_failed_sync(
    context: AsyncGenerationContext,
    *,
    error: str,
    timeout: bool = False,
) -> dict:
    from document_ai.models import RAGJob

    with capture_db_spans():
        rag_job = RAGJob.objects.get(pk=context.job_id)
        rag_job.status = AIStatus.FAILED
        rag_job.stage = RAGStage.FAILED
        rag_job.stage_message = (
            "모델 응답 시간이 초과되었습니다."
            if timeout
            else "답변 생성 중 오류가 발생했습니다."
        )
        rag_job.completed_at = timezone.now()
        rag_job.error_message = error
        rag_job.performance_metrics = _finish_metrics(
            context,
            completed_at=rag_job.completed_at,
            failed=True,
            timeout=timeout,
        )
        rag_job.save(
            update_fields=[
                "status",
                "stage",
                "stage_message",
                "completed_at",
                "error_message",
                "performance_metrics",
            ]
        )
        return {"status": "failed", "job_id": rag_job.id, "error": error}


def _parse_stream_line(raw_line: str | bytes, usage_holder: dict) -> str:
    if not raw_line:
        return ""
    line = (
        raw_line.decode("utf-8", errors="replace")
        if isinstance(raw_line, bytes)
        else str(raw_line)
    ).strip()
    if not line or line.startswith(":"):
        return ""
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line or line == "[DONE]":
        return ""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        logger.debug("Skipping non-JSON RAG stream line: %r", line[:200])
        return ""
    usage = _get_stream_usage(payload)
    if usage:
        usage_holder.update(usage)
    return _get_responses_stream_delta_content(
        payload
    ) or _get_chat_completions_stream_delta_content(payload)


async def iter_rag_generation_events_async(
    rag_job_id: int,
) -> AsyncIterator[dict]:
    """Stream generation events without dedicating a thread to upstream HTTP."""

    context, immediate_result = await sync_to_async(
        _prepare_generation_sync, thread_sensitive=True
    )(rag_job_id)
    if immediate_result is not None:
        yield {"type": "terminal", "result": immediate_result}
        return
    assert context is not None

    redis_client = AsyncRedis.from_url(get_rag_cancel_redis_url())
    cancel_key = get_rag_cancel_key(rag_job_id)
    raw_parts: list[str] = []
    emitted_answer = ""
    first_token_at = None
    usage_holder: dict = {}
    llm_started = time_module.perf_counter()
    last_cancel_check = 0.0

    try:
        if await redis_client.exists(cancel_key):
            result = await sync_to_async(
                _mark_generation_canceled_sync, thread_sensitive=True
            )(context)
            yield {"type": "terminal", "result": result}
            return

        timeout = httpx.Timeout(context.request_timeout, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                context.request_url,
                json=context.payload,
                headers=context.headers,
            ) as response:
                put_metric(context.metrics, "llm_connect_ms", elapsed_ms(llm_started))
                if response.is_error:
                    await response.aread()
                    raise RuntimeError(
                        f"RAG LLM HTTP {response.status_code}: {response.text[:2000]}"
                    )

                async for raw_line in response.aiter_lines():
                    content = _parse_stream_line(raw_line, usage_holder)
                    if not content:
                        continue
                    if first_token_at is None:
                        first_token_at = time_module.perf_counter()
                        put_metric(
                            context.metrics,
                            "llm_ttft_ms",
                            elapsed_ms(llm_started, first_token_at),
                        )
                    raw_parts.append(content)
                    candidate = _extract_llm_final_content(
                        "".join(raw_parts), fallback_to_raw=True
                    )
                    if candidate.startswith(emitted_answer):
                        safe_delta = candidate[len(emitted_answer) :]
                        if safe_delta:
                            emitted_answer = candidate
                            yield {"type": "token", "text": safe_delta}

                    now_monotonic = time_module.monotonic()
                    if now_monotonic - last_cancel_check >= 0.5:
                        last_cancel_check = now_monotonic
                        if await redis_client.exists(cancel_key):
                            result = await sync_to_async(
                                _mark_generation_canceled_sync,
                                thread_sensitive=True,
                            )(context)
                            yield {"type": "terminal", "result": result}
                            return

        raw_answer = "".join(raw_parts).strip()
        put_metric(context.metrics, "llm_total_ms", elapsed_ms(llm_started))
        generation_after_first_token_ms = None
        if first_token_at is not None:
            generation_after_first_token_ms = elapsed_ms(first_token_at)
            put_metric(
                context.metrics,
                "llm_generation_after_first_token_ms",
                generation_after_first_token_ms,
            )
        output_token_count = usage_holder.get("completion_tokens") or usage_holder.get(
            "output_tokens"
        )
        input_token_count = usage_holder.get("prompt_tokens") or usage_holder.get(
            "input_tokens"
        )
        put_metric(context.metrics, "output_token_count", output_token_count)
        put_metric(context.metrics, "input_token_count", input_token_count)
        if output_token_count and generation_after_first_token_ms:
            put_metric(
                context.metrics,
                "output_tokens_per_second",
                round(
                    (output_token_count - 1)
                    / (generation_after_first_token_ms / 1000),
                    3,
                ),
            )

        if await redis_client.exists(cancel_key):
            result = await sync_to_async(
                _mark_generation_canceled_sync, thread_sensitive=True
            )(context)
            yield {"type": "terminal", "result": result}
            return

        result, final_delta = await sync_to_async(
            _complete_generation_sync, thread_sensitive=True
        )(context, raw_answer=raw_answer, emitted_answer=emitted_answer)
        if final_delta:
            yield {"type": "token", "text": final_delta}
        yield {"type": "terminal", "result": result}
    except asyncio.CancelledError:
        await sync_to_async(_mark_generation_canceled_sync, thread_sensitive=True)(
            context
        )
        raise
    except httpx.TimeoutException as exc:
        error = f"RAG request timed out after {context.request_timeout}s"
        logger.error("Async RAG LLM timeout after %ss: %s", context.request_timeout, exc)
        result = await sync_to_async(
            _mark_generation_failed_sync, thread_sensitive=True
        )(context, error=error, timeout=True)
        yield {"type": "terminal", "result": result}
    except Exception as exc:
        logger.exception("Async RAG LLM error: job_id=%s", rag_job_id)
        result = await sync_to_async(
            _mark_generation_failed_sync, thread_sensitive=True
        )(context, error=str(exc))
        yield {"type": "terminal", "result": result}
    finally:
        await redis_client.aclose()
