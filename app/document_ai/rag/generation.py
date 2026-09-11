from __future__ import annotations

import json
import logging
import os
import re
import time as time_module

from django.utils import timezone

from config.enums import AIStatus, QueryIntent, RAGStage
from config.tracing import get_trace_id
from document_ai.db_span import capture_db_spans
from document_ai.performance import datetime_delta_ms, elapsed_ms, put_metric

logger = logging.getLogger(__name__)


def _get_positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default

    if value < 1:
        logger.warning("%s must be >= 1, falling back to %s", name, default)
        return default

    return value


def _get_responses_stream_delta_content(payload: dict) -> str:
    event_type = payload.get("type")
    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
        return str(payload.get("delta") or "")

    if event_type == "response.output_item.done":
        item = payload.get("item") or {}
        content_parts = item.get("content") or []
        return "".join(
            str(part.get("text", ""))
            for part in content_parts
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}
        )

    response = payload.get("response") or {}
    output_parts = response.get("output") or payload.get("output") or []
    text_parts = []
    for item in output_parts:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                text_parts.append(str(part.get("text", "")))
    if text_parts:
        return "".join(text_parts)

    return ""


def _get_chat_completions_stream_delta_content(payload: dict) -> str:
    choice = (payload.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if content is None:
        content = choice.get("message", {}).get("content")
    if content is None:
        content = choice.get("text")
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content or "")


def _get_stream_usage(payload: dict) -> dict | None:
    # Chat Completions final chunk (stream_options.include_usage=True): top-level "usage".
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    # Responses API "response.completed" event: usage nested under "response".
    usage = (payload.get("response") or {}).get("usage")
    if isinstance(usage, dict):
        return usage
    return None


def _iter_openai_responses_stream_content(response, usage_holder: dict | None = None):
    for raw_line in response.iter_lines(decode_unicode=False):
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON RAG stream line: %r", line[:200])
            continue
        if usage_holder is not None:
            usage = _get_stream_usage(payload)
            if usage:
                usage_holder.update(usage)
        content = _get_responses_stream_delta_content(payload)
        if not content:
            content = _get_chat_completions_stream_delta_content(payload)
        if content:
            yield content


def _strip_llm_control_tokens(text: str) -> str:
    cleaned = re.sub(r"<\|[^>]+?\|>", "", text or "").strip()
    cleaned = re.sub(r"^```(?:[a-zA-Z]+)?\s*|\s*```$", "", cleaned).strip()
    return cleaned


def _extract_llm_final_content(text: str, *, fallback_to_raw: bool = True) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""

    final_markers = [
        "<|channel>final",
        "<channel|>",
        "<|final|>",
        "</think>",
        "Final Output:",
        "Final Output",
        "최종 답변:",
        "답변:",
    ]
    for marker in final_markers:
        if marker in raw:
            return _strip_llm_control_tokens(raw.split(marker, 1)[1])

    if "<|channel>thought" in raw or "<|think" in raw or "<think>" in raw:
        return ""

    if not fallback_to_raw:
        return ""
    raw = re.sub(r"<\|channel\>thought[\s\S]*?(?=<\|channel\>final|$)", "", raw).strip()
    raw = re.sub(r"<think>[\s\S]*?(?=</think>|$)", "", raw).strip()
    return _strip_llm_control_tokens(raw)


def _render_citation_block(citation: dict) -> str:
    return "\n".join(
        [
            f"[{citation['id']}] {citation['node_name']}",
            f"section: {citation['section'] or 'N/A'}",
            f"pages: {citation['pages'] or 'N/A'}",
            f"text: {citation['text']}",
        ]
    )


def _merge_citation_field(existing_value: str, new_value) -> str:
    new_value = str(new_value or "").strip()
    if not new_value:
        return existing_value
    parts = [part.strip() for part in existing_value.split(",")] if existing_value else []
    if new_value in parts:
        return existing_value
    parts.append(new_value)
    return ", ".join(part for part in parts if part)


def _better_citation_score(existing, candidate):
    existing_score = _coerce_score(existing)
    candidate_score = _coerce_score(candidate)
    if candidate_score is None:
        return existing
    if existing_score is None or candidate_score > existing_score:
        return candidate
    return existing


