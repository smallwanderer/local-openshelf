from __future__ import annotations

import asyncio
import json
import queue
import threading

from asgiref.sync import sync_to_async
from django.http import StreamingHttpResponse

from config.enums import QueryAnswerMode, QueryIntent, RAGStage
from config.tracing import get_trace_id, set_trace_id
from document_ai.models import RAGJob, SearchJob
from document_ai.services.rag_cancel_service import set_rag_cancel_signal


class RAGSearchError(RuntimeError):
    pass


class RAGSearchBusyError(RAGSearchError):
    """Search failed specifically because embedding-executor's admission queue
    rejected the query embedding call (EMBEDDING_BUSY) -- callers surface
    this as a retryable 503 instead of a generic search-failed 500.
    """

    def __init__(self, message: str, *, retry_after_seconds: float = 5.0):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _create_rag_jobs_sync(
    *,
    owner,
    workspace=None,
    question: str,
    retrieval_query: str,
    top_k: int,
    threshold: float | None,
    language: str,
    requested_node_ids: list[str],
    scoped_node_ids: list[str],
    llm_snapshot: dict,
    tuning_params: dict | None = None,
    conversation=None,
    reply_to=None,
) -> tuple[SearchJob, RAGJob]:
    from document_ai.search.execution import perform_vector_search_sync

    search_job = SearchJob.objects.create(
        owner=owner,
        workspace=workspace,
        query=retrieval_query,
        top_k=top_k,
        threshold=threshold,
        node_ids=scoped_node_ids,
        tuning_params=tuning_params or {},
    )
    search_result = perform_vector_search_sync(search_job.id, max_retries=0)
    search_job.refresh_from_db()
    if search_result.get("status") != "success":
        error_message = search_result.get("error") or "Search failed."
        if search_result.get("error_code") == "EMBEDDING_BUSY":
            raise RAGSearchBusyError(
                error_message,
                retry_after_seconds=search_result.get("retry_after_seconds", 5.0),
            )
        raise RAGSearchError(error_message)

    rag_job_values = dict(
        owner=owner,
        workspace=search_job.workspace,
        search_job=search_job,
        question=question,
        retrieval_query=retrieval_query,
        query_intent=QueryIntent.DOCUMENT_QUESTION,
        answer_mode=QueryAnswerMode.RAG,
        retrieval_required=True,
        query_confidence=0.0,
        top_k=top_k,
        language=language,
        node_ids=[str(node_id) for node_id in requested_node_ids],
        stage=RAGStage.GENERATING,
        stage_message="검색된 근거를 바탕으로 답변을 생성하고 있습니다.",
        **llm_snapshot,
    )
    if conversation is not None and reply_to is not None:
        from django.db import transaction
        from django.db.models import Max
        from document_ai.models import RAGConversation, RAGMessage

        with transaction.atomic():
            locked_conversation = RAGConversation.objects.select_for_update().get(pk=conversation.pk)
            if reply_to.conversation_id != locked_conversation.pk:
                raise ValueError("Reply and RAG job must belong to the same conversation.")
            rag_job = RAGJob.objects.create(
                conversation=locked_conversation, **rag_job_values
            )
            sequence = (
                RAGMessage.objects.filter(conversation=locked_conversation).aggregate(maximum=Max("sequence"))["maximum"]
                or 0
            ) + 1
            assistant_message = RAGMessage.objects.create(
                conversation=locked_conversation,
                sequence=sequence,
                role=RAGMessage.ROLE_ASSISTANT,
                reply_to=reply_to,
                rag_job=rag_job,
                node_ids=rag_job.node_ids,
            )
            locked_conversation.save(update_fields=["updated_at"])
        rag_job._conversation_message_uid = str(assistant_message.uid)
        rag_job._client_request_id = str(reply_to.client_request_id)
    else:
        rag_job = RAGJob.objects.create(**rag_job_values)
    return search_job, rag_job


