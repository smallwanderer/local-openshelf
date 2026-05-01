from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Pattern


@dataclass(slots=True)
class NormalizationRule:
    """One regex-based normalization rule."""

    name: str
    pattern: Pattern[str]
    replacement: str
    repeat_until_stable: bool = False


@dataclass(slots=True)
class NormalizationChange:
    """Tracks one applied normalization step for debugging."""

    rule_name: str
    before: str
    after: str


@dataclass(slots=True)
class PreNormalizationResult:
    """Output of pre-normalization before locale selection."""

    raw_query: str
    normalized_query: str
    changes: list[NormalizationChange] = field(default_factory=list)


class PreNormalizer:
    """
    Lightweight, locale-independent query normalizer.

    Goals:
    1. Increase resource match rate before locale selection.
    2. Reduce spacing / punctuation / unit-expression variation.
    3. Normalize only safe surface forms, not semantic meaning.

    Important:
    - This layer should NOT decide model scopes or filters.
    - This layer should NOT rewrite domain keywords aggressively.
    - This layer should stay conservative and explainable.
    """

    def __init__(self) -> None:
        self.rules = self._build_rules()

    def normalize(self, query: str) -> PreNormalizationResult:
        current = query
        changes: list[NormalizationChange] = []

        # 1. Unicode-ish safe whitespace cleanup first
        cleaned = self._normalize_whitespace(current)
        if cleaned != current:
            changes.append(
                NormalizationChange(
                    rule_name="normalize_whitespace",
                    before=current,
                    after=cleaned,
                )
            )
            current = cleaned

        # 2. Lowercase only ASCII letters to avoid unexpected language-side effects
        lowered = self._lower_ascii(current)
        if lowered != current:
            changes.append(
                NormalizationChange(
                    rule_name="lower_ascii",
                    before=current,
                    after=lowered,
                )
            )
            current = lowered

        # 3. Regex rule-based normalization
        for rule in self.rules:
            before_rule = current
            after_rule = self._apply_rule(current, rule)
            if after_rule != before_rule:
                changes.append(
                    NormalizationChange(
                        rule_name=rule.name,
                        before=before_rule,
                        after=after_rule,
                    )
                )
                current = after_rule

        # 4. Final trim / spacing stabilization
        stabilized = self._normalize_whitespace(current)
        if stabilized != current:
            changes.append(
                NormalizationChange(
                    rule_name="stabilize_whitespace",
                    before=current,
                    after=stabilized,
                )
            )
            current = stabilized

        return PreNormalizationResult(
            raw_query=query,
            normalized_query=current,
            changes=changes,
        )

    def _apply_rule(self, text: str, rule: NormalizationRule) -> str:
        result = rule.pattern.sub(rule.replacement, text)
        if not rule.repeat_until_stable:
            return result

        while result != text:
            text = result
            result = rule.pattern.sub(rule.replacement, text)
        return result

    def _normalize_whitespace(self, text: str) -> str:
        text = text.replace("\u00A0", " ")
        text = text.replace("\t", " ")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _lower_ascii(self, text: str) -> str:
        return "".join(ch.lower() if "A" <= ch <= "Z" else ch for ch in text)

    def _build_rules(self) -> list[NormalizationRule]:
        return [
            # --- Punctuation / separator cleanup ---
            NormalizationRule(
                name="remove_light_punctuation_between_tokens",
                pattern=re.compile(r"(?<=\w)\s*[,/|]+\s*(?=\w)"),
                replacement=" ",
                repeat_until_stable=True,
            ),
            NormalizationRule(
                name="remove_sentence_like_punctuation",
                pattern=re.compile(r"[，,؛;:!?]+"),
                replacement=" ",
            ),
            NormalizationRule(
                name="collapse_parenthesis_spacing",
                pattern=re.compile(r"\(\s+|\s+\)"),
                replacement=lambda_match_placeholder(),
            ),
            # --- Korean high-frequency phrase joins ---
            NormalizationRule(
                name="join_ko_last_week",
                pattern=re.compile(r"지난\s+주"),
                replacement="지난주",
            ),
            NormalizationRule(
                name="join_ko_this_week",
                pattern=re.compile(r"이번\s+주"),
                replacement="이번주",
            ),
            NormalizationRule(
                name="join_ko_next_week",
                pattern=re.compile(r"다음\s+주"),
                replacement="다음주",
            ),
            NormalizationRule(
                name="join_ko_previous_week_variant",
                pattern=re.compile(r"저번\s+주"),
                replacement="지난주",
            ),
            NormalizationRule(
                name="join_ko_last_month",
                pattern=re.compile(r"지난\s+달"),
                replacement="지난달",
            ),
            NormalizationRule(
                name="join_ko_this_month",
                pattern=re.compile(r"이번\s+달"),
                replacement="이번달",
            ),
            NormalizationRule(
                name="join_ko_next_month",
                pattern=re.compile(r"다음\s+달"),
                replacement="다음달",
            ),
            # --- English high-frequency phrase spacing ---
            NormalizationRule(
                name="normalize_en_last_week",
                pattern=re.compile(r"last\s+week", re.IGNORECASE),
                replacement="last week",
            ),
            NormalizationRule(
                name="normalize_en_this_week",
                pattern=re.compile(r"this\s+week", re.IGNORECASE),
                replacement="this week",
            ),
            NormalizationRule(
                name="normalize_en_next_week",
                pattern=re.compile(r"next\s+week", re.IGNORECASE),
                replacement="next week",
            ),
            # --- Number + unit joins ---
            NormalizationRule(
                name="join_decimal_and_unit_ascii",
                pattern=re.compile(r"(\d+(?:\.\d+)?)\s+(kb|mb|gb|tb|kib|mib|gib|tib)\b", re.IGNORECASE),
                replacement=r"\1\2",
            ),
            NormalizationRule(
                name="join_decimal_and_unit_korean_mb",
                pattern=re.compile(r"(\d+(?:\.\d+)?)\s+(메가바이트|메가)\b"),
                replacement=r"\1메가",
            ),
            NormalizationRule(
                name="join_decimal_and_unit_korean_gb",
                pattern=re.compile(r"(\d+(?:\.\d+)?)\s+(기가바이트|기가)\b"),
                replacement=r"\1기가",
            ),
            NormalizationRule(
                name="join_decimal_and_unit_korean_kb",
                pattern=re.compile(r"(\d+(?:\.\d+)?)\s+(킬로바이트|킬로)\b"),
                replacement=r"\1킬로",
            ),
            # --- Common file-type spoken variants ---
            NormalizationRule(
                name="normalize_spoken_pdf_variant",
                pattern=re.compile(r"피\s*디\s*에\s*프"),
                replacement="pdf",
            ),
            NormalizationRule(
                name="normalize_spoken_csv_variant",
                pattern=re.compile(r"씨\s*에스\s*브이"),
                replacement="csv",
            ),
            NormalizationRule(
                name="normalize_spoken_xlsx_variant",
                pattern=re.compile(r"엑\s*셀"),
                replacement="엑셀",
            ),
            # --- Comparison-expression stabilization ---
            NormalizationRule(
                name="normalize_ko_lte_spacing",
                pattern=re.compile(r"이\s+하"),
                replacement="이하",
            ),
            NormalizationRule(
                name="normalize_ko_gte_spacing",
                pattern=re.compile(r"이\s+상"),
                replacement="이상",
            ),
            NormalizationRule(
                name="normalize_ko_lt_spacing",
                pattern=re.compile(r"미\s+만"),
                replacement="미만",
            ),
            NormalizationRule(
                name="normalize_ko_gt_spacing",
                pattern=re.compile(r"초\s+과"),
                replacement="초과",
            ),
            # --- File-extension spacing cleanup ---
            NormalizationRule(
                name="join_dot_extension_spacing",
                pattern=re.compile(r"\.\s+(pdf|docx?|xlsx?|csv|txt|json|xml|md)\b", re.IGNORECASE),
                replacement=r".\1",
            ),
        ]


def lambda_match_placeholder():
    """
    Returns a callable replacement for spacing inside parentheses.

    Example:
        '( pdf )' -> '(pdf)'
    """

    def _replace(match: re.Match[str]) -> str:
        value = match.group(0)
        if value.startswith("("):
            return "("
        return ")"

    return _replace


if __name__ == "__main__":
    normalizer = PreNormalizer()

    queries = [
        "지난 주에 만든 10 mb 이하 pdf 파일",
        "  last   week   report  ",
        "피 디 에 프 파일 20 메가 이하",
        "( pdf ) 파일 / csv 파일",
    ]

    for query in queries:
        result = normalizer.normalize(query)
        print("\nRAW:", result.raw_query)
        print("NORMALIZED:", result.normalized_query)
        for change in result.changes:
            print(f" - {change.rule_name}: {change.before!r} -> {change.after!r}")