def _build_rag_context(
    results: list,
    *,
    evidence_limit: int = 3,
    context_max_chars: int = 3000,
    evidence_text_max_chars: int = 500,
) -> tuple[str, list[dict]]:
    # Evidences from the same document are merged into one citation instead of
    # getting a separate [N] per chunk - otherwise the same source shows up as
    # [1][2][3] in the answer even though it is a single document.
    citations: list[dict] = []
    citation_by_node: dict[str, dict] = {}
    evidence_count = 0
    used_chars = 0

    for result in results or []:
        node_name = result.get("node_name", "")
        node_id = result.get("node_id")
        doc_score = result.get("doc_score")
        node_key = str(node_id) if node_id is not None else f"name:{node_name}"

        for evidence in result.get("evidences", []) or []:
            if evidence_count >= evidence_limit:
                return "\n\n".join(_render_citation_block(c) for c in citations), citations

            text = evidence.get("compressed_text") or evidence.get("context_text") or evidence.get("text") or ""
            text = str(text).strip()
            if not text:
                continue
            remaining_chars = context_max_chars - used_chars
            if remaining_chars <= 0:
                return "\n\n".join(_render_citation_block(c) for c in citations), citations

            text_limit = max(100, min(evidence_text_max_chars, remaining_chars))
            clipped_text = text[:text_limit]

            existing = citation_by_node.get(node_key)
            if existing is not None:
                before_len = len(_render_citation_block(existing))
                merged_text = f"{existing['text']}\n{clipped_text}"
                merged_section = _merge_citation_field(existing["section"], evidence.get("section"))
                merged_pages = _merge_citation_field(existing["pages"], evidence.get("pages"))
                after_len = before_len + len(merged_text) - len(existing["text"]) + len(merged_section) - len(
                    existing["section"]
                ) + len(merged_pages) - len(existing["pages"])
                if used_chars + (after_len - before_len) > context_max_chars:
                    return "\n\n".join(_render_citation_block(c) for c in citations), citations

                existing["text"] = merged_text
                existing["section"] = merged_section
                existing["pages"] = merged_pages
                existing["chunk_ids"].append(evidence.get("chunk_id"))
                existing["hybrid_score"] = _better_citation_score(existing.get("hybrid_score"), evidence.get("hybrid_score"))
                existing["dense_score"] = _better_citation_score(existing.get("dense_score"), evidence.get("dense_score"))
                existing["sparse_score"] = _better_citation_score(existing.get("sparse_score"), evidence.get("sparse_score"))
                used_chars += after_len - before_len
                evidence_count += 1
                continue

            citation = {
                "id": len(citations) + 1,
                "node_id": str(node_id) if node_id is not None else "",
                "node_name": node_name,
                "chunk_ids": [evidence.get("chunk_id")],
                "section": str(evidence.get("section") or ""),
                "pages": str(evidence.get("pages") or ""),
                "doc_score": doc_score,
                "hybrid_score": evidence.get("hybrid_score"),
                "dense_score": evidence.get("dense_score"),
                "sparse_score": evidence.get("sparse_score"),
                "text": clipped_text,
            }
            block = _render_citation_block(citation)
            if used_chars + len(block) > context_max_chars and citations:
                return "\n\n".join(_render_citation_block(c) for c in citations), citations

            citations.append(citation)
            citation_by_node[node_key] = citation
            used_chars += len(block) + 2
            evidence_count += 1

    return "\n\n".join(_render_citation_block(c) for c in citations), citations


