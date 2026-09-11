from __future__ import annotations

import gc
import logging
from typing import Any, Dict

from document_ai.parsers.config import get_embedding_max_tokens

from .base import EmbeddingProviderSpec, EmbeddingResult
from .bgem3 import (
    clear_cuda_cache as _clear_cuda_cache,
    coerce_dense_vector as _coerce_dense_vector,
    validate_text as _validate_text,
)

logger = logging.getLogger(__name__)

_ST_MODEL_CACHE: Dict[str, Any] = {}
_torch_module = None
_torch_import_attempted = False
_DEVICE = None


def _ensure_torch():
    global _torch_module, _torch_import_attempted, _DEVICE
    if _torch_import_attempted:
        return _torch_module
    _torch_import_attempted = True
    try:
        import torch as torch_module
    except ImportError:
        _torch_module = None
    else:
        _torch_module = torch_module
        _DEVICE = torch_module.device("cuda" if torch_module.cuda.is_available() else "cpu")
    return _torch_module


def get_sentence_transformer_model(model_name: str, model_revision: str = ""):
    cache_key = f"{model_name}@{model_revision or 'default'}"
    model = _ST_MODEL_CACHE.get(cache_key)
    if model is None:
        _ensure_torch()
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is required for sentence_transformers embeddings."
            ) from exc

        kwargs: dict[str, Any] = {}
        if model_revision:
            kwargs["revision"] = model_revision

        logger.info(
            "Loading sentence_transformers model: %s (revision=%s)",
            model_name,
            model_revision or "default",
        )
        model = SentenceTransformer(model_name, **kwargs)
        if _DEVICE is not None:
            model = model.to(_DEVICE)
        _ST_MODEL_CACHE[cache_key] = model
    return model


class SentenceTransformersEmbeddingProvider:
    backend = "sentence_transformers"

    def __init__(
        self,
        *,
        model_name: str,
        model_revision: str = "",
        dimension: int | None = None,
        normalize_embeddings: bool = True,
        query_prefix: str = "",
        document_prefix: str = "",
        default_distance: str = "inner_product",
    ):
        self.spec = EmbeddingProviderSpec(
            backend=self.backend,
            model_name=model_name,
            model_revision=model_revision,
            dimension=dimension,
            supports_sparse=False,
            supports_dense=True,
            default_distance=default_distance,
            normalize_embeddings=normalize_embeddings,
            query_prefix=query_prefix,
            document_prefix=document_prefix,
        )

    def embed_document(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        normalized_text = _validate_text(text)
        if self.spec.document_prefix:
            normalized_text = f"{self.spec.document_prefix}{normalized_text}"
        return self._embed_single(normalized_text, max_length=max_length)

    def embed_query(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        normalized_text = _validate_text(text)
        if self.spec.query_prefix:
            normalized_text = f"{self.spec.query_prefix}{normalized_text}"
        return self._embed_single(normalized_text, max_length=max_length)

    def embed_documents(self, texts: list[str], *, max_length: int | None = None) -> list[EmbeddingResult]:
        if not texts:
            return []
        prepared_texts = []
        for t in texts:
            normalized = _validate_text(t)
            if self.spec.document_prefix:
                normalized = f"{self.spec.document_prefix}{normalized}"
            prepared_texts.append(normalized)
        return self._embed_batch(prepared_texts, max_length=max_length)

    def healthcheck(self) -> dict:
        result = self.embed_query("healthcheck")
        dim = len(result.dense_vector)
        return {
            "status": "ready",
            "backend": self.backend,
            "model_name": self.spec.model_name,
            "dimension": dim,
            "supports_sparse": False,
        }

    def _embed_single(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        results = self._embed_batch([text], max_length=max_length)
        if not results:
            raise RuntimeError("SentenceTransformer produced no embeddings.")
        return results[0]

    def _embed_batch(self, texts: list[str], *, max_length: int | None = None) -> list[EmbeddingResult]:
        model = get_sentence_transformer_model(
            self.spec.model_name,
            self.spec.model_revision,
        )
        torch = _ensure_torch()

        # Update model max_seq_length if requested
        if max_length is not None and hasattr(model, "max_seq_length"):
            model.max_seq_length = max_length

        try:
            embeddings = model.encode(
                texts,
                batch_size=len(texts),
                normalize_embeddings=self.spec.normalize_embeddings,
                show_progress_bar=False,
            )
            if hasattr(embeddings, "tolist"):
                embeddings = embeddings.tolist()

            results: list[EmbeddingResult] = []
            for row in embeddings:
                dense_vector = _coerce_dense_vector(row)
                results.append(
                    EmbeddingResult(
                        dense_vector=dense_vector,
                        sparse_vector={},
                        model_name=self.spec.model_name,
                        backend=self.spec.backend,
                        dimension=len(dense_vector),
                    )
                )
            return results

        except Exception as exc:
            if torch is not None and isinstance(exc, getattr(torch.cuda, "OutOfMemoryError", tuple())):
                logger.exception(
                    "CUDA OOM during sentence_transformers embedding. model=%s, batch_size=%s",
                    self.spec.model_name,
                    len(texts),
                )
                _clear_cuda_cache()
                gc.collect()
                raise RuntimeError(
                    f"GPU OOM while embedding with {self.spec.model_name}"
                ) from exc
            raise
        finally:
            gc.collect()
            if torch is not None and _DEVICE is not None and getattr(_DEVICE, "type", "") == "cuda":
                _clear_cuda_cache()