# DEPRECATED - thread-per-request sync path, superseded by create_rag_streaming_response_async.
# RAGStreamView (document_ai/search/views.py) only calls the async version now. Kept because
# app/tests/test_llm_calibration.py still drives it directly; do not route live requests here.
def create_rag_streaming_response(
    *,
    owner,
    workspace=None,
    question: str,
    retrieval_query: str,
    top_k: int,
    threshold: float | None,
    language: str,
    requested_node_ids: list[str],
    scoped_node_ids: list[str],
    llm_snapshot: dict,
    admission_token=None,
    tuning_params: dict | None = None,
    conversation=None,
    reply_to=None,
) -> StreamingHttpResponse:
    try:
        search_job, rag_job = _create_rag_jobs_sync(
            owner=owner,
            workspace=workspace,
            question=question,
            retrieval_query=retrieval_query,
            top_k=top_k,
            threshold=threshold,
            language=language,
            requested_node_ids=requested_node_ids,
            scoped_node_ids=scoped_node_ids,
            llm_snapshot=llm_snapshot,
            tuning_params=tuning_params,
            conversation=conversation,
            reply_to=reply_to,
        )
    except Exception:
        # Nothing made it to the background generation thread, so this
        # function still owns releasing the admission token.
        if admission_token is not None:
            admission_token.release()
        raise

    def encode_event(event: dict) -> bytes:
        return (json.dumps(event, ensure_ascii=False, default=str) + "\n").encode("utf-8")

    def event_stream():
        from django.db import close_old_connections
        from document_ai.rag.generation import (
            _build_rag_context,
            _get_positive_int_env,
            generate_rag_response_sync,
        )

        event_queue: queue.Queue = queue.Queue()
        # threading.Thread does not inherit contextvars, so the request's trace_id
        # must be captured here and re-applied inside the worker thread.
        trace_id = get_trace_id()
        _, initial_citations = _build_rag_context(
            search_job.results,
            evidence_limit=_get_positive_int_env("RAG_EVIDENCE_LIMIT", 5),
            context_max_chars=_get_positive_int_env("RAG_CONTEXT_MAX_CHARS", 3000),
            evidence_text_max_chars=_get_positive_int_env("RAG_EVIDENCE_TEXT_MAX_CHARS", 500),
        )
        yield encode_event({
            "type": "started",
            "job_id": rag_job.id,
            "conversation_uid": str(conversation.uid) if conversation is not None else None,
            "message_uid": getattr(rag_job, "_conversation_message_uid", None),
            "client_request_id": getattr(rag_job, "_client_request_id", None),
            "llm_target": "external" if rag_job.llm_endpoint_id else "server",
            "llm_model": rag_job.llm_model,
        })
        yield encode_event({"type": "sources", "citations": initial_citations})

        def run_generation():
            set_trace_id(trace_id)
            close_old_connections()
            try:
                result = generate_rag_response_sync(
                    rag_job.id,
                    on_token=lambda token: event_queue.put({"type": "token", "text": token}),
                )
            except Exception as exc:
                result = {"status": "failed", "error": str(exc)}
            finally:
                close_old_connections()
                if admission_token is not None:
                    admission_token.release()
            event_queue.put({"type": "terminal", "result": result})

        worker = threading.Thread(target=run_generation, name=f"rag-stream-{rag_job.id}", daemon=True)
        worker.start()
        try:
            while True:
                event = event_queue.get()
                if event["type"] == "token":
                    yield encode_event(event)
                    continue

                rag_job.refresh_from_db()
                result = event["result"]
                result_status = result.get("status")
                if result_status in {"success", "insufficient_evidence"}:
                    yield encode_event({
                        "type": "completed",
                        "job_id": rag_job.id,
                        "answer": rag_job.answer,
                        "citations": rag_job.citations,
                        "performance_metrics": rag_job.performance_metrics,
                    })
                elif result_status == "canceled":
                    yield encode_event({
                        "type": "canceled",
                        "job_id": rag_job.id,
                        "performance_metrics": rag_job.performance_metrics,
                    })
                else:
                    yield encode_event({
                        "type": "error",
                        "job_id": rag_job.id,
                        "code": "RAG_GENERATION_FAILED",
                        "message": result.get("error") or rag_job.error_message or "Answer generation failed.",
                        "performance_metrics": rag_job.performance_metrics,
                    })
                break
        finally:
            if worker.is_alive():
                try:
                    set_rag_cancel_signal(rag_job.id)
                except Exception:
                    pass

    response = StreamingHttpResponse(event_stream(), content_type="application/x-ndjson; charset=utf-8")
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response


def _load_terminal_job_sync(rag_job_id: int) -> dict:
    rag_job = RAGJob.objects.get(pk=rag_job_id)
    return {
        "answer": rag_job.answer,
        "citations": rag_job.citations,
        "error_message": rag_job.error_message,
        "performance_metrics": rag_job.performance_metrics,
    }


