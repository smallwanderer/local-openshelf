from typing import TYPE_CHECKING, Optional
from functools import lru_cache

from django.conf import settings

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter
    from docling.chunking import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    from docling_core.transforms.serializer.base import BaseSerializerProvider


def get_embedding_model():
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    return get_active_embedding_runtime().model_id

def get_embedding_backend() -> str:
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    return get_active_embedding_runtime().provider

def get_embedding_dimension() -> int | None:
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    return get_active_embedding_runtime().dimension

def get_embedding_sparse_enabled() -> bool:
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    return get_active_embedding_runtime().supports_sparse

def get_embedding_store() -> str:
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    return get_active_embedding_runtime().store

def get_parser_backend() -> str:
    return getattr(settings, "DOCUMENT_PARSER_BACKEND", "docling")

def get_parser_tokenizer_id() -> str:
    try:
        from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

        runtime = get_active_embedding_runtime()
        if runtime.tokenizer_id and runtime.catalog_id != "legacy-env":
            return runtime.tokenizer_id
    except Exception:
        pass
    return getattr(settings, "PARSER_TOKENIZER_ID", "BAAI/bge-m3")

def get_parser_tokenizer_revision() -> str | None:
    try:
        from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

        runtime = get_active_embedding_runtime()
        if runtime.catalog_id != "legacy-env":
            rev = runtime.tokenizer_revision
            # ``external`` records that the embedding service owns the model
            # revision.  Hugging Face must resolve the tokenizer at its default
            # revision, rather than inheriting the legacy BGE-M3 pin below.
            return rev if rev and rev not in {"", "legacy", "main", "external"} else None
    except Exception:
        pass
    value = getattr(settings, "PARSER_TOKENIZER_REVISION", "5617a9f")
    return value if value and value != "legacy" else None

def get_embedding_token_headroom() -> int:
    return getattr(settings, "EMBEDDING_TOKEN_HEADROOM", 128)

def get_embedding_max_tokens() -> int:
    try:
        from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

        runtime = get_active_embedding_runtime()
        if getattr(runtime, "max_tokens", None) and runtime.catalog_id != "legacy-env":
            return int(runtime.max_tokens)
    except Exception:
        pass
    return getattr(settings, "EMBEDDING_MAX_TOKENS", 1280)

def get_embedding_document_batch_max_chunks() -> int:
    return getattr(settings, "EMBEDDING_DOCUMENT_BATCH_MAX_CHUNKS", 4)

def get_embedding_document_batch_max_tokens() -> int:
    return getattr(settings, "EMBEDDING_DOCUMENT_BATCH_MAX_TOKENS", 3200)

def get_chunk_max_tokens() -> int:
    chunk_max_tokens = get_embedding_max_tokens() - get_embedding_token_headroom()
    if chunk_max_tokens <= 0:
        raise ValueError(
            "EMBEDDING_MAX_TOKENS must be greater than "
            "EMBEDDING_TOKEN_HEADROOM."
        )
    return chunk_max_tokens

def get_max_tokens() -> int:
    return get_chunk_max_tokens()


@lru_cache(maxsize=1)
def get_raw_tokenizer():
    import logging
    from transformers import AutoTokenizer
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    logger = logging.getLogger(__name__)
    runtime = get_active_embedding_runtime()
    revision = (
        runtime.tokenizer_revision
        if runtime.tokenizer_revision not in {"", "legacy", "main", "external"}
        else None
    )
    try:
        return AutoTokenizer.from_pretrained(
            runtime.tokenizer_id,
            revision=revision,
        )
    except Exception as exc:
        fallback_id = getattr(settings, "PARSER_TOKENIZER_ID", "BAAI/bge-m3")
        fallback_rev = getattr(settings, "PARSER_TOKENIZER_REVISION", "5617a9f")
        rev = fallback_rev if fallback_rev and fallback_rev != "legacy" else None
        logger.warning(
            "Failed to load raw tokenizer '%s' (%s). Falling back to '%s': %s",
            runtime.tokenizer_id,
            revision,
            fallback_id,
            exc,
        )
        return AutoTokenizer.from_pretrained(
            fallback_id,
            revision=rev,
        )


@lru_cache(maxsize=1)
def get_hf_tokenizer() -> "HuggingFaceTokenizer":
    import logging
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    from transformers import AutoTokenizer

    logger = logging.getLogger(__name__)
    tokenizer_id = get_parser_tokenizer_id()
    tokenizer_revision = get_parser_tokenizer_revision()

    try:
        hf_tok = AutoTokenizer.from_pretrained(
            tokenizer_id,
            revision=tokenizer_revision,
        )
    except Exception as exc:
        fallback_id = getattr(settings, "PARSER_TOKENIZER_ID", "BAAI/bge-m3")
        fallback_rev = getattr(settings, "PARSER_TOKENIZER_REVISION", "5617a9f")
        rev = fallback_rev if fallback_rev and fallback_rev != "legacy" else None
        logger.warning(
            "Failed to load tokenizer '%s' (%s) for parser. Falling back to '%s': %s",
            tokenizer_id,
            tokenizer_revision,
            fallback_id,
            exc,
        )
        hf_tok = AutoTokenizer.from_pretrained(
            fallback_id,
            revision=rev,
        )

    return HuggingFaceTokenizer(
        tokenizer=hf_tok,
        max_tokens=get_chunk_max_tokens(),
    )


def clear_parser_tokenizer_cache() -> None:
    get_raw_tokenizer.cache_clear()
    get_hf_tokenizer.cache_clear()
    get_converter.cache_clear()


@lru_cache(maxsize=1)
def get_converter() -> "DocumentConverter":
    from docling.document_converter import DocumentConverter

    return DocumentConverter()


def get_hybrid_hf_chunker(
    serializer_provider: Optional["BaseSerializerProvider"] = None,
) -> "HybridChunker":
    from docling.chunking import HybridChunker

    return HybridChunker(
        tokenizer=get_hf_tokenizer(),
        # [max_tokens] Optional, default is derived from tokenizer for HF case
        # max_tokens=MAX_TOKENS, 
        merge_peers=True,
        serializer_provider=serializer_provider,
    )
