from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from document_ai.models import ChunkSegmentEmbedding
from document_ai.parsers.config import get_embedding_backend, get_embedding_model
from document_ai.parsers.text_utils import normalize_extracted_text

logger = logging.getLogger(__name__)

SENTENCE_PATTERN = re.compile(r"[^.!?。！？\n]+(?:[.!?。！？]+|$)")


@dataclass(slots=True)
class SegmentSpec:
    segment_index: int
    text: str
    char_start: int
    char_end: int


def _sparse_dot_product(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items())


def _dense_dot_product(left, right) -> float:
    if left is None or right is None:
        return 0.0
    return sum(float(a) * float(b) for a, b in zip(left, right))


def _is_enabled() -> bool:
    raw = getattr(settings, "CONTEXTUAL_COMPRESSION_ENABLED", False)
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return bool(raw)


class EmbeddingContextualCompressor:
    def __init__(
        self,
        *,
        enabled: bool | None = None,
        window_size: int | None = None,
        top_segments: int | None = None,
        max_segments_per_chunk: int | None = None,
        max_chars: int | None = None,
        min_score: float | None = None,
        dense_weight: float | None = None,
        sparse_weight: float | None = None,
    ):
        self.enabled = _is_enabled() if enabled is None else enabled
        self.window_size = max(1, int(window_size or getattr(settings, "CONTEXTUAL_COMPRESSION_WINDOW_SIZE", 2)))
        self.top_segments = max(1, int(top_segments or getattr(settings, "CONTEXTUAL_COMPRESSION_TOP_SEGMENTS", 3)))
        self.max_segments_per_chunk = max(
            1,
            int(max_segments_per_chunk or getattr(settings, "CONTEXTUAL_COMPRESSION_MAX_SEGMENTS_PER_CHUNK", 16)),
        )
        self.max_chars = max(100, int(max_chars or getattr(settings, "CONTEXTUAL_COMPRESSION_MAX_CHARS", 700)))
        self.min_score = max(
            0.0,
            float(min_score if min_score is not None else getattr(settings, "CONTEXTUAL_COMPRESSION_MIN_SCORE", 0.1)),
        )
        self.dense_weight = float(dense_weight if dense_weight is not None else getattr(settings, "CONTEXTUAL_COMPRESSION_DENSE_WEIGHT", 0.4))
        self.sparse_weight = float(sparse_weight if sparse_weight is not None else getattr(settings, "CONTEXTUAL_COMPRESSION_SPARSE_WEIGHT", 0.6))
        self.model_name = get_embedding_model()
        self.embedding_backend = get_embedding_backend()

    def compress_evidences(
        self,
        *,
        evidences: list[dict[str, Any]],
        query_embedding,
        query_sparse: dict[str, float],
    ) -> list[dict[str, Any]]:
        if not self.enabled or not evidences:
            return evidences

        compressed = []
        for evidence in evidences:
            try:
                compressed.append(
                    self.compress_evidence(
                        evidence=evidence,
                        query_embedding=query_embedding,
                        query_sparse=query_sparse,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Contextual compression skipped: chunk_id=%s error=%s",
                    evidence.get("chunk_id"),
                    exc,
                )
                compressed.append(evidence)
        return compressed

    def combine_evidence_texts(self, evidences: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        blocks = []
        compressed_count = 0

        for evidence in evidences or []:
            text = evidence.get("compressed_text") or evidence.get("context_text") or evidence.get("text") or ""
            text = normalize_extracted_text(str(text))
            if not text:
                continue
            if evidence.get("compressed_text"):
                compressed_count += 1

            blocks.append(text)

        compressed_text, unique_count = self._join_unique_sentences(blocks)
        return compressed_text, {
            "enabled": self.enabled,
            "method": "embedding_lazy_segment_document",
            "evidence_count": len(evidences or []),
            "compressed_evidence_count": compressed_count,
            "max_chars": self.max_chars,
            "min_score": self.min_score,
            "unique_sentence_count": unique_count,
        }

    def compress_evidence(
        self,
        *,
        evidence: dict[str, Any],
        query_embedding,
        query_sparse: dict[str, float],
    ) -> dict[str, Any]:
        chunk = evidence.get("_chunk")
        if chunk is None:
            return evidence

        segments = self.get_or_create_segments(chunk)
        if not segments:
            return evidence

        scored = []
        query_dense = getattr(query_embedding, "dense_vector", None)
        for segment in segments:
            dense_score = max(0.0, _dense_dot_product(query_dense, segment.vector))
            sparse_score = _sparse_dot_product(query_sparse, segment.sparse_vector or {})
            score = self.dense_weight * dense_score + self.sparse_weight * sparse_score
            scored.append(
                {
                    "segment": segment,
                    "score": score,
                    "dense_score": dense_score,
                    "sparse_score": sparse_score,
                }
            )

        scored.sort(key=lambda item: item["score"], reverse=True)
        selected = [item for item in scored if item["score"] >= self.min_score][: self.top_segments]
        if not selected:
            top_score = scored[0]["score"] if scored else 0.0
            evidence["compression"] = {
                "enabled": True,
                "method": "embedding_lazy_segment",
                "window_size": self.window_size,
                "top_segments": self.top_segments,
                "selected_segment_count": 0,
                "segment_count": len(segments),
                "max_chars": self.max_chars,
                "min_score": self.min_score,
                "top_score": top_score,
                "dense_weight": self.dense_weight,
                "sparse_weight": self.sparse_weight,
                "skipped_reason": "below_threshold",
            }
            return evidence

        selected.sort(key=lambda item: item["segment"].segment_index)
        texts = [item["segment"].text for item in selected]
        compressed_text, unique_count = self._join_unique_sentences(texts)

        if not compressed_text:
            return evidence

        now = timezone.now()
        ChunkSegmentEmbedding.objects.filter(
            id__in=[item["segment"].id for item in selected if getattr(item["segment"], "id", None)]
        ).update(last_used_at=now)

        evidence["compressed_text"] = compressed_text
        evidence["compression"] = {
            "enabled": True,
            "method": "embedding_lazy_segment",
            "window_size": self.window_size,
            "top_segments": self.top_segments,
            "selected_segment_count": len(selected),
            "segment_count": len(segments),
            "max_chars": self.max_chars,
            "min_score": self.min_score,
            "dense_weight": self.dense_weight,
            "sparse_weight": self.sparse_weight,
            "unique_sentence_count": unique_count,
            "selected_segments": [
                {
                    "segment_index": item["segment"].segment_index,
                    "score": item["score"],
                    "dense_score": item["dense_score"],
                    "sparse_score": item["sparse_score"],
                    "char_start": item["segment"].char_start,
                    "char_end": item["segment"].char_end,
                }
                for item in selected
            ],
        }
        return evidence

    def get_or_create_segments(self, chunk) -> list[ChunkSegmentEmbedding]:
        if not hasattr(chunk, "_meta"):
            return []

        specs = self.split_segments(chunk.text or "")
        if not specs:
            return []

        existing = {
            item.segment_index: item
            for item in ChunkSegmentEmbedding.objects.filter(
                chunk=chunk,
                window_size=self.window_size,
                segment_index__in=[spec.segment_index for spec in specs],
            )
        }

        stale_indexes = {
            spec.segment_index
            for spec in specs
            if spec.segment_index in existing
            and (
                normalize_extracted_text(existing[spec.segment_index].text) != spec.text
                or existing[spec.segment_index].char_start != spec.char_start
                or existing[spec.segment_index].char_end != spec.char_end
            )
        }
        if len(existing) == len(specs) and not stale_indexes:
            return [existing[spec.segment_index] for spec in specs]

        missing = [spec for spec in specs if spec.segment_index not in existing or spec.segment_index in stale_indexes]
        created = self._embed_and_create_segments(chunk, missing)
        merged = {**existing, **created}
        return [merged[spec.segment_index] for spec in specs if spec.segment_index in merged]

    def split_segments(self, text: str) -> list[SegmentSpec]:
        normalized = normalize_extracted_text(text)
        if not normalized:
            return []

        sentence_matches = list(SENTENCE_PATTERN.finditer(normalized))
        if not sentence_matches:
            return [SegmentSpec(segment_index=0, text=normalized[: self.max_chars], char_start=0, char_end=len(normalized))]

        sentences = []
        for match in sentence_matches:
            sentence = normalize_extracted_text(match.group(0))
            if sentence:
                sentences.append((sentence, match.start(), match.end()))

        if not sentences:
            return []

        specs = []
        for index in range(0, len(sentences)):
            window = sentences[index : index + self.window_size]
            if not window:
                continue
            segment_text = normalize_extracted_text(" ".join(item[0] for item in window))
            if not segment_text:
                continue
            specs.append(
                SegmentSpec(
                    segment_index=len(specs),
                    text=segment_text,
                    char_start=window[0][1],
                    char_end=window[-1][2],
                )
            )
            if len(specs) >= self.max_segments_per_chunk:
                break
        return specs

    def _join_unique_sentences(self, texts: list[str]) -> tuple[str, int]:
        unique = []
        seen = set()
        used_chars = 0

        for text in texts:
            for sentence in self._iter_sentences(text):
                key = normalize_extracted_text(sentence).casefold()
                if not key or key in seen:
                    continue

                remaining = self.max_chars - used_chars
                if remaining <= 0:
                    return "\n\n".join(unique), len(unique)
                if len(sentence) > remaining:
                    sentence = sentence[:remaining].rstrip()
                    key = normalize_extracted_text(sentence).casefold()
                    if not sentence or key in seen:
                        continue

                unique.append(sentence)
                seen.add(key)
                used_chars += len(sentence) + 2

        return "\n\n".join(unique), len(unique)

    def _iter_sentences(self, text: str) -> list[str]:
        normalized = normalize_extracted_text(str(text or ""))
        if not normalized:
            return []

        sentences = [normalize_extracted_text(match.group(0)) for match in SENTENCE_PATTERN.finditer(normalized)]
        sentences = [sentence for sentence in sentences if sentence]
        return sentences or [normalized]

    def _embed_and_create_segments(self, chunk, specs: list[SegmentSpec]) -> dict[int, ChunkSegmentEmbedding]:
        if not specs:
            return {}

        from document_ai.embedding.embeding_models import embed_document

        created = {}
        with transaction.atomic():
            for spec in specs:
                embedding = embed_document(
                    spec.text,
                    model_name=self.model_name,
                    backend=self.embedding_backend,
                )
                obj, _ = ChunkSegmentEmbedding.objects.update_or_create(
                    chunk=chunk,
                    window_size=self.window_size,
                    segment_index=spec.segment_index,
                    defaults={
                        "text": spec.text,
                        "char_start": spec.char_start,
                        "char_end": spec.char_end,
                        "vector": embedding.dense_vector,
                        "sparse_vector": embedding.sparse_vector or {},
                        "last_used_at": timezone.now(),
                    },
                )
                created[spec.segment_index] = obj
        return created
