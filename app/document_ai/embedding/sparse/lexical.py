from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache

# Common English stopwords
ENGLISH_STOPWORDS = frozenset(
    {
        "a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "or",
        "is", "are", "was", "were", "it", "this", "that", "with", "as", "by",
        "from", "be", "have", "has", "had", "do", "does", "did", "but", "not",
        "what", "which", "who", "when", "where", "why", "how", "all", "any",
        "both", "each", "few", "more", "most", "other", "some", "such", "than",
        "too", "very", "can", "will", "just", "should", "now",
    }
)

# Common single-syllable Korean functional terms / particles
KOREAN_SINGLE_STOPWORDS = frozenset({"이", "그", "저", "것", "수", "등", "및", "를", "을", "에", "의", "로", "과", "와", "도", "은", "는"})

# Common Korean postposition (조사) suffix pattern for rule-based stripping
_JOSA_SUFFIX_PATTERN = re.compile(
    r"(에서부터|에게서|으로써|으로서|에서는|으로는|에서|에게|으로|까지|부터|처럼|보다|에는|은|는|이|가|을|를|의|에|로|과|와|도)$"
)

_WORD_PATTERN = re.compile(r"[a-zA-Z0-9_\-\.]+")
_HANGUL_PATTERN = re.compile(r"[\uac00-\ud7a3]+")


class LexicalSparseEncoder:
    """Lightweight lexical sparse encoder with optional Kiwi morphological analysis.

    Extracts word tokens (alphanumeric, identifiers, version numbers) and
    Hangul nouns/morphemes. If `kiwipiepy` is installed, it leverages Kiwi
    to precisely extract nouns (NNG, NNP, NR) and roots (XR) while stripping
    particles. If Kiwi is unavailable, it gracefully falls back to rule-based
    postposition stripping and 2-gram character bigrams.
    """

    def __init__(
        self,
        *,
        default_doc_max_terms: int = 128,
        default_query_max_terms: int = 32,
    ):
        self.default_doc_max_terms = default_doc_max_terms
        self.default_query_max_terms = default_query_max_terms
        self._kiwi = self._init_kiwi()

    @staticmethod
    def _init_kiwi():
        try:
            from kiwipiepy import Kiwi
            return Kiwi()
        except Exception:
            return None

    @property
    def has_kiwi(self) -> bool:
        return self._kiwi is not None

    def tokenize(self, text: str) -> list[str]:
        if not text or not text.strip():
            return []

        tokens: list[str] = []

        # 1. Alphanumeric, code identifiers, version numbers (e.g. v2.4.1, harrier-270m, 8080)
        # Always run regex tokenizer so technical tokens and versions are never split
        for match in _WORD_PATTERN.finditer(text):
            raw = match.group().strip(".-_").lower()
            if not raw or len(raw) < 2:
                continue
            if raw in ENGLISH_STOPWORDS:
                continue
            tokens.append(raw)

        # 2. Korean morphological analysis (Kiwi) or graceful bigram fallback
        if self._kiwi is not None:
            try:
                for res in self._kiwi.tokenize(text):
                    # NNG: 일반명사, NNP: 고유명사, NR: 수사, XR: 어근
                    if res.tag in ("NNG", "NNP", "NR", "XR"):
                        form = res.form.strip().lower()
                        if form and len(form) >= 2 and form not in KOREAN_SINGLE_STOPWORDS:
                            tokens.append(form)
                        elif len(form) == 1 and form not in KOREAN_SINGLE_STOPWORDS:
                            tokens.append(form)
                    # VV: 동사, VA: 형용사 어간 (길이 2 이상)
                    elif res.tag in ("VV", "VA") and len(res.form) >= 2:
                        tokens.append(res.form.strip().lower())
                return tokens
            except Exception:
                pass  # Fallback to rule-based tokenizer below if Kiwi raises unexpectedly

        # 3. Pure Python Fallback: Josa stripping + Character bigrams
        for match in _HANGUL_PATTERN.finditer(text):
            h_word = match.group()
            if not h_word:
                continue

            # Full word token
            if len(h_word) >= 2:
                tokens.append(h_word)

                # Rule-based josa (조사) suffix stripping: "도토리에서" -> "도토리"
                stripped = _JOSA_SUFFIX_PATTERN.sub("", h_word)
                if stripped and stripped != h_word and len(stripped) >= 2:
                    tokens.append(stripped)

                # Character bigrams (e.g. "임베딩" -> "임베", "베딩")
                if len(h_word) >= 3:
                    for i in range(len(h_word) - 1):
                        bg = h_word[i : i + 2]
                        tokens.append(bg)
            elif len(h_word) == 1 and h_word not in KOREAN_SINGLE_STOPWORDS:
                tokens.append(h_word)

        return tokens

    def encode(
        self,
        text: str,
        *,
        max_terms: int | None = None,
        is_query: bool = False,
    ) -> dict[str, float]:
        """Encode text into an L2-normalized sparse vector dictionary {term: weight}."""
        limit = max_terms or (self.default_query_max_terms if is_query else self.default_doc_max_terms)
        tokens = self.tokenize(text)
        if not tokens:
            return {}

        counts = Counter(tokens)

        # Sublinear TF scaling: 1 + ln(count)
        weights: dict[str, float] = {}
        for term, count in counts.items():
            tf = 1.0 + math.log(count)
            weights[term] = tf

        # Select top terms by weight
        sorted_terms = sorted(weights.items(), key=lambda item: item[1], reverse=True)[:limit]

        # L2 normalize
        norm_sq = sum(w * w for _, w in sorted_terms)
        if norm_sq <= 0:
            return {}

        norm = math.sqrt(norm_sq)
        return {term: round(w / norm, 4) for term, w in sorted_terms}


@lru_cache(maxsize=1)
def get_lexical_sparse_encoder() -> LexicalSparseEncoder:
    return LexicalSparseEncoder()
