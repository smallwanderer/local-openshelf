# 운영 가이드

Dotori for Document(이하 Dotori)는 자가 호스팅 서비스로, 개인 PC·NAS·클라우드 서버 등에 설치해 RAG 기능과 고급 검색(IR) 기능을 이용합니다. 이 문서는 이미 설치된 서버를 운영하는 방법을 설명합니다 — 상태 확인, 계정 관리, 문서 파이프라인 운영, 검색·RAG 파라미터 조정, 보안 점검을 다룹니다.

설치가 아직이라면 [설치 가이드](./installation-guide.md)를 먼저 참고하세요.

---

## 개요

Dotori는 단일 Docker Compose 스택으로 구동되는 자가 호스팅 문서 검색·RAG 서비스입니다. 운영자는 서버를 구동하고, 사용자 계정을 관리하며, 문서 처리 파이프라인과 LLM 런타임이 정상 동작하는지 확인할 수 있습니다. AI 모델과 런타임 파라미터는 서버 단위로 한 번 설정되며, 사용자별로 변경할 수 없습니다.

### 기본 시스템 구성 요소

Dotori 스택은 다음 컴포넌트로 구성됩니다. 각 컴포넌트는 Docker 환경에서 별도의 이미지 및 볼륨으로 구성되어 있으며, docker-compose에 의해 실행됩니다. 문서 파싱과 임베딩은 Redis/Celery 파이프라인으로 실행됩니다. 대화형 검색과 기본 RAG UI는 요청 경로에서 직접 실행됩니다.

| 컴포넌트 | 역할 |
|----------|------|
| **Dotori Web App (app)** | 웹 UI, 파일 업로드, 작업 등록, 관리자 기능을 담당합니다. |
| **Database (db)** | 파일 메타데이터, 분석작업 결과를 저장합니다. |
| **Storage** | Docker volume으로 구성된 영구 저장소. 원본 파일과 모델 cache가 저장됩니다. |
| **parser-executor** | parse 큐를 소비하여 Docling 파싱과 청크 저장을 수행합니다. |
| **embedding-executor-consumer** | embed 큐를 소비하여 배치, 상태 전환과 벡터 저장을 수행합니다. |
| **embedding-executor** | query/document 임베딩 HTTP endpoint이며 local 모드에서만 모델을 로드합니다. |
| **dotori-llm** | 로컬 LLM 컨테이너. 설치 단계에서 설정된 LLM 서빙 엔진으로 동작하며, 자원 격리와 런타임 엔진의 동적 교체를 위해 별도 컨테이너로 운영됩니다. 설정에 따라 OpenAI 호환 API를 통한 원격 LLM 연동도 가능합니다. |

