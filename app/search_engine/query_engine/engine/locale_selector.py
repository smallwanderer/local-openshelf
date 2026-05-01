from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from .resource_loader import ResourceLoader, ResourceNotFoundError
except (ImportError, ValueError):
    from resource_loader import ResourceLoader, ResourceNotFoundError


@dataclass(slots=True)
class MatchEvidence:
    """
    로케일 판별 점수에 기여한 개별 매칭 증거 데이터 클래스입니다.
    
    Attributes:
        category: 리소스 카테고리 (예: 'time', 'operators')
        term: 원문 질의에서 발견된 용어
        normalized: 정규화된 값 (필요 시)
        weight: 해당 매칭에 부여된 점수 비중
    """

    category: str
    term: str
    normalized: str | None = None
    weight: float = 0.0


@dataclass(slots=True)
class LocaleMatchResult:
    """
    특정 로케일에 대한 최종 점수 합계와 매칭 증거들의 요약입니다.
    """

    locale: str
    score: float = 0.0
    evidences: list[MatchEvidence] = field(default_factory=list)

    @property
    def matched_terms(self) -> list[str]:
        """중복을 제거한 매칭 용어 리스트를 반환합니다."""
        seen: set[str] = set()
        terms: list[str] = []
        for evidence in self.evidences:
            if evidence.term not in seen:
                seen.add(evidence.term)
                terms.append(evidence.term)
        return terms


@dataclass(slots=True)
class LocaleSelectionResult:
    """
    쿼리 분석기에서 최종적으로 결정된 주 로케일과 활성화된 로케일 리스트입니다.
    """

    primary_locale: str
    active_locales: list[str]
    matches: list[LocaleMatchResult]


