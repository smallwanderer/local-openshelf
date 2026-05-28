# LLM Reasoning/Thinking Output Policy

이 문서는 `llama.cpp` 호환 LLM 호출에서 `reasoning_format`을 `none`으로 두고,
thinking/thought 값을 요청하거나 저장하지 않는 이유와 현재 적용 정책을 정리한다.

## 1. 배경

RAG, Text2SQL, Query parser는 모두 `llm-parser`의 OpenAI 호환
`/v1/chat/completions` 엔드포인트를 호출한다. 이때 일부 reasoning 모델은 최종 답변
외에 내부 추론 채널을 함께 반환할 수 있다.

예시:

```text
<|channel>thought
질문 분석, 근거 검토, 답변 초안 작성 과정...

<|channel>final
최종 답변
```

이 형식은 모델 내부 추론과 최종 산출물이 명확히 분리되어 있을 때는 후처리가 가능하지만,
실제 운영 경로에서는 다음 문제가 반복적으로 발생했다.

- `thought` 내용이 사용자 답변에 그대로 노출됨
- 최종 답변이 한국어가 아니라 reasoning trace 형태로 표시됨
- `<|channel>final`이 비어 있거나 누락되어 답변이 공백 처리됨
- JSON, QueryDSL, SQL처럼 엄격한 출력 형식이 필요한 경로에서 파싱 실패가 발생함
- 로그/DB에 최종 산출물이 아닌 내부 추론 흔적이 저장될 위험이 생김

따라서 기본 운영 정책은 "모델에게 thinking을 요청하지 않고, thinking을 저장하지 않으며,
가능하면 서버 요청에서도 thinking 출력을 끈다"로 정했다.

## 2. 적용 원칙

### 2.1 최종 답변만 시스템 산출물로 취급

사용자에게 보여주거나 DB에 저장할 값은 다음에 한정한다.

- RAG: 최종 답변, citation, 검색된 근거 문서 메타데이터
- Text2SQL: 최종 SQL/응답 payload
- Query parser: 검증 가능한 QueryDSL 후보 또는 fallback semantic query

thinking/thought/reasoning trace는 위 산출물에 포함하지 않는다.

### 2.2 `reasoning_format=none`을 기본값으로 사용

`llama.cpp`/`llm-parser` 요청에서 thinking 출력을 끌 수 있는 경우
`reasoning_format`을 `none`으로 지정한다.

현재 코드에서 명시적으로 적용되는 대표 경로:

- Text2SQL: `app/document_ai/tasks.py`의 Text2SQL LLM 요청 payload
- RAG: `app/document_ai/tasks.py`의 RAG LLM 요청 payload
- Query parser: `QUERY_REASONING_FORMAT` 환경변수 기본값이 `none`

이 설정의 목적은 모델 성능 최적화가 아니라 출력 계약 안정화다. 즉, 모델이 더 잘
생각하게 만들기 위한 설정이 아니라, 시스템이 받을 수 있는 응답 형식을 안정적으로
제한하기 위한 설정이다.

### 2.3 후처리는 final content만 신뢰

LLM 응답 후처리에서는 `final` 마커를 우선 찾는다.

우선 추출 대상:

- `<|channel>final`
- `<|final|>`
- `Final Output`
- `최종 답변:`
- `답변:`

`thought`만 있고 `final`이 없으면 정상 최종 산출물로 보지 않는다. Query parser처럼
fallback이 가능한 경로에서는 원 질의를 semantic query로 그대로 사용하는 방식을 택한다.
RAG처럼 답변이 필요한 경로에서는 citation 기반 fallback 답변을 사용하거나 오류를 기록한다.

## 3. 경로별 이유

### 3.1 RAG

RAG는 검색 근거를 바탕으로 사용자에게 자연어 답변을 제공하는 기능이다. 여기에서
thinking이 노출되면 다음 문제가 생긴다.

- 답변 본문이 길고 산만해짐
- 근거와 모델의 내부 추론이 섞여 citation 신뢰성이 떨어짐
- 한국어 답변 지시가 있어도 영어 reasoning trace가 노출될 수 있음
- `근거 부족` 같은 자체 판단 과정이 노출되어 충분한 근거가 있어도 답변 품질이 나빠짐

RAG prompt는 이미 다음을 요구한다.

- 제공된 근거만 사용
- 최종 답변만 출력
- reasoning, analysis, thoughts, channel, step-by-step thinking 출력 금지
- code block 금지

따라서 RAG에서는 `reasoning_format=none`과 prompt 제한을 함께 사용한다.

### 3.2 Text2SQL

Text2SQL은 자연어 질의를 SQL 또는 SQL 유사 응답으로 변환하는 구조다. 이 경로에서는
출력 형식 안정성이 특히 중요하다.

thinking이 섞이면 다음 문제가 생긴다.

- SQL 앞뒤에 설명/추론이 붙어 실행 전 검증이 어려워짐
- JSON payload를 기대하는 클라이언트에서 파싱 실패 발생
- thought 안에 잘못된 SQL 초안이 포함되어 최종 SQL과 혼동될 수 있음
- 로그에 사용자의 질의 의도와 내부 추론이 불필요하게 저장됨