미처리된 문서는 `repair_document_pipeline` 관리 명령으로 점검하고 복구합니다 — [문제가 있다면](#문제가-있다면)를 참고하세요.

### 서비스 아키텍처

```text
   Dotori Web App (app) ----------- Database (db)
        |
        +-- parser-executor              [parse]
        +-- embedding-executor-consumer [embed]
        |        `-- embedding-executor [HTTP model/backend]
        +-- synchronous search
        `-- HTTP RAG stream -------- 선택된 로컬 런타임
                                           |-- llama.cpp
                                           `-- vLLM
```

오래 걸리는 문서 파싱과 임베딩만 전용 Celery worker에서 실행됩니다. 검색은 즉시 결과를 반환하고 RAG 답변은 NDJSON으로 스트리밍됩니다.

### 운영 모드별 활성 서비스

설치 시 선택한 운영 모드에 따라 활성 서비스가 달라집니다.

| 모드 | 활성 서비스 | 비고 |
|------|------------|------|
| **Full 로컬 AI RAG** | app, db, redis, parser-executor, embedding-executor-consumer, embedding-executor, dotori-llm | 검색은 app에서 동기 실행하고 LLM 답변은 NDJSON 스트리밍 |
| **Hybrid/Search AI** | app, db, redis, parser-executor, embedding-executor-consumer, embedding-executor | LLM 없이 파싱·임베딩·하이브리드 검색만 동작 |
| **기본 모드** | app, db, redis | 파일 관리만 사용 |

현재 설치된 모드는 Docker에서 실행되는 컨테이너 상태로 확인하거나, `llm_runtime.json`에 저장되어 있습니다.

### 확장 서비스 요소

| 컴포넌트 | 역할 |
|----------|------|
| **Nginx** | 선택형 외부 HTTPS 리버스 프록시. `direct-https` profile을 연결한 경우에만 실행됩니다. |

---

## 환경변수 반영

`.env`의 값 대부분은 컨테이너 재시작이 있어야 반영됩니다. 예를 들어 `LOGIN_REQUIRED`를 직접 `.env`에서 수정한 경우 `python install.py --restart`로 재시작해야 하며, `python install.py --login enable|disable` 명령을 사용하면 값 갱신과 재시작 확인을 한 번에 처리할 수 있습니다.

---

## 상태 확인 및 모니터링

관리자 계정으로 로그인하면 `/workspace/operations/`(사이드바 "운영 요약")에서 현재 상태, 최근 검색·RAG·업로드 성능, 처리 대기·실패, 자원 여유를 한 화면에서 확인할 수 있습니다. 화면 구성과 활용법은 [모니터링 가이드](./monitoring-and-quality-guide.md)를 참고하세요.

아래는 대시보드에 없는 값을 더 깊이 조사하고 싶을 때, 또는 웹 UI 없이 직접 확인하고 싶을 때를 위한 명령·파일 위치입니다.

### 컨테이너 상태 확인

```bash
docker compose ps
docker compose exec -T app python manage.py server_status --json-output
```

`server_status`는 DB 연결, 파일 입출력 파이프라인, 임베딩 런타임, LLM 런타임(RAG) 상태를 한 번에 점검합니다. `--json-output` 없이 실행하면 사람이 읽기 쉬운 형태로 출력됩니다. 컨테이너 자체가 떠 있는지는 `docker compose ps`의 `STATUS` 컬럼(healthy/unhealthy)으로 먼저 확인하세요.

### 서비스별 로그 확인

`app`과 각 executor는 로그 파일을 서비스별로 분리해서 씁니다(동시 쓰기 경합 방지).

- `data/logs/operations.<service>.log` — 운영 로그
- `data/logs/document_ai.<service>.log` — 파싱/임베딩/검색/RAG 진행 상황 서술 로그
- `data/logs/db_span.<service>.log` — 요청 안에서 실행된 SQL 목록과 소요시간 (파라미터 값은 기록하지 않음)

모든 로그 줄과 `SearchJob`/`RAGJob.performance_metrics`에는 같은 `trace_id`가 남습니다. 특정 요청 하나의 전체 흐름(여러 서비스에 걸친)을 보려면:

```bash
grep <trace_id> data/logs/*.log
```

업로드 요청(적재 trace)은 파싱→임베딩까지 하나의 `trace_id`로 이어지고, 검색·RAG 요청(질의 trace)은 요청 하나당 하나의 `trace_id`를 가집니다.

### LLM 런타임 상태 확인

```bash
docker compose exec -T app python manage.py server_status --json-output
```

출력의 `rag` 섹션에서 `enabled`/`configured`/`health_status`를 확인합니다. 현재 활성 런타임·모델·동시성(`serving_concurrency`) 설정은 `data/config/llm_runtime.json`에 저장되어 있습니다. 동시성 값을 실측 기반으로 조정하려면 [LLM 운용 동시성 Calibration](#llm-운용-동시성-calibration)을 참고하세요.

### 문서 처리 파이프라인 상태 파악

```bash
docker compose exec -T app python manage.py server_status --json-output
```

`embedding` 섹션에서 임베딩 런타임 활성화 여부를 확인합니다. 개별 문서의 처리 상태(파싱 실패, 임베딩 대기/실패)는 admin에서 확인합니다.

- `/admin/document_ai/documentparseresult/` — 문서별 파싱 상태(`status`), 실패 사유(`errors`)
- `/admin/document_ai/documentchunk/` — 청크별 임베딩 상태(`status`)

대기·실패 건이 쌓여 있으면 [문제가 있다면](#문제가-있다면)의 `repair_document_pipeline`으로 복구합니다.

### 성능 지표와 자원 확인

**요청 단위 성능 수치** (평균·최댓값·실패율): `SearchJob`/`RAGJob.performance_metrics`를 직접 쿼리합니다.

```python
# docker compose exec -T app python manage.py shell
from document_ai.models import RAGJob
from django.db.models import Avg, Max, Count
RAGJob.objects.filter(status="COMPLETED").aggregate(
    Avg("performance_metrics__llm_ttft_ms"),
    Max("performance_metrics__llm_ttft_ms"),
)
```

또는 admin(`/admin/document_ai/searchjob/`, `/admin/document_ai/ragjob/`)에서 `performance_metrics` JSON을 필드별로 확인할 수 있습니다. percentile(p50/p95 등) 집계 저장소는 없습니다 — 표본이 작은 규모라 평균·최댓값 같은 단순 집계로 충분하다는 판단입니다.

**자원 여유(DB connection 수, 디스크 여유 공간)**:

```bash
docker compose exec -T app python manage.py collect_resource_snapshot
```

결과는 `ResourceSnapshot` 테이블에 쌓이고 `/admin/document_ai/resourcesnapshot/`에서 확인합니다. 컨테이너별 CPU/RAM/GPU는 이 명령 범위에 없습니다 — 필요하면 호스트에서 `docker stats`를 직접 실행하세요.

**소수 동시 요청에서 검색·RAG가 버티는지 점검**:

```bash
python3 scripts/check_search_load.py --cookie '...' --requests 5 --concurrency 3
python3 scripts/check_rag_stream.py --cookie '...' --csrf-token '...' --requests 3 --concurrency 1
```

결과는 stdout에만 출력됩니다(요청별 결과 줄 + 마지막 `summary` 줄).

**LLM 서빙 동시성 실측 데이터**: [LLM 운용 동시성 Calibration](#llm-운용-동시성-calibration)의 실행 결과가 `data/evaluation/runs/<run_id>/`에 남습니다 — `steps.json`(동시성 단계별 TTFT/처리량 percentile), `resource_samples.jsonl`(GPU 샘플), `summary.json`(최종 선택값)을 확인하세요. 지금 가장 상세한 성능 관측 데이터입니다.

---

## 관리자 계정 및 사용자 관리

Dotori는 외부 접속이 없는 개인/로컬 사용을 기본 전제로 하며, 이에 맞춰 로그인 여부를 서버 단위로 켜고 끌 수 있습니다.

| 설정값 | 동작 | 적합한 환경 |
|--------|------|--------------|
| `LOGIN_REQUIRED=0` (기본값) | 로그인 절차 없이 접속 시 로컬 관리자 프로필로 자동 인증됨 | 외부 접속이 없는 개인 PC, NAS 등 |
| `LOGIN_REQUIRED=1` | 일반적인 이메일/비밀번호 로그인이 필수 | 외부에서 접속 가능한 서버, 다중 사용자 환경 |

### 최초 관리자 계정 생성

`LOGIN_REQUIRED=0`(기본값) 상태에서는 별도의 계정 생성 절차 없이 첫 접속 시 `local-admin@dotori.local` 계정이 자동으로 생성되어 로그인 없이 관리자 권한(`is_staff`/`is_superuser`)으로 접속됩니다. 이 계정은 비밀번호가 없으므로 실제 로그인 화면으로는 인증할 수 없습니다.

`LOGIN_REQUIRED=1`인 환경에서는 자동 로그인이 동작하지 않으므로, `/accounts/signup/`에서 계정을 만든 뒤 `python manage.py createsuperuser` 또는 Django Admin에서 `is_staff`/`is_superuser`를 부여해 관리자 계정을 구성합니다.

### Django Admin 접속

`/admin/`은 관리자 계정만 접근할 수 있습니다. `LOGIN_REQUIRED=0`일 때는 자동 로그인된 계정이 이미 관리자 권한이므로 별도 로그인 없이 바로 접근됩니다. `LOGIN_REQUIRED=1`일 때는 일반 로그인 화면을 거쳐야 합니다.

### 사용자 관리

**계정 전환 (로그인 없이 프로필 전환)**

`LOGIN_REQUIRED=0`일 때만 네브바의 계정명을 클릭하거나 `/accounts/switch/`로 접속하면 비밀번호 없이 로컬 프로필을 선택하거나 그 자리에서 새 프로필을 만들 수 있습니다. 이 경우, 실제 로그인이나 이메일 인증에는 쓰이지 않는 내부용 식별자(`local-xxxxxxxxxxxx@dotori.local`)가 자동으로 부여되며 새로 만든 프로필도 기본적으로 관리자 권한을 가집니다. 해당 이메일은 실제 이메일 서버에서 수신하는 것이 아니므로 별도의 설정이 필요하지 않습니다. `LOGIN_REQUIRED=1`인 서버에서는 이 경로 자체가 404로 막혀 있어 실제 로그인을 우회할 수 없습니다.

별칭은 설정 페이지(`/accounts/settings/` → "표시 이름")에서 언제든 바꿀 수 있습니다. 로그인 필수 모드의 회원가입 화면에도 같은 별칭 입력란이 있지만, 그쪽은 선택 항목이며 실제 이메일 계정을 그대로 사용합니다.

**비로그인 계정을 로그인 모드에서도 쓸 수 있게 준비하기**

비로그인 모드에서 만든 계정(합성 이메일, 비밀번호 없음)은 `LOGIN_REQUIRED=1`로 전환하면 실제 로그인 폼으로 인증할 수 없습니다. 나중에도 이 계정을 계속 쓰려면 전환 전에 설정 페이지(`/accounts/settings/`)에서 미리 다음을 해두세요.

- **이메일** 섹션에서 실제로 받을 수 있는 이메일로 변경
- **위험 구역 → 비밀번호 설정**에서 비밀번호 지정 (비밀번호가 없는 계정은 "현재 비밀번호" 확인 없이 바로 새 비밀번호를 설정할 수 있습니다)

두 가지를 미리 해두지 않고 `LOGIN_REQUIRED=1`로 전환하면, 서버 콘솔에서 `docker compose exec app python manage.py changepassword <email>`로 비밀번호를 설정하거나 Django Admin에서 직접 계정을 수정해야 합니다.

**로그인 요구 여부 전환**

```bash
python install.py --login [enable/disable]
```

또는 `start.bat` → `[6] Maintenance` → `[5] Toggle Login Requirement`에서도 동일하게 전환할 수 있습니다. 두 방법 모두 `.env`의 `LOGIN_REQUIRED` 값을 갱신한 뒤 즉시 재시작할지 확인합니다(`--yes`로 확인 생략 가능).

**계정 목록 확인**

```bash
python install.py --accounts list
```

등록된 계정의 이메일, 활성화 여부, staff/superuser 상태를 표로 출력합니다.

---

## 문서 파이프라인 운영

### 지원 파일 형식

| 분류 | 확장자 |
|------|--------|
| 텍스트/코드 | `.txt`, `.md`, `.markdown`, `.yaml`, `.yml`, `.json`, `.py`, `.sh`, `.bash`, `.sql`, `.xml`, `.html`, `.htm`, `.toml`, `.ini`, `.cfg`, `.conf`, `.js`, `.ts` |
| 한글 문서 | `.hwp`, `.hwpx` |
| 오피스/PDF/이미지 | `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.tiff`, `.gif` |

이미지 파일도 업로드하면 Docling 기반 OCR로 텍스트를 추출해 파싱·임베딩 대상이 됩니다. 위 목록에 없는 확장자는 업로드는 되지만 파싱 대상에서 제외되어 원본 파일 보관 용도로만 사용됩니다.

### 처리 큐 튜닝

파싱과 임베딩 consumer는 각각 `parser-executor`, `embedding-executor-consumer`에서 실행됩니다. 대기열이 계속 쌓이면 다음 값을 조정합니다(`.env`에서 변경 후 `python install.py --restart` 필요).

| 값 | 기본값 | 설명 |
|---|---|---|
| `PARSE_WORKER_CONCURRENCY` | `1` | 파싱 executor의 동시 실행 프로세스 수입니다. |
| `EMBEDDING_WORKER_CONCURRENCY` | `1` | 임베딩 consumer의 동시 실행 프로세스 수입니다. |
| `EMBEDDING_HTTP_THREADS` | `8` | 임베딩 서비스(gunicorn)의 스레드 수. 임베딩 요청이 몰릴 때 응답 지연이 크면 늘려봅니다. |
| `EMBEDDING_SERVICE_TIMEOUT` | `60` | query와 단일 document 임베딩 HTTP 호출의 제한 시간(초)입니다. |
| `EMBEDDING_BATCH_SERVICE_TIMEOUT` | `180` | 여러 document 청크를 한 번에 처리하는 배치 호출의 제한 시간(초)입니다. CPU 추론은 query보다 오래 걸릴 수 있어 별도로 둡니다. |

이 값들은 서버 하드웨어 여유 안에서만 늘리세요. 과도하게 늘리면 다른 컨테이너(특히 `dotori-llm`)와 자원을 다투게 됩니다.

> 임베딩 모델을 BGE-M3 외의 다른 오픈소스 모델이나 외부 OpenAI 호환 엔드포인트로 변경하려면 [임베딩 런타임 가이드](./embedding-guide.md)를 참고하세요.

---

## 검색 및 RAG 설정

### 하이브리드 검색 튜닝

`/api/document-ai/sandbox/`(로그인 필요)와 `/api/document-ai/v1/tuning/` API는 dense/sparse 검색 가중치(`dense_weight`, `sparse_weight`), 후보 배수(`candidate_multiplier`), pool 크기(`pool_top_k`, `pool_tau`) 등 10개 저수준 retrieval 파라미터를 직접 바꿔가며 결과를 실험할 수 있는 화면입니다.

> 이 화면은 일반 사용자 설정 UI가 아니라 운영자가 검색 품질을 진단할 때 쓰는 실험용 도구입니다. 접근 제한이 로그인 여부뿐이라 로그인 없이 자동 접속되는 기본 구성(`LOGIN_REQUIRED=0`)에서는 사실상 누구나 열어볼 수 있습니다 — 여기서 바꾼 값은 저장되지 않고 그 요청에만 적용되지만, 값의 의미를 모르고 만지면 검색 결과가 왜 이상해졌는지 혼란을 줄 수 있습니다.

### RAG 파라미터 조정

| 값 | 기본값 | 설명 |
|---|---|---|
| `RAG_SEARCH_TOP_K` | `3` | RAG 답변 생성 시 근거로 사용할 검색 결과 개수. |
| `RAG_RETRIEVAL_THRESHOLD` | (미설정) | 이 값 미만의 유사도 점수를 가진 결과는 RAG 근거에서 제외합니다. 비워두면 기준을 적용하지 않습니다. |

두 값 모두 `.env`에서 조정 후 `python install.py --restart`가 필요합니다. 요청 단위로 다른 값을 쓰고 싶다면(예: CLI의 `dotori ask`) API 호출 시 `top_k`/`threshold`를 직접 지정할 수 있습니다 — 자세한 요청 형식은 [API 계약](./api-contract-v1.md)을 참고하세요.

### Contextual Compression 설정

검색된 청크에서 질문과 관련된 부분만 남기고 압축해 RAG 컨텍스트 길이를 줄이는 기능입니다. 기본값은 비활성(`0`)이며, `.env`에서 아래 값을 조정한 뒤 `python install.py --restart`로 반영합니다.

| 값 | 기본값 | 설명 |
|---|---|---|
| `CONTEXTUAL_COMPRESSION_ENABLED` | `0` | `1`로 설정하면 기능이 켜집니다. |
| `CONTEXTUAL_COMPRESSION_WINDOW_SIZE` | `2` | 관련 구간 앞뒤로 함께 포함할 문장 수. |
| `CONTEXTUAL_COMPRESSION_MAX_SEGMENTS_PER_CHUNK` | `16` | 청크 하나에서 평가할 최대 구간 수. |
| `CONTEXTUAL_COMPRESSION_TOP_SEGMENTS` | `3` | 최종적으로 남길 구간 수. |
| `CONTEXTUAL_COMPRESSION_MAX_CHARS` | `700` | 압축 결과의 최대 글자 수. |
| `CONTEXTUAL_COMPRESSION_MIN_SCORE` | `0.1` | 이 점수 미만인 구간은 버립니다. |
| `CONTEXTUAL_COMPRESSION_DENSE_WEIGHT` / `_SPARSE_WEIGHT` | `0.4` / `0.6` | 구간 점수를 계산할 때 dense/sparse 유사도에 부여하는 가중치. |

RAG 답변 품질이 컨텍스트 과다로 떨어진다고 판단될 때만 켜보는 것을 권장하며, 켠 뒤에는 [LLM 운용 동시성 Calibration](#llm-운용-동시성-calibration)과 같은 방식으로 실제 답변 품질 변화를 확인하세요.

---

## LLM 런타임 관리

현재 설정 확인, 모델 변경, 상세 설치 절차는 [LLM 설치 가이드](./llm-installation-guide.md)에서 다룹니다.

### LLM 운용 동시성 Calibration

동시에 여러 사용자가 사용할 경우, 사용자가 원하는 성능을 얻기 위해선 calibration 과정이 필요합니다. 해당 과정은 팀 단위 사용자에게 유효한 옵션과 기능이며 개인 사용자에게는 추천하지 않습니다.

Local LLM이 install wizard에 의해 자동 설치된 직후 모든 운용 동시성은 1로 제한됩니다. 즉, LLM 모델은 한 번에 하나의 요청만을 처리하도록 제한됩니다. 그대로 사용하더라도 시스템이 요청을 순서대로 처리하므로 팀 단위 사용에도 문제는 없습니다. 다만 사용자가 많아질 경우 LLM 모델이 요청을 처리하는 시간이 길어지고, 다른 사용자들이 기다려야 하므로 하드웨어 성능이 충분하다면 calibration을 통해 운용 동시성을 높이는 것을 추천합니다.

이 작업은 동시성별로 로컬 LLM 컨테이너를 재시작하므로 다른 사용자가 없는 점검 시간에 수행해야 합니다. 다만 진행 과정에서 프로세스 강제 종료로 `calibration_status=running` 상태가 남은 경우, 다음 calibration 실행 시 이전 세션이 복구됩니다.

측정 데이터를 구성하는 경우 warm-up 질문과 측정 질문은 달라야 하며, `node_ids`에는 Dotori에 올린 파일 또는 폴더 UID를 지정합니다. 더 정확한 계산을 위하여 다양한 파일을 통해 테스트를 진행하시는 것을 추천합니다.

```json
{"id":"warmup-1","phase":"warmup","question":"이 문서들의 목적을 한 문단으로 설명해줘","node_ids":["<file-or-folder-uid>"],"top_k":3,"language":"ko"}
{"id":"measure-1","phase":"measure","question":"Dotori의 LLM 런타임 변경 절차와 안전장치를 정리해줘","node_ids":["<file-or-folder-uid>"],"top_k":3,"language":"ko"}
```

해당 계정의 RAG 요청이 외부 LLM endpoint로 향하면 로컬 runtime 측정이 아니므로 실행기는 그 요청을 실패 처리합니다.

```bash
python scripts/calibrate_llm_concurrency.py \
  --workload /path/to/dotori-calibration.jsonl \
  --cookie-file /path/to/cookie.txt \
  --csrf-token-file /path/to/csrf-token.txt
```

실행기는 `1..safe_concurrency_ceiling`을 순차 측정하며, 각 단계에서 runtime health check, warm-up, 실제 RAG 요청, GPU 표본 수집을 수행합니다. 처리량 증가가 작거나 preset의 TTFT/전체 지연 예산을 넘으면 더 높은 동시성 측정을 중단합니다. 선택 결과는 runtime 인자와 RAG semaphore에 함께 반영됩니다.

결과는 `data/evaluation/runs/<run_id>/`에 저장됩니다. 질문 원문, 답변 본문, Cookie, CSRF token은 결과에 저장하지 않으며 workload 파일의 이름과 SHA-256 해시만 기록합니다. `summary.json`에서 선택값과 단계별 제외 이유를 확인할 수 있습니다.

---

## 보안

### HTTPS / Let's Encrypt 설정

외부 접속을 켤 계획이라면 HTTPS는 선택이 아니라 필수입니다(HTTP만으로 외부에 노출하는 구성은 지원하지 않습니다). 설정 파일 준비부터 인증서 발급·갱신까지 전체 절차는 [설치 가이드](./installation-guide.md#외부-접속-연결-선택)에 정리되어 있습니다. 요약하면:

1. `python install.py --network-access-create` / `--network-access-open`으로 `data/config/network_access/provider.env`를 채웁니다.
2. `bash scripts/init-letsencrypt.sh`로 최초 인증서를 발급받습니다. **이 단계 없이 `--network-access-connect`만 실행하면 인증서 파일이 없어 Nginx가 시작되지 않습니다.**
3. 이후 `python install.py --network-access-connect`로 연결합니다.
4. Let's Encrypt 인증서는 90일마다 만료되므로 `bash scripts/renew-letsencrypt.sh`를 주기적으로(예: 매월) 직접 실행하거나 cron으로 예약합니다. 자동 갱신 장치는 내장되어 있지 않습니다.

### 허용 호스트 및 CSRF 설정

외부 도메인을 연결하기 전에 `data/config/network_access/provider.env`의 `DOTORI_DJANGO_ALLOWED_HOSTS`와 `DOTORI_DJANGO_CSRF_TRUSTED_ORIGINS`에 그 도메인을 반드시 등록해야 합니다(자세한 설명은 [설치 가이드](./installation-guide.md#외부-접속-연결-선택) 참고).

- `DOTORI_DJANGO_ALLOWED_HOSTS`에 도메인이 없으면 Django가 모든 요청을 `DisallowedHost` 오류로 거부합니다.
- `DOTORI_DJANGO_CSRF_TRUSTED_ORIGINS`에 `https://<도메인>`이 없으면 로그인·업로드 등 CSRF 토큰이 필요한 요청이 모두 `403 CSRF_FAILED`로 실패합니다.
- 값을 바꾼 뒤에는 `python install.py --network-access-connect`를 다시 실행해야 반영됩니다.

---

## 문제가 있다면

### 문서 파이프라인 복구가 필요함

먼저 후보만 확인한 뒤 필요한 경우 한 번만 적용합니다.

```bash
python manage.py repair_document_pipeline --dry-run
python manage.py repair_document_pipeline --apply
```

---

## 더 보기

- [설치 가이드](./installation-guide.md) — 서버 설치와 운영 모드
- [LLM 설치 가이드](./llm-installation-guide.md) — 로컬 LLM 선택, 변경, 상세 설정
- [임베딩 런타임 가이드](./embedding-guide.md) — 임베딩 모델 전환, 외부 엔드포인트 연결
- [모니터링 가이드](./monitoring-and-quality-guide.md) — trace_id 추적, 로그 조회, 검색 품질 점검
- [API 계약](./api-contract-v1.md) — 검색·RAG 요청 형식과 파라미터
