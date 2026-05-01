import gc
import logging
from typing import Dict, Tuple

import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer

from document_ai.parsers.config import (
    get_embedding_backend,
    get_embedding_max_tokens,
)

logger = logging.getLogger(__name__)

_TOKENIZER_CACHE: Dict[str, AutoTokenizer] = {}
_MODEL_CACHE: Dict[str, AutoModel] = {}
_DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


# TODO: Review batch embedding once retrieval quality is stable.
# TODO: Separate embedding inference into its own service if AI traffic grows.


def _clear_cuda_cache() -> None:
    if _DEVICE.type == "cuda":
        torch.cuda.empty_cache()
        if torch.cuda.is_available():
            torch.cuda.ipc_collect()


def _get_model_and_tokenizer(model_name: str) -> Tuple[AutoModel, AutoTokenizer]:
    tokenizer = _TOKENIZER_CACHE.get(model_name)
    model = _MODEL_CACHE.get(model_name)

    if tokenizer is None:
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        _TOKENIZER_CACHE[model_name] = tokenizer

    if model is None:
        model = AutoModel.from_pretrained(model_name)
        model.to(_DEVICE)
        model.eval()
        _MODEL_CACHE[model_name] = model

    return model, tokenizer


def _validate_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Text not String")

    normalized = text.strip()
    if not normalized:
        raise ValueError("Text is Empty")

    return normalized


def _tokenize_text(
    tokenizer: AutoTokenizer,
    text: str,
    max_length: int,
    truncation: bool,
    padding: bool,
) -> Dict[str, torch.Tensor]:
    inputs = tokenizer(
        text,
        return_tensors="pt",
        truncation=truncation,
        padding=padding,
        max_length=max_length,
    )
    return {key: value.to(_DEVICE) for key, value in inputs.items()}


def _mean_pool(last_hidden_state: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    expanded_mask = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
    masked_embeddings = last_hidden_state * expanded_mask
    token_sums = masked_embeddings.sum(dim=1)
    token_counts = expanded_mask.sum(dim=1).clamp(min=1e-9)
    return token_sums / token_counts


def _embed_with_hf_mean_pooling(
    text: str,
    model_name: str,
    max_length: int,
    truncation: bool,
    padding: bool,
    normalize: bool,
) -> list[float]:
    model, tokenizer = _get_model_and_tokenizer(model_name)
    inputs = outputs = pooled = vector_tensor = None

    try:
        inputs = _tokenize_text(
            tokenizer=tokenizer,
            text=text,
            max_length=max_length,
            truncation=truncation,
            padding=padding,
        )

        with torch.inference_mode():
            outputs = model(**inputs)
            pooled = _mean_pool(outputs.last_hidden_state, inputs["attention_mask"])

            if normalize:
                pooled = F.normalize(pooled, p=2, dim=1)

        vector_tensor = pooled[0].detach().cpu()
        vector = vector_tensor.tolist()
        if not vector:
            raise RuntimeError("Embedding vector is empty")
        return vector

    except torch.cuda.OutOfMemoryError as exc:
        logger.exception(
            "CUDA OOM during embedding. model=%s, backend=hf_mean_pooling, max_length=%s, device=%s",
            model_name,
            max_length,
            _DEVICE,
        )
        _clear_cuda_cache()
        gc.collect()
        raise RuntimeError(
            f"GPU OOM while embedding text (model={model_name}, backend=hf_mean_pooling, max_length={max_length})"
        ) from exc

    finally:
        del inputs, outputs, pooled, vector_tensor
        gc.collect()
        if _DEVICE.type == "cuda":
            torch.cuda.empty_cache()


def _embed_with_hf_cls_legacy(
    text: str,
    model_name: str,
    max_length: int,
    truncation: bool,
    padding: bool,
    normalize: bool,
) -> list[float]:
    model, tokenizer = _get_model_and_tokenizer(model_name)
    inputs = outputs = embeddings = vector_tensor = None

    try:
        inputs = _tokenize_text(
            tokenizer=tokenizer,
            text=text,
            max_length=max_length,
            truncation=truncation,
            padding=padding,
        )

        with torch.inference_mode():
            outputs = model(**inputs)
            embeddings = outputs.last_hidden_state[:, 0]

            if normalize:
                embeddings = F.normalize(embeddings, p=2, dim=1)

        vector_tensor = embeddings[0].detach().cpu()
        vector = vector_tensor.tolist()
        if not vector:
            raise RuntimeError("Embedding vector is empty")
        return vector

    except torch.cuda.OutOfMemoryError as exc:
        logger.exception(
            "CUDA OOM during embedding. model=%s, backend=hf_cls_legacy, max_length=%s, device=%s",
            model_name,
            max_length,
            _DEVICE,
        )
        _clear_cuda_cache()
        gc.collect()
        raise RuntimeError(
            f"GPU OOM while embedding text (model={model_name}, backend=hf_cls_legacy, max_length={max_length})"
        ) from exc

    finally:
        del inputs, outputs, embeddings, vector_tensor
        gc.collect()
        if _DEVICE.type == "cuda":
            torch.cuda.empty_cache()


def bge_m3_embedder(
    text: str,
    model_name: str = "BAAI/bge-m3",
    padding: bool = False,
    truncation: bool = True,
    normalize: bool = True,
    max_length: int | None = None,
    backend: str | None = None,
) -> list[float]:
    """
    Embeds text with a selectable backend so retrieval quality can be compared
    without losing the legacy path.
    """
    normalized_text = _validate_text(text)
    resolved_backend = backend or get_embedding_backend()
    resolved_max_length = max_length or get_embedding_max_tokens()

    if resolved_backend == "hf_mean_pooling":
        return _embed_with_hf_mean_pooling(
            text=normalized_text,
            model_name=model_name,
            max_length=resolved_max_length,
            truncation=truncation,
            padding=padding,
            normalize=normalize,
        )

    if resolved_backend == "hf_cls_legacy":
        return _embed_with_hf_cls_legacy(
            text=normalized_text,
            model_name=model_name,
            max_length=resolved_max_length,
            truncation=truncation,
            padding=padding,
            normalize=normalize,
        )

    raise ValueError(f"Unsupported embedding backend: {resolved_backend}")
