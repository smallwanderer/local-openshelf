from __future__ import annotations

from django.conf import settings

from document_ai.parsers.config import (
    get_embedding_backend,
    get_embedding_max_tokens,
    get_embedding_model,
)

from .providers.bgem3 import (
    check_normalized as _check_normalized,
    clear_cuda_cache as _clear_cuda_cache,
    coerce_dense_vector as _coerce_dense_vector,
    coerce_sparse_vector as _coerce_sparse_vector,
    get_bgem3_model as _get_bgem3_model,
    normalize_sparse_vector as _normalize_sparse_vector,
    validate_text as _validate_text,
)
from .providers.base import EmbeddingResult
from .registry import get_embedding_provider
from document_ai.services.embedding_runtime_config import EmbeddingRuntimeSnapshot


def _embed_with_bgem3_hybrid(
    text: str,
    model_name: str,
    max_length: int,
    runtime: EmbeddingRuntimeSnapshot | None = None,
) -> EmbeddingResult:
    provider = get_embedding_provider(
        backend="bgem3_hybrid",
        model_name=model_name,
        runtime=runtime,
    )
    return provider.embed_document(text, max_length=max_length)


def bge_m3_embedder(
    text: str,
    model_name: str | None = None,
    max_length: int | None = None,
    backend: str | None = None,
    runtime: EmbeddingRuntimeSnapshot | None = None,
) -> EmbeddingResult:
    normalized_text = _validate_text(text)
    resolved_backend = backend or (
        runtime.provider if runtime else get_embedding_backend()
    )
    resolved_model = model_name or (
        runtime.model_id if runtime else get_embedding_model()
    )
    resolved_max_length = max_length or get_embedding_max_tokens()

    if resolved_backend == "bgem3_hybrid":
        result = _embed_with_bgem3_hybrid(
            text=normalized_text,
            model_name=resolved_model,
            max_length=resolved_max_length,
            runtime=runtime,
        )
    else:
        provider = get_embedding_provider(
            backend=resolved_backend,
            model_name=resolved_model,
            runtime=runtime,
        )
        result = provider.embed_document(normalized_text, max_length=resolved_max_length)

    # Fallback lexical sparse encoding if provider returned an empty sparse vector
    if not result.sparse_vector:
        from .sparse import get_lexical_sparse_encoder
        result.sparse_vector = get_lexical_sparse_encoder().encode(normalized_text, is_query=False)

    return result


def embed_document(
    text: str,
    model_name: str | None = None,
    max_length: int | None = None,
    backend: str | None = None,
    runtime: EmbeddingRuntimeSnapshot | None = None,
) -> EmbeddingResult:
    return bge_m3_embedder(
        text=text,
        model_name=model_name,
        max_length=max_length,
        backend=backend,
        runtime=runtime,
    )


def embed_documents(
    texts: list[str],
    model_name: str | None = None,
    max_length: int | None = None,
    backend: str | None = None,
    runtime: EmbeddingRuntimeSnapshot | None = None,
) -> list[EmbeddingResult]:
    resolved_backend = backend or (
        runtime.provider if runtime else get_embedding_backend()
    )
    resolved_model = model_name or (
        runtime.model_id if runtime else get_embedding_model()
    )
    resolved_max_length = max_length or get_embedding_max_tokens()

    provider = get_embedding_provider(
        backend=resolved_backend,
        model_name=resolved_model,
        runtime=runtime,
    )
    results = provider.embed_documents(texts, max_length=resolved_max_length)

    # Fallback lexical sparse encoding for items with empty sparse vector
    from .sparse import get_lexical_sparse_encoder
    encoder = None
    for text, result in zip(texts, results):
        if not result.sparse_vector:
            if encoder is None:
                encoder = get_lexical_sparse_encoder()
            result.sparse_vector = encoder.encode(text, is_query=False)

    return results


def embed_query(
    query: str,
    model_name: str | None = None,
    max_length: int | None = None,
    backend: str | None = None,
    runtime: EmbeddingRuntimeSnapshot | None = None,
) -> EmbeddingResult:
    from .registry import _is_embedding_model_process
    from .inference_context import admitted

    if _is_embedding_model_process() and not admitted.get():
        from .internal_views import run_embedding_with_admission
        return run_embedding_with_admission(
            "query", text=query, model_name=model_name, backend=backend,
            runtime=runtime, max_length=max_length,
        )[0]
    resolved_max_length = max_length or getattr(settings, "SEARCH_QUERY_EMBEDDING_MAX_TOKENS", None)
    if resolved_max_length is not None:
        resolved_max_length = min(resolved_max_length, get_embedding_max_tokens())
    resolved_backend = backend or (
        runtime.provider if runtime else get_embedding_backend()
    )
    resolved_model = model_name or (
        runtime.model_id if runtime else get_embedding_model()
    )

    # Always resolve a provider and call its embed_query() directly. Routing
    # bgem3_hybrid through bge_m3_embedder()/_embed_with_bgem3_hybrid() here
    # would call provider.embed_document() for a query — harmless today only
    # because this deployment's query_prefix/document_prefix are both "".
    provider = get_embedding_provider(
        backend=resolved_backend,
        model_name=resolved_model,
        runtime=runtime,
    )
    result = provider.embed_query(query, max_length=resolved_max_length)

    # Fallback lexical sparse encoding if query has empty sparse vector
    if not result.sparse_vector:
        from .sparse import get_lexical_sparse_encoder
        result.sparse_vector = get_lexical_sparse_encoder().encode(query, is_query=True)

    return result
