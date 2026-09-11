from __future__ import annotations

import gc
import logging
import math
from typing import Any, Dict

from document_ai.parsers.config import get_embedding_max_tokens

from .base import EmbeddingProviderSpec, EmbeddingResult

logger = logging.getLogger(__name__)

_MODEL_CACHE: Dict[str, Any] = {}

# torch (~500MB RSS just to import) is deferred to first actual use instead of
# module import time. Only the process that owns the model (embedding-executor's
# gunicorn worker, gated by DOTORI_EMBEDDING_MODEL_PROCESS) ever calls
# get_bgem3_model()/_embed(); every other process that merely imports this
# module for validate_text()/normalize_sparse_vector() etc. must not pay for
# torch it will never use.
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
    except ImportError:  # pragma: no cover - exercised only when torch is unavailable
        _torch_module = None
    else:
        _torch_module = torch_module
        _DEVICE = torch_module.device("cuda" if torch_module.cuda.is_available() else "cpu")
    return _torch_module


def clear_cuda_cache() -> None:
    torch = _ensure_torch()
    if torch is not None and _DEVICE is not None and _DEVICE.type == "cuda":
        torch.cuda.empty_cache()
        if torch.cuda.is_available():
            torch.cuda.ipc_collect()


def get_bgem3_model(model_name: str, model_revision: str = ""):
    cache_key = f"{model_name}@{model_revision or 'default'}"
    model = _MODEL_CACHE.get(cache_key)
    if model is None:
        _ensure_torch()
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            raise RuntimeError(
                "FlagEmbedding is required for bgem3_hybrid embeddings."
            ) from exc

        resolved_model_path = model_name
        if model_revision and model_revision not in {"legacy", "main"}:
            from huggingface_hub import snapshot_download

            resolved_model_path = snapshot_download(
                repo_id=model_name,
                revision=model_revision,
            )

        model = BGEM3FlagModel(
            resolved_model_path,
            use_fp16=(_DEVICE is not None and _DEVICE.type == "cuda"),
            normalize_embeddings=True,
        )
        _MODEL_CACHE[cache_key] = model
    return model


def validate_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Text not String")

    normalized = text.strip()
    if not normalized:
        raise ValueError("Text is Empty")

    return normalized


def normalize_sparse_vector(weights: dict[str, float]) -> dict[str, float]:
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


def check_normalized(vector: list[float]) -> list[float]:
    if not vector:
        return []

    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 0:
        return vector

    if not math.isclose(norm, 1.0, rel_tol=1e-5):
        return [v / norm for v in vector]

    return vector


def coerce_sparse_vector(raw_sparse: dict) -> dict[str, float]:
    sparse_vector = {
        str(key): float(value)
        for key, value in (raw_sparse or {}).items()
        if float(value) > 0
    }
    return normalize_sparse_vector(sparse_vector)


def coerce_dense_vector(raw_dense) -> list[float]:
    if hasattr(raw_dense, "tolist"):
        raw_dense = raw_dense.tolist()

    if raw_dense and isinstance(raw_dense[0], list):
        raw_dense = raw_dense[0]

    vector = [float(value) for value in raw_dense]
    if not vector:
        raise RuntimeError("Embedding vector is empty")
    return check_normalized(vector)


