import gc
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict

from django.conf import settings

try:
    import torch
except ImportError:  # pragma: no cover - exercised only when torch is unavailable
    torch = None

from document_ai.parsers.config import (
    get_embedding_backend,
    get_embedding_max_tokens,
)

logger = logging.getLogger(__name__)

_MODEL_CACHE: Dict[str, Any] = {}
if torch is not None:
    _DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
else:
    _DEVICE = None


@dataclass
class EmbeddingResult:
    dense_vector: list[float]
    sparse_vector: dict[str, float]


def _clear_cuda_cache() -> None:
    if torch is not None and _DEVICE is not None and _DEVICE.type == "cuda":
        torch.cuda.empty_cache()
        if torch.cuda.is_available():
            torch.cuda.ipc_collect()


def _get_bgem3_model(model_name: str):
    model = _MODEL_CACHE.get(model_name)
    if model is None:
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            raise RuntimeError(
                "FlagEmbedding is required for bgem3_hybrid embeddings."
            ) from exc

        model = BGEM3FlagModel(
            model_name,
            use_fp16=(_DEVICE is not None and _DEVICE.type == "cuda"),
            normalize_embeddings=True,
        )
        _MODEL_CACHE[model_name] = model
    return model


def _validate_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Text not String")

    normalized = text.strip()
    if not normalized:
        raise ValueError("Text is Empty")

    return normalized


def _normalize_sparse_vector(weights: dict[str, float]) -> dict[str, float]:
    if not weights:
        return {}

    norm = math.sqrt(sum(value * value for value in weights.values()))
    if norm <= 0:
        return {}

    return {
        key: value / norm
        for key, value in weights.items()
        if value > 0
    }

    
def _check_normalized(vector: list[float]) -> list[float]:
    if not vector:
        return []

    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 0:
        return vector

    if not math.isclose(norm, 1.0, rel_tol=1e-5):
        return [v / norm for v in vector]

    return vector


def _coerce_sparse_vector(raw_sparse: dict) -> dict[str, float]:
    sparse_vector = {
        str(key): float(value)
        for key, value in (raw_sparse or {}).items()
        if float(value) > 0
    }
    return _normalize_sparse_vector(sparse_vector)


def _coerce_dense_vector(raw_dense) -> list[float]:
    if hasattr(raw_dense, "tolist"):
        raw_dense = raw_dense.tolist()

    if raw_dense and isinstance(raw_dense[0], list):
        raw_dense = raw_dense[0]

    vector = [float(value) for value in raw_dense]
    if not vector:
        raise RuntimeError("Embedding vector is empty")
    return _check_normalized(vector)


def _embed_with_bgem3_hybrid(
    text: str,
    model_name: str,
    max_length: int,
) -> EmbeddingResult:
    model = _get_bgem3_model(model_name)

    try:
        output = model.encode(
            [text],
            batch_size=1,
            max_length=max_length,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )

        dense_vector = _coerce_dense_vector(output["dense_vecs"])
        lexical_weights = output.get("lexical_weights") or [{}]
        if isinstance(lexical_weights, list):
            lexical_weights = lexical_weights[0] if lexical_weights else {}
        sparse_vector = _coerce_sparse_vector(lexical_weights)

        return EmbeddingResult(
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
        )

    except Exception as exc:
        if torch is not None and isinstance(exc, torch.cuda.OutOfMemoryError):
            logger.exception(
                "CUDA OOM during embedding. model=%s, backend=bgem3_hybrid, max_length=%s, device=%s",
                model_name,
                max_length,
                _DEVICE,
            )
            _clear_cuda_cache()
            gc.collect()
            raise RuntimeError(
                f"GPU OOM while embedding text (model={model_name}, backend=bgem3_hybrid, max_length={max_length})"
            ) from exc
        raise
    finally:
        gc.collect()
        if torch is not None and _DEVICE is not None and _DEVICE.type == "cuda":
            torch.cuda.empty_cache()


def bge_m3_embedder(
    text: str,
    model_name: str = "BAAI/bge-m3",
    max_length: int | None = None,
    backend: str | None = None,
) -> EmbeddingResult:
    """
    Generate dense and sparse embeddings from BGE-M3 using FlagEmbedding.
    """
    normalized_text = _validate_text(text)
    resolved_backend = backend or get_embedding_backend()
    resolved_max_length = max_length or get_embedding_max_tokens()

    if resolved_backend == "bgem3_hybrid":
        return _embed_with_bgem3_hybrid(
            text=normalized_text,
            model_name=model_name,
            max_length=resolved_max_length,
        )

    raise ValueError(f"Unsupported embedding backend: {resolved_backend}")


def embed_document(
    text: str,
    model_name: str = "BAAI/bge-m3",
    max_length: int | None = None,
    backend: str | None = None,
) -> EmbeddingResult:
    """
    Embed a stored document chunk. This is the public entrypoint for chunk
    embeddings so document-side policy can evolve independently.
    """
    return bge_m3_embedder(
        text=text,
        model_name=model_name,
        max_length=max_length,
        backend=backend,
    )


def embed_query(
    query: str,
    model_name: str = "BAAI/bge-m3",
    max_length: int | None = None,
    backend: str | None = None,
) -> EmbeddingResult:
    """
    Embed a user search query. It currently uses the same BGE-M3 encoder as
    document chunks, but keeps query-side token limits and future rewrite or
    instruction policy separate.
    """
    resolved_max_length = max_length or getattr(settings, "QUERY_EMBEDDING_MAX_TOKENS", None)
    return bge_m3_embedder(
        text=query,
        model_name=model_name,
        max_length=resolved_max_length,
        backend=backend,
    )