async def create_rag_streaming_response_async(
    *,
    owner,
    workspace=None,
    question: str,
    retrieval_query: str,
    top_k: int,
    threshold: float | None,
    language: str,
    requested_node_ids: list[str],
    scoped_node_ids: list[str],
    llm_snapshot: dict,
    admission_token,
    tuning_params: dict | None = None,
    conversation=None,
    reply_to=None,
) -> StreamingHttpResponse:
    """Search in a bounded sync section, then stream LLM HTTP on the event loop."""

    try:
        search_job, rag_job = await sync_to_async(
            _create_rag_jobs_sync, thread_sensitive=True
        )(
            owner=owner,
            workspace=workspace,
            question=question,
            retrieval_query=retrieval_query,
            top_k=top_k,
            threshold=threshold,
            language=language,
            requested_node_ids=requested_node_ids,
            scoped_node_ids=scoped_node_ids,
            llm_snapshot=llm_snapshot,
            tuning_params=tuning_params,
            conversation=conversation,
            reply_to=reply_to,
        )
    except (Exception, asyncio.CancelledError):
        # CancelledError (client disconnect / stop-button abort during the
        # search phase, before generation starts) is BaseException, not
        # Exception, so it must be listed explicitly - otherwise the admission
        # token leaks until its Redis stale-client timeout expires, which
        # blocks all further RAG requests under the (typically 1-slot) local
        # concurrency limit until then.
        await admission_token.release_async()
        raise

    from document_ai.rag.async_generation import iter_rag_generation_events_async
    from document_ai.rag.generation import _build_rag_context, _get_positive_int_env

    _, initial_citations = _build_rag_context(
        search_job.results,
        evidence_limit=_get_positive_int_env("RAG_EVIDENCE_LIMIT", 5),
        context_max_chars=_get_positive_int_env("RAG_CONTEXT_MAX_CHARS", 3000),
        evidence_text_max_chars=_get_positive_int_env(
            "RAG_EVIDENCE_TEXT_MAX_CHARS", 500
        ),
    )

    def encode_event(event: dict) -> bytes:
        return (json.dumps(event, ensure_ascii=False, default=str) + "\n").encode(
            "utf-8"
        )

    async def event_stream():
        yield encode_event(
            {
                "type": "started",
                "job_id": rag_job.id,
                "conversation_uid": str(conversation.uid) if conversation is not None else None,
                "message_uid": getattr(rag_job, "_conversation_message_uid", None),
                "client_request_id": getattr(rag_job, "_client_request_id", None),
                "llm_target": "external" if rag_job.llm_endpoint_id else "server",
                "llm_model": rag_job.llm_model,
            }
        )
        yield encode_event({"type": "sources", "citations": initial_citations})
        try:
            async for event in iter_rag_generation_events_async(rag_job.id):
                if event["type"] == "token":
                    yield encode_event(event)
                    continue

                result = event["result"]
                terminal_job = await sync_to_async(
                    _load_terminal_job_sync, thread_sensitive=True
                )(rag_job.id)
                result_status = result.get("status")
                if result_status in {"success", "insufficient_evidence"}:
                    yield encode_event(
                        {
                            "type": "completed",
                            "job_id": rag_job.id,
                            "answer": terminal_job["answer"],
                            "citations": terminal_job["citations"],
                            "performance_metrics": terminal_job[
                                "performance_metrics"
                            ],
                        }
                    )
                elif result_status == "canceled":
                    yield encode_event(
                        {
                            "type": "canceled",
                            "job_id": rag_job.id,
                            "performance_metrics": terminal_job[
                                "performance_metrics"
                            ],
                        }
                    )
                else:
                    yield encode_event(
                        {
                            "type": "error",
                            "job_id": rag_job.id,
                            "code": "RAG_GENERATION_FAILED",
                            "message": result.get("error")
                            or terminal_job["error_message"]
                            or "Answer generation failed.",
                            "performance_metrics": terminal_job[
                                "performance_metrics"
                            ],
                        }
                    )
                break
        finally:
            # There is no detached generation thread in this path. Closing the
            # async iterator cancels the upstream httpx stream directly; the
            # generation iterator records the canceled terminal state.
            await admission_token.release_async()

    response = StreamingHttpResponse(
        event_stream(), content_type="application/x-ndjson; charset=utf-8"
    )
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    # Covers a response closed before the async iterator is entered. The token
    # is idempotent, so Django's normal response close after iteration is safe.
    response._resource_closers.append(admission_token.release)
    return response