class LocaleSelector:
    """
    별도의 언어 감지 모듈 없이, 사전 정의된 리소스 매칭을 통해 로케일을 선택합니다.

    전략:
    1. 'common' 리소스는 항상 로드된다고 가정하며, 판별 대상에서 제외합니다.
    2. 로케일 후보군(ko, en 등)의 리소스(사전)와 질의문 간의 매칭 점수를 계산합니다.
    3. 시간(time), 연산자(operators), 필드 별칭(field_alias) 등 카테고리별로 가중치를 차등 부여합니다.
    4. 더 긴 구문 매칭(Longest match)을 우선하며, 이미 매칭된 영역은 중복 매칭되지 않도록 방어합니다.
    5. 'pdf', 'xlsx'와 같이 범용적인 분석용 토큰은 낮은 신뢰도로 취급하여 특정 언어로 오판하지 않게 합니다.
    """

    # 카테고리별 기본 가중치: 시간과 연산자가 언어 판별에 가장 큰 힌트를 제공한다고 가정합니다.
    DEFAULT_WEIGHTS = {
        "time": 3.0,
        "operators": 3.0,
        "field_alias": 2.0,
        "status": 2.0,
        "file_types": 1.0,
        "intents": 1.0,
        "owner": 1.5,
        "sort": 1.5,
    }

    def __init__(
        self,
        resources_base_dir: str | Path,
        *,
        default_primary_locale: str = "ko",
        max_active_locales: int = 2,
        activation_threshold_ratio: float = 0.35,
        min_absolute_score: float = 1.0,
        category_weights: dict[str, float] | None = None,
    ) -> None:
        """
        Args:
            resources_base_dir: 리소스 파일들이 위치한 루트 디렉토리
            default_primary_locale: 매칭 점수가 낮을 경우 사용할 기본 로케일
            max_active_locales: 동시 활성화할 최대 로케일 수 (결과 병합용)
            activation_threshold_ratio: 1순위 대비 n% 이상의 점수를 가진 로케일만 활성화
            min_absolute_score: 활성화를 위한 최소 절대 점수 임계값
        """
        self.loader = ResourceLoader(resources_base_dir)
        self.default_primary_locale = default_primary_locale
        self.max_active_locales = max_active_locales
        self.activation_threshold_ratio = activation_threshold_ratio
        self.min_absolute_score = min_absolute_score
        self.category_weights = {**self.DEFAULT_WEIGHTS, **(category_weights or {})}

    def select(self, query: str, candidate_locales: list[str] | None = None) -> LocaleSelectionResult:
        """
        입력 질의를 분석하여 최적의 로케일을 선택합니다.
        """
        # 검색 전 쿼리 정규화 (소문자화, 공백 정리)
        cleaned_query = self._normalize_query(query)
        
        # 후보 로케일 목록 조회 (지정되지 않으면 디렉토리 전체 조회)
        locales = candidate_locales or self.loader.list_locales()
        if not locales:
            return LocaleSelectionResult(
                primary_locale=self.default_primary_locale,
                active_locales=["common", self.default_primary_locale],
                matches=[],
            )

        # 각 로케일별 점수 계산
        results: list[LocaleMatchResult] = []
        for locale in locales:
            try:
                # 해당 로케일의 리소스 파일을 직접 읽어 점수산정 (ResourceLoader의 private 메서드 활용 권장X나 현재 구조상 사용)
                locale_resources = self.loader._load_directory_resources(  # noqa: SLF001
                    self.loader.base_dir / locale,
                    required_file_stems=set(),
                )
            except ResourceNotFoundError:
                continue
            results.append(self._score_locale(locale, cleaned_query, locale_resources))

        # 매칭 결과가 전혀 없는 경우 기본 로케일 반환
        if not results:
            return LocaleSelectionResult(
                primary_locale=self.default_primary_locale,
                active_locales=["common", self.default_primary_locale],
                matches=[],
            )

        # 점수 높은 순으로 정렬
        results.sort(key=lambda item: (-item.score, item.locale))
        top = results[0]

        # 최소 임계값 미만인 경우 기본값 사용
        if top.score < self.min_absolute_score:
            primary_locale = self.default_primary_locale
            active_locales = ["common", self.default_primary_locale]
        else:
            primary_locale = top.locale
            active_locales = ["common", primary_locale]

            # 1순위와 경쟁 가능한 2순위 로케일 추가 (threshold 기반)
            threshold = max(self.min_absolute_score, top.score * self.activation_threshold_ratio)
            for result in results[1:]:
                if len(active_locales) - 1 >= self.max_active_locales:
                    break
                if result.score >= threshold:
                    active_locales.append(result.locale)

        return LocaleSelectionResult(
            primary_locale=primary_locale,
            active_locales=active_locales,
            matches=results,
        )

    def _score_locale(
        self,
        locale: str,
        query: str,
        locale_resources: dict[str, Any],
    ) -> LocaleMatchResult:
        """단일 로케일에 대해 카테고리별 매칭 점수를 합산합니다."""
        result = LocaleMatchResult(locale=locale)

        for category, weight in self.category_weights.items():
            resource = locale_resources.get(category)
            if resource is None:
                continue

            evidences = self._match_resource_category(category, resource, query, weight)
            result.evidences.extend(evidences)

        # 4자리 소수점으로 가중치 합계 계산
        result.score = round(sum(item.weight for item in result.evidences), 4)
        return result

    def _match_resource_category(
        self,
        category: str,
        resource: Any,
        query: str,
        base_weight: float,
    ) -> list[MatchEvidence]:
        """
        특정 카테고리(예: time)의 사전 데이터(dict/list)를 질의문과 대照하여 매칭 리스트를 생성합니다.
        Longest Phrase First 전략을 사용하여 '지난주'가 '지난'보다 먼저 매칭되도록 합니다.
        """
        phrases: list[tuple[str, str | None]] = []

        # YAML 구조에 따른 구문 수집 (딕셔너리 또는 리스트 구조 지원)
        if isinstance(resource, dict):
            phrases.extend(self._collect_dict_phrases(resource))
        elif isinstance(resource, list):
            for item in resource:
                if isinstance(item, str):
                    phrases.append((item, None))
        else:
            return []

        evidences: list[MatchEvidence] = []
        used_spans: list[tuple[int, int]] = [] # 이미 매칭된 위치(index) 기록

        # 길이가 긴 구문부터 찾아서 중복 매칭 방지
        for phrase, normalized in sorted(phrases, key=lambda item: len(item[0]), reverse=True):
            phrase_norm = self._normalize_query(phrase)
            if not phrase_norm or len(phrase_norm) <= 1:
                continue

            start = query.find(phrase_norm)
            if start < 0:
                continue
            end = start + len(phrase_norm)

            # 이미 매칭된 영역과 겹치면 건너뜀 (예: '지난주' 매칭 후 '지난' 매칭 방지)
            if self._overlaps_existing_span(start, end, used_spans):
                continue

            # 가중치 계산 (길이 보너스 및 범용 토큰 페널티 적용)
            weight = self._calculate_match_weight(category, phrase_norm, base_weight)
            evidences.append(
                MatchEvidence(
                    category=category,
                    term=phrase,
                    normalized=normalized,
                    weight=weight,
                )
            )
            used_spans.append((start, end))

        return evidences

    def _collect_dict_phrases(self, data: dict[str, Any]) -> list[tuple[str, str | None]]:
        """
        로케일 YAML 구조에서 검색 가능한 문구들을 추출합니다.
        지원 패턴:
        1. 단순 맵: '지난주: last_week'
        2. 객체 구조: '완료: { value: success }'
        3. 별칭 리스트: '용량: { aliases: [크기, 사이즈] }'
        """
        phrases: list[tuple[str, str | None]] = []

        for key, value in data.items():
            if isinstance(key, str):
                if isinstance(value, str):
                    phrases.append((key, value))
                elif isinstance(value, dict):
                    normalized = value.get("value") if isinstance(value.get("value"), str) else None
                    phrases.append((key, normalized))

                    # 별칭(aliases) 필드 처리
                    aliases = value.get("aliases")
                    if isinstance(aliases, list):
                        for alias in aliases:
                            if isinstance(alias, str):
                                phrases.append((alias, normalized or key))
                elif isinstance(value, list):
                    phrases.append((key, None))

        return phrases

    def _calculate_match_weight(self, category: str, phrase: str, base_weight: float) -> float:
        """
        매칭된 구문에 대해 가충치를 세밀하게 조정합니다.
        - 구문의 길이가 길수록 더 정확한 증거로 간주하여 보너스를 부여합니다.
        - 'pdf', 'xlsx' 등 공통적으로 쓰이는 확장자 명칭에는 큰 페널티를 주어 언어 판별 왜곡을 막습니다.
        """
        weight = base_weight

        # 길이 보너스 (최대 +0.75)
        token_length_bonus = min(len(phrase) * 0.05, 0.75)
        weight += token_length_bonus

        # 범용 ASCII 토큰(확장자 등)인 경우 가중치 대폭 삭감
        if self._is_generic_ascii_token(phrase):
            weight *= 0.35

        return round(weight, 4)

    def _is_generic_ascii_token(self, phrase: str) -> bool:
        """언어 판별에 도움이 안 되는 범용 확장자/기술 용어인지 확인합니다."""
        generic_tokens = {
            "pdf", "xls", "xlsx", "doc", "docx", "csv", "txt", "json", "xml", "md",
        }
        lowered = phrase.lower().strip()
        return lowered in generic_tokens

    def _overlaps_existing_span(self, start: int, end: int, spans: list[tuple[int, int]]) -> bool:
        """새로운 매칭 시도가 기존 매칭된 영역과 겹치는지 체크합니다."""
        for existing_start, existing_end in spans:
            if start < existing_end and end > existing_start:
                return True
        return False

    def _normalize_query(self, text: str) -> str:
        """질의문을 소문자화하고 불필요한 연속 공백을 제거합니다."""
        return " ".join(text.lower().strip().split())


if __name__ == "__main__":
    # 로컬 테스트용 실행 코드
    # 현재 파일(engine/locale_selector.py)에서 두 단계 위로 올라가야 resources/가 있음
    base_dir = Path(__file__).parent.parent / "resources"

    selector = LocaleSelector(
        resources_base_dir=base_dir,
        default_primary_locale="ko",
        max_active_locales=2,
        activation_threshold_ratio=0.35,
        min_absolute_score=1.0,
    )

    queries = [
        "지난주 pdf 파일",
        "last week report",
        "last week AX 전환 pdf",
        "어제 만든 excel file",
    ]

    for query in queries:
        result = selector.select(query)
        print(f"\nQuery: {query}")
        print("Primary:", result.primary_locale)
        print("Active:", result.active_locales)
        for match in result.matches:
            print(" -", match.locale, match.score, match.matched_terms)