def _coerce_score(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _citation_best_score(citation: dict) -> float | None:
    doc_score = _coerce_score(citation.get("doc_score"))
    if doc_score is not None:
        return doc_score

    score_candidates = [
        citation.get("hybrid_score"),
        citation.get("dense_score"),
        citation.get("sparse_score"),
    ]
    scores = [_coerce_score(value) for value in score_candidates]
    scores = [score for score in scores if score is not None]
    return max(scores) if scores else None


def _evidence_is_sufficient(citations: list[dict], threshold: float | None) -> bool:
    if not citations:
        return False
    if threshold is None:
        return True

    saw_numeric_score = False
    for citation in citations:
        score = _citation_best_score(citation)
        if score is None:
            continue
        saw_numeric_score = True
        if score >= threshold:
            return True

    return not saw_numeric_score


def _insufficient_evidence_answer(language: str, *, scoped: bool) -> str:
    if language == "ko":
        if scoped:
            return (
                "선택한 문서 범위에서 이 질문에 답할 충분한 근거를 찾지 못했습니다.\n"
                "질문 범위를 넓히거나 다른 문서를 선택해 다시 시도해 주세요."
            )
        return "현재 문서에서 이 질문과 직접 관련된 충분한 근거를 찾지 못했습니다."

    if scoped:
        return (
            "I could not find enough evidence in the selected document scope to answer this question.\n"
            "Try broadening the scope or selecting different documents."
        )
    return "I could not find enough directly relevant evidence in the current documents to answer this question."


def _uses_openai_responses_api(base_url: str) -> bool:
    normalized = (base_url or "").strip().lower().rstrip("/")
    return normalized == "https://api.openai.com" or normalized.endswith(".openai.com")


def _build_responses_payload(
    *,
    llm_config,
    system_prompt: str,
    fewshot_user: str,
    fewshot_assistant: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
) -> dict:
    payload = {
        "model": llm_config.model,
        "instructions": system_prompt,
        "input": [
            {"role": "user", "content": fewshot_user},
            {"role": "assistant", "content": fewshot_assistant},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "top_p": top_p,
        "stream": True,
    }
    token_limit_key = "max_output_tokens" if _uses_openai_responses_api(llm_config.base_url) else "max_tokens"
    payload[token_limit_key] = max_tokens
    return payload


def _build_chat_completions_payload(
    *,
    llm_config,
    system_prompt: str,
    fewshot_user: str,
    fewshot_assistant: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
) -> dict:
    return {
        "model": llm_config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": fewshot_user},
            {"role": "assistant", "content": fewshot_assistant},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stream": True,
        # OpenAI-compatible streaming servers (llama.cpp, vLLM) omit token usage
        # from stream chunks unless explicitly requested. Needed to compute the
        # actual generation throughput (tokens/sec) for concurrency tuning.
        "stream_options": {"include_usage": True},
    }


def _build_llm_request(*, llm_config, system_prompt, fewshot_user, fewshot_assistant, user_prompt, max_tokens, temperature, top_p):
    if _uses_openai_responses_api(llm_config.base_url):
        return (
            llm_config.responses_url,
            _build_responses_payload(
                llm_config=llm_config,
                system_prompt=system_prompt,
                fewshot_user=fewshot_user,
                fewshot_assistant=fewshot_assistant,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
            ),
        )

    return (
        llm_config.chat_completions_url,
        _build_chat_completions_payload(
            llm_config=llm_config,
            system_prompt=system_prompt,
            fewshot_user=fewshot_user,
            fewshot_assistant=fewshot_assistant,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        ),
    )


def _normalize_rag_answer(raw_answer: str) -> str:
    answer = _extract_llm_final_content(raw_answer, fallback_to_raw=True)
    if not answer:
        return ""

    answer = re.sub(r"<\|channel\>thought[\s\S]*?(?=<\|channel\>final|$)", "", answer).strip()
    answer = re.sub(r"<\|[^>]+?\|>", "", answer).strip()
    answer = re.sub(r"^```(?:[a-zA-Z]+)?\s*|\s*```$", "", answer).strip()
    return answer


def _clean_rag_evidence_text(text: str, *, limit: int = 220) -> str:
    cleaned = re.sub(r"<!--.*?-->", " ", str(text or ""), flags=re.DOTALL)
    cleaned = re.sub(r"\|[-:\s|]+\|", " ", cleaned)
    cleaned = re.sub(r"[|#*_`]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "..."
    return cleaned


def _fallback_rag_answer(citations: list[dict], language: str) -> str:
    if not citations:
        return "근거가 부족해 답변할 수 없습니다." if language == "ko" else "There is not enough evidence to answer."

    selected = citations[:3]

    if language == "ko":
        lines = ["핵심 답변"]
        for citation in selected[:2]:
            text = _clean_rag_evidence_text(citation.get("text"), limit=230)
            if text:
                lines.append(f"- {text} [{citation.get('id')}]")

        lines.append("")
        lines.append("주요 근거")
        for citation in selected:
            section = citation.get("section") or "N/A"
            pages = citation.get("pages") or "N/A"
            node_name = citation.get("node_name") or "문서"
            lines.append(f"- {node_name} / section {section} / page {pages} [{citation.get('id')}]")

        lines.append("")
        lines.append("근거 부족")
        lines.append("- 위 근거 밖의 세부 사항은 확인하지 않았습니다.")
        return "\n".join(lines)

    lines = ["Answer"]
    for citation in selected[:2]:
        text = _clean_rag_evidence_text(citation.get("text"), limit=230)
        if text:
            lines.append(f"- {text} [{citation.get('id')}]")

    lines.append("")
    lines.append("Key Evidence")
    for citation in selected:
        section = citation.get("section") or "N/A"
        pages = citation.get("pages") or "N/A"
        node_name = citation.get("node_name") or "Document"
        lines.append(f"- {node_name} / section {section} / page {pages} [{citation.get('id')}]")

    lines.append("")
    lines.append("Limitations")
    lines.append("- Details outside the cited evidence were not verified.")
    return "\n".join(lines)


def _mark_rag_canceled(rag_job, redis_client, performance_metrics=None) -> dict:
    from document_ai.services.rag_cancel_service import clear_rag_cancel_signal

    rag_job.status = AIStatus.CANCELED
    rag_job.stage = RAGStage.CANCELED
    rag_job.stage_message = "사용자 요청으로 RAG 작업을 중단했습니다."
    rag_job.canceled_at = rag_job.canceled_at or timezone.now()
    rag_job.completed_at = rag_job.completed_at or timezone.now()
    update_fields = ["status", "stage", "stage_message", "canceled_at", "completed_at"]
    if performance_metrics is not None:
        rag_job.performance_metrics = performance_metrics
        update_fields.append("performance_metrics")
    rag_job.save(update_fields=update_fields)
    clear_rag_cancel_signal(rag_job.id, redis_client=redis_client)
    return {"status": "canceled", "job_id": rag_job.id}


def _build_generation_prompts(rag_job, *, skip_retrieval: bool, context_text: str) -> tuple[str, str, str, str]:
    from document_ai.rag.prompt_profiles import build_system_prompt

    prompt_route = "no_retrieval" if skip_retrieval else "document_rag"
    system_prompt = build_system_prompt(
        route=prompt_route,
        language=rag_job.language,
        workspace=rag_job.workspace,
    )
    if skip_retrieval:
        user_prompt = f"Question:\n{rag_job.question}\n\nAnswer without document citations."
    else:
        user_prompt = (
            f"Question:\n{rag_job.question}\n\n"
            f"Evidence:\n{context_text}\n\n"
            "Answer the question. If it is an informational question, answer strictly based on the evidence provided."
        )

    if rag_job.language == "ko":
        fewshot_user = (
            "Question:\n주요 지원 대책은 무엇인가요?\n\n"
            "Evidence:\n[1] 문서 A\nsection: 정책\npages: 1\n"
            "text: 정부는 공급 확대와 할인 지원을 병행해 가격 부담을 낮춘다.\n\n"
            "Answer concisely but sufficiently based on the evidence provided."
        )
        fewshot_assistant = "주요 지원 대책은 공급 확대와 할인 지원을 병행하는 것입니다 [1]."
    else:
        fewshot_user = (
            "Question:\nWhat are the main support measures?\n\n"
            "Evidence:\n[1] Document A\nsection: Policy\npages: 1\n"
            "text: The government will expand supply and provide discount support to reduce price pressure.\n\n"
            "Answer using only the evidence above."
        )
        fewshot_assistant = "The main support measures include supply expansion and discount support [1]."

    if skip_retrieval:
        fewshot_user = "Question:\n안녕 너는 뭐 할 수 있어?\n\nAnswer without document citations."
        fewshot_assistant = (
            "안녕하세요. 문서 검색, 요약, 근거 기반 질문 답변을 도와드릴 수 있습니다."
            if rag_job.language == "ko"
            else "Hello. I can help with document search, summaries, and evidence-based answers."
        )

    return system_prompt, user_prompt, fewshot_user, fewshot_assistant


# DEPRECATED - superseded by the ASGI path (document_ai.rag.async_generation.
# iter_rag_generation_events_async, driven from streaming.create_rag_streaming_response_async).
# RAGStreamView no longer calls this. Kept only because app/tests/test_rag_flow.py and
# test_performance_instrumentation.py still exercise prompt-building/cancellation/endpoint
# selection through it; do not wire this back into a live request path.
def generate_rag_response_sync(rag_job_id: int, *, on_token=None) -> dict:
    with capture_db_spans():
        return _generate_rag_response_sync(rag_job_id, on_token=on_token)


def _generate_rag_response_sync(rag_job_id: int, *, on_token=None) -> dict:
    import requests
    from redis import Redis

    from document_ai.models import RAGJob
    from document_ai.services.llm_endpoint_service import resolve_rag_llm_request_config
    from document_ai.services.rag_cancel_service import (
        is_rag_cancel_requested,
    )

    worker_started = time_module.perf_counter()
    try:
        rag_job = RAGJob.objects.select_related("search_job", "llm_endpoint").get(pk=rag_job_id)
    except RAGJob.DoesNotExist:
        logger.error("RAG job %s not found", rag_job_id)
        return {"status": "failed", "job_id": rag_job_id, "error": f"RAG job {rag_job_id} not found"}

    metrics = dict(rag_job.performance_metrics or {})
    put_metric(metrics, "trace_id", get_trace_id())

    def finish_metrics(**extra):
        for name, value in extra.items():
            put_metric(metrics, name, value)
        put_metric(metrics, "worker_total_ms", elapsed_ms(worker_started))
        completed_at = rag_job.completed_at or timezone.now()
        put_metric(metrics, "end_to_end_ms", datetime_delta_ms(rag_job.created_at, completed_at))
        return metrics

    skip_retrieval = not rag_job.search_job and (
        not rag_job.retrieval_required
        and rag_job.query_intent != QueryIntent.INFORMATION
    )

    if not skip_retrieval and (not rag_job.search_job or rag_job.search_job.status != AIStatus.COMPLETED):
        rag_job.status = AIStatus.FAILED
        rag_job.stage = RAGStage.FAILED
        rag_job.stage_message = "검색 결과가 완료되지 않아 답변을 생성할 수 없습니다."
        rag_job.completed_at = timezone.now()
        rag_job.error_message = "Search job is not completed."
        rag_job.performance_metrics = finish_metrics(failed=True)
        rag_job.save(update_fields=["status", "stage", "stage_message", "completed_at", "error_message", "performance_metrics"])
        return {"status": "failed", "job_id": rag_job_id, "error": rag_job.error_message}

    rag_job.status = AIStatus.PROCESSING
    rag_job.stage = RAGStage.GENERATING
    rag_job.stage_message = "검색된 근거를 바탕으로 답변을 생성하고 있습니다."
    rag_job.started_at = timezone.now()
    rag_job.error_message = ""
    if "queue_wait_ms" not in metrics:
        put_metric(metrics, "queue_wait_ms", datetime_delta_ms(rag_job.created_at, rag_job.started_at))
    if rag_job.search_job_id:
        put_metric(
            metrics,
            "generation_queue_wait_ms",
            datetime_delta_ms(rag_job.search_job.completed_at, rag_job.started_at),
        )
    rag_job.performance_metrics = metrics
    rag_job.save(update_fields=["status", "stage", "stage_message", "started_at", "error_message", "performance_metrics"])

    from document_ai.search.profiles import get_effective_generation_config

    llm_config = resolve_rag_llm_request_config(rag_job)
    generation_config = get_effective_generation_config(rag_job.workspace)
    redis_url = os.getenv("RAG_REDIS_URL", os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"))
    request_timeout = _get_positive_int_env("RAG_REQUEST_TIMEOUT", 300)
    max_tokens = generation_config["max_output_tokens"]
    evidence_limit = _get_positive_int_env("RAG_EVIDENCE_LIMIT", 5)
    context_max_chars = _get_positive_int_env("RAG_CONTEXT_MAX_CHARS", 3000)
    evidence_text_max_chars = _get_positive_int_env("RAG_EVIDENCE_TEXT_MAX_CHARS", 500)

    context_started = time_module.perf_counter()
    if skip_retrieval:
        context_text, citations = "", []
    else:
        context_text, citations = _build_rag_context(
            rag_job.search_job.results,
            evidence_limit=evidence_limit,
            context_max_chars=context_max_chars,
            evidence_text_max_chars=evidence_text_max_chars,
        )
    put_metric(metrics, "context_build_ms", elapsed_ms(context_started))
    metrics["citation_count"] = len(citations)
    metrics["context_chars"] = len(context_text)
    if not skip_retrieval and not _evidence_is_sufficient(citations, rag_job.search_job.threshold):
        rag_job.answer = _insufficient_evidence_answer(rag_job.language, scoped=bool(rag_job.node_ids))
        rag_job.status = AIStatus.COMPLETED
        rag_job.stage = RAGStage.COMPLETED
        rag_job.stage_message = "문서에서 충분한 근거를 찾지 못했습니다."
        rag_job.completed_at = timezone.now()
        rag_job.error_message = ""
        rag_job.citations = []
        rag_job.performance_metrics = finish_metrics(skipped_llm=True, output_chars=len(rag_job.answer))
        rag_job.save(update_fields=["answer", "status", "stage", "stage_message", "completed_at", "error_message", "citations", "performance_metrics"])
        return {"status": "insufficient_evidence", "job_id": rag_job_id, "citation_count": 0}

    system_prompt, user_prompt, fewshot_user, fewshot_assistant = _build_generation_prompts(
        rag_job,
        skip_retrieval=skip_retrieval,
        context_text=context_text,
    )

    redis_client = Redis.from_url(redis_url)

    try:
        if rag_job.status == AIStatus.CANCELED or is_rag_cancel_requested(rag_job.id, redis_client=redis_client):
            return _mark_rag_canceled(rag_job, redis_client, finish_metrics(canceled=True))

        request_url, payload = _build_llm_request(
            llm_config=llm_config,
            system_prompt=system_prompt,
            fewshot_user=fewshot_user,
            fewshot_assistant=fewshot_assistant,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            temperature=generation_config["temperature"],
            top_p=generation_config["top_p"],
        )
        llm_started = time_module.perf_counter()
        response = requests.post(
            request_url,
            json=payload,
            headers=llm_config.headers,
            timeout=(5, request_timeout),
            stream=True,
        )
        put_metric(metrics, "llm_connect_ms", elapsed_ms(llm_started))
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            response_text = response.text[:2000]
            raise RuntimeError(
                f"RAG LLM HTTP {response.status_code}: {response_text}"
            ) from exc
        raw_parts = []
        emitted_answer = ""
        last_cancel_check = 0.0
        first_token_at = None
        usage_holder: dict = {}
        for content in _iter_openai_responses_stream_content(response, usage_holder):
            if first_token_at is None:
                first_token_at = time_module.perf_counter()
                put_metric(metrics, "llm_ttft_ms", elapsed_ms(llm_started, first_token_at))
            raw_parts.append(content)
            if on_token is not None:
                # Normalize the accumulated output before publishing a delta.
                # Reasoning-tagged output stays empty until its final marker,
                # so internal reasoning is never sent to the client.
                candidate = _extract_llm_final_content("".join(raw_parts), fallback_to_raw=True)
                if candidate.startswith(emitted_answer):
                    safe_delta = candidate[len(emitted_answer):]
                    if safe_delta:
                        on_token(safe_delta)
                        emitted_answer = candidate
            now_monotonic = time_module.monotonic()
            if now_monotonic - last_cancel_check < 0.5:
                continue
            last_cancel_check = now_monotonic
            if is_rag_cancel_requested(rag_job.id, redis_client=redis_client):
                response.close()
                return _mark_rag_canceled(rag_job, redis_client, finish_metrics(canceled=True))

        raw_answer = "".join(raw_parts).strip()
        put_metric(metrics, "llm_total_ms", elapsed_ms(llm_started))
        generation_after_first_token_ms = None
        if first_token_at is not None:
            generation_after_first_token_ms = elapsed_ms(first_token_at)
            put_metric(metrics, "llm_generation_after_first_token_ms", generation_after_first_token_ms)

        # usage counts come from the serving LLM itself (stream_options.include_usage),
        # not a local tokenizer, so the resulting tokens/sec reflects the actual
        # decode speed of the runtime currently in use.
        output_token_count = usage_holder.get("completion_tokens") or usage_holder.get("output_tokens")
        input_token_count = usage_holder.get("prompt_tokens") or usage_holder.get("input_tokens")
        put_metric(metrics, "output_token_count", output_token_count)
        put_metric(metrics, "input_token_count", input_token_count)
        if output_token_count and generation_after_first_token_ms:
            put_metric(
                metrics,
                "output_tokens_per_second",
                round((output_token_count - 1) / (generation_after_first_token_ms / 1000), 3),
            )

        if is_rag_cancel_requested(rag_job.id, redis_client=redis_client):
            return _mark_rag_canceled(rag_job, redis_client, finish_metrics(canceled=True))

        rag_job.refresh_from_db(fields=["status", "stage"])
        if rag_job.status == AIStatus.CANCELED:
            from document_ai.services.rag_cancel_service import clear_rag_cancel_signal

            rag_job.performance_metrics = finish_metrics(canceled=True)
            rag_job.save(update_fields=["performance_metrics"])
            clear_rag_cancel_signal(rag_job.id, redis_client=redis_client)
            return {"status": "canceled", "job_id": rag_job_id}
        answer = _normalize_rag_answer(raw_answer)
        if not answer:
            logger.warning(
                "RAG answer normalization returned empty output: job_id=%s raw_preview=%r",
                rag_job.id,
                raw_answer[:500],
            )
            answer = _fallback_rag_answer(citations, rag_job.language)
            rag_job.error_message = f"LLM returned no final answer after normalization. raw_preview={raw_answer[:1000]}"
        else:
            rag_job.error_message = ""

        if on_token is not None and answer.startswith(emitted_answer):
            final_delta = answer[len(emitted_answer):]
            if final_delta:
                on_token(final_delta)

        rag_job.answer = answer
        rag_job.citations = citations
        rag_job.status = AIStatus.COMPLETED
        rag_job.stage = RAGStage.COMPLETED
        rag_job.stage_message = "답변 생성이 완료되었습니다."
        rag_job.completed_at = timezone.now()
        rag_job.performance_metrics = finish_metrics(output_chars=len(answer))
        rag_job.save(update_fields=["answer", "citations", "status", "stage", "stage_message", "completed_at", "error_message", "performance_metrics"])
        logger.info("RAG generation completed: job_id=%s performance=%s", rag_job.id, rag_job.performance_metrics)

        return {"status": "success", "job_id": rag_job_id, "citation_count": len(citations)}
    except requests.Timeout as exc:
        rag_job.status = AIStatus.FAILED
        rag_job.stage = RAGStage.FAILED
        rag_job.stage_message = "모델 응답 시간이 초과되었습니다."
        rag_job.completed_at = timezone.now()
        rag_job.error_message = f"RAG request timed out after {request_timeout}s"
        rag_job.performance_metrics = finish_metrics(failed=True, timeout=True)
        rag_job.save(update_fields=["status", "stage", "stage_message", "completed_at", "error_message", "performance_metrics"])
        logger.error("RAG LLM timeout after %ss: %s", request_timeout, exc)
        return {"status": "failed", "job_id": rag_job_id, "error": rag_job.error_message}
    except Exception as exc:
        rag_job.status = AIStatus.FAILED
        rag_job.stage = RAGStage.FAILED
        rag_job.stage_message = "답변 생성 중 오류가 발생했습니다."
        rag_job.completed_at = timezone.now()
        rag_job.error_message = str(exc)
        rag_job.performance_metrics = finish_metrics(failed=True)
        rag_job.save(update_fields=["status", "stage", "stage_message", "completed_at", "error_message", "performance_metrics"])
        logger.exception("RAG LLM error: job_id=%s", rag_job_id)
        return {"status": "failed", "job_id": rag_job_id, "error": str(exc)}