class BGEM3HybridProvider:
    backend = "bgem3_hybrid"

    def __init__(
        self,
        *,
        model_name: str,
        model_revision: str = "",
        dimension: int | None = None,
        normalize_embeddings: bool = True,
        query_prefix: str = "",
        document_prefix: str = "",
    ):
        self.spec = EmbeddingProviderSpec(
            backend=self.backend,
            model_name=model_name,
            model_revision=model_revision,
            dimension=dimension,
            supports_sparse=True,
            default_distance="inner_product",
            normalize_embeddings=normalize_embeddings,
            query_prefix=query_prefix,
            document_prefix=document_prefix,
        )

    def embed_document(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        return self._embed(
            f"{self.spec.document_prefix}{text}", max_length=max_length
        )

    def embed_query(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        return self._embed(f"{self.spec.query_prefix}{text}", max_length=max_length)

    def embed_documents(self, texts: list[str], *, max_length: int | None = None) -> list[EmbeddingResult]:
        prefixed_texts = [f"{self.spec.document_prefix}{text}" for text in texts]
        return self._embed_batch(prefixed_texts, max_length=max_length)

    def healthcheck(self) -> dict:
        result = self.embed_query("embedding healthcheck", max_length=32)
        return {
            "backend": result.backend,
            "model_name": result.model_name,
            "dimension": result.dimension,
            "supports_sparse": bool(result.sparse_vector),
        }

    def _embed(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        normalized_text = validate_text(text)
        resolved_max_length = max_length or get_embedding_max_tokens()
        model = get_bgem3_model(
            self.spec.model_name,
            self.spec.model_revision,
        )
        # get_bgem3_model() already resolved torch/_DEVICE above; this just
        # gets the cached reference for the except/finally blocks below.
        torch = _ensure_torch()

        try:
            output = model.encode(
                [normalized_text],
                batch_size=1,
                max_length=resolved_max_length,
                return_dense=True,
                return_sparse=True,
                return_colbert_vecs=False,
            )

            dense_vector = coerce_dense_vector(output["dense_vecs"])
            lexical_weights = output.get("lexical_weights") or [{}]
            if isinstance(lexical_weights, list):
                lexical_weights = lexical_weights[0] if lexical_weights else {}
            sparse_vector = coerce_sparse_vector(lexical_weights)

            return EmbeddingResult(
                dense_vector=dense_vector,
                sparse_vector=sparse_vector,
                model_name=self.spec.model_name,
                backend=self.spec.backend,
                dimension=len(dense_vector),
            )

        except Exception as exc:
            if torch is not None and isinstance(exc, torch.cuda.OutOfMemoryError):
                logger.exception(
                    "CUDA OOM during embedding. model=%s, backend=%s, max_length=%s, device=%s",
                    self.spec.model_name,
                    self.spec.backend,
                    resolved_max_length,
                    _DEVICE,
                )
                clear_cuda_cache()
                gc.collect()
                raise RuntimeError(
                    "GPU OOM while embedding text "
                    f"(model={self.spec.model_name}, backend={self.spec.backend}, max_length={resolved_max_length})"
                ) from exc
            raise
        finally:
            gc.collect()
            if torch is not None and _DEVICE is not None and _DEVICE.type == "cuda":
                torch.cuda.empty_cache()

    def _embed_batch(self, texts: list[str], *, max_length: int | None = None) -> list[EmbeddingResult]:
        # One model.encode() call for N texts instead of N separate calls --
        # amortizes tokenization/kernel-launch overhead across a document's
        # chunks. Query embedding never goes through this path (latency
        # sensitive, always single); only document micro-batching uses it.
        normalized_texts = [validate_text(text) for text in texts]
        resolved_max_length = max_length or get_embedding_max_tokens()
        model = get_bgem3_model(
            self.spec.model_name,
            self.spec.model_revision,
        )
        torch = _ensure_torch()

        try:
            output = model.encode(
                normalized_texts,
                batch_size=len(normalized_texts),
                max_length=resolved_max_length,
                return_dense=True,
                return_sparse=True,
                return_colbert_vecs=False,
            )

            dense_rows = output["dense_vecs"]
            if hasattr(dense_rows, "tolist"):
                dense_rows = dense_rows.tolist()
            lexical_weights = output.get("lexical_weights") or []

            results: list[EmbeddingResult] = []
            for index in range(len(normalized_texts)):
                dense_vector = coerce_dense_vector(dense_rows[index])
                sparse_vector = coerce_sparse_vector(
                    lexical_weights[index] if index < len(lexical_weights) else {}
                )
                results.append(
                    EmbeddingResult(
                        dense_vector=dense_vector,
                        sparse_vector=sparse_vector,
                        model_name=self.spec.model_name,
                        backend=self.spec.backend,
                        dimension=len(dense_vector),
                    )
                )
            return results

        except Exception as exc:
            if torch is not None and isinstance(exc, torch.cuda.OutOfMemoryError):
                logger.exception(
                    "CUDA OOM during batch embedding. model=%s, backend=%s, max_length=%s, batch_size=%s, device=%s",
                    self.spec.model_name,
                    self.spec.backend,
                    resolved_max_length,
                    len(normalized_texts),
                    _DEVICE,
                )
                clear_cuda_cache()
                gc.collect()
                raise RuntimeError(
                    "GPU OOM while embedding batch "
                    f"(model={self.spec.model_name}, backend={self.spec.backend}, "
                    f"max_length={resolved_max_length}, batch_size={len(normalized_texts)})"
                ) from exc
            raise
        finally:
            gc.collect()
            if torch is not None and _DEVICE is not None and _DEVICE.type == "cuda":
                torch.cuda.empty_cache()