따라서 Text2SQL에서는 final content만 남기고, thinking은 요청하지 않는다.

### 3.3 Query Parser

Query parser는 사용자 자연어를 시스템이 검증 가능한 QueryDSL 후보로 바꾸는 실험 경로다.
이 경로의 핵심은 "LLM이 대략적인 구조를 만들고, QueryDSL validator/query engine이
검증한다"는 점이다.

thinking이 포함되면 다음 문제가 생긴다.

- JSON object 추출이 불안정해짐
- schema에 없는 필드나 설명 문장이 섞임
- validator가 실제 후보가 아니라 reasoning text를 처리하게 됨
- final이 비어 있을 때 전체 검색 파이프라인이 중단될 수 있음

그래서 Query parser는 final content가 없으면 원 질의를 그대로 semantic query로 보내는
fallback을 둔다. 이 정책은 검색/RAG 기본 기능을 보존하기 위한 안전장치다.

## 4. 저장하지 않는 이유

thinking 값을 DB나 사용자 이력에 저장하지 않는 이유는 다음과 같다.

### 4.1 사용자에게 제공할 산출물이 아님

thinking은 답변, SQL, QueryDSL, citation과 달리 제품 기능의 결과물이 아니다. 저장해도
사용자가 검증하거나 재사용할 수 있는 안정적인 데이터가 아니다.

### 4.2 보안 및 프롬프트 노출 위험

thinking에는 다음이 섞일 수 있다.

- 시스템 프롬프트 구조
- 근거 선택 과정
- 내부 fallback 판단
- 사용자의 원문 질의 재해석
- 모델이 만든 중간 초안

이는 운영 로그나 DB에 장기 보관할 정보가 아니다.

### 4.3 디버깅 데이터로도 품질이 낮음

thinking trace는 모델별/버전별로 형식이 불안정하다. 디버깅에는 다음 데이터가 더 유용하다.

- 요청 id/job id
- 검색 query
- 검색 결과 score
- selected citations
- final answer
- LLM HTTP status
- normalization/fallback reason
- error message

따라서 운영 관측성은 thinking 저장이 아니라 구조화된 job metadata와 score trace 중심으로
확보한다.

## 5. 현재 방어 로직

현재 시스템은 thinking 출력을 끄는 것에 더해, 응답에 thought가 섞여 들어오는 경우를
대비해 후처리 방어 로직을 둔다.

대표 정책:

- final marker가 있으면 final 이후만 사용
- thought만 있고 final이 없으면 빈 최종값으로 간주
- raw answer에 thought/channel token이 남아 있으면 제거
- RAG 답변이 비어 있으면 citation 기반 fallback 사용
- RAG가 충분한 citation이 있는데도 근거 부족을 주장하면 fallback 답변 사용
- Query parser final이 비어 있으면 원 질의를 semantic query로 사용

이 방어 로직은 `reasoning_format=none`이 항상 완벽하게 지켜진다는 가정에 의존하지
않기 위한 보조 장치다.

## 6. 운영 정책

### 6.1 기본값

운영 기본값은 다음과 같다.

```text
reasoning_format=none
```

Query parser는 필요 시 환경변수로 조정할 수 있지만, 기본값은 `none`이어야 한다.

```env
QUERY_REASONING_FORMAT=none
```

RAG와 Text2SQL은 현재 코드에서 `none`을 명시적으로 사용한다.

### 6.2 저장 정책

저장한다:

- 최종 답변
- citation
- 검색 결과와 score metadata
- 검증된 QueryDSL 또는 fallback 결과
- 오류 메시지와 fallback reason

저장하지 않는다:

- thought
- thinking
- reasoning trace
- chain-of-thought
- step-by-step internal analysis

### 6.3 로그 정책

정상 운영 로그에는 thinking을 남기지 않는다. 장애 분석을 위해 raw preview를 남길 때도
길이를 제한하고, 최종 산출물 파싱 실패 원인을 확인하는 수준으로만 사용한다.

## 7. 예외적으로 thinking을 켜야 하는 경우

운영 경로에서는 켜지 않는다. 다만 로컬 실험에서 모델별 final marker 호환성, query parser
출력 형식, reasoning 모델의 응답 특성을 평가해야 할 때만 제한적으로 허용할 수 있다.

조건:

- 운영 DB에 저장하지 않을 것
- 사용자 화면에 노출하지 않을 것
- 로그 파일에 장기 보관하지 않을 것
- 실험 종료 후 `reasoning_format=none`으로 되돌릴 것
- final parser/fallback 테스트를 함께 수행할 것

## 8. 결론

이 프로젝트에서 thinking을 끄고 저장하지 않는 이유는 다음으로 요약된다.

1. 사용자 답변에 내부 추론이 섞이는 것을 막기 위해
2. RAG/Text2SQL/QueryDSL의 출력 형식과 파싱 안정성을 지키기 위해
3. 내부 프롬프트와 중간 추론이 로그/DB에 남는 것을 막기 위해
4. 운영 관측성을 thinking trace가 아니라 구조화된 score, citation, error metadata로 확보하기 위해

따라서 RAG, Text2SQL, Query parser의 기본 정책은 계속 `final output only`이며,
thinking은 요청하지 않고 저장하지 않는다.
