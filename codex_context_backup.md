# Codex Context Backup

기준일: 2026-05-16

이 문서는 현재까지 합의한 OpenShelf 아키텍처, 핵심 파일 경로, 남은 작업을 빠르게 복원하기 위한 컨텍스트 백업입니다.

## 1. 합의된 전체 아키텍처

OpenShelf는 파일 저장/다운로드를 담당하는 웹 서버와 무거운 AI 작업을 담당하는 worker를 분리하는 방향입니다.

```text
nginx
  -> app
      - Django/Gunicorn 또는 dev runserver
      - 로그인, 파일 목록, 업로드, 다운로드, 휴지통
      - AI 작업 직접 실행 금지, Celery task 큐잉만 수행
      - 운영 app 이미지는 AI 의존성 미설치

redis
  - Celery broker
  - 복구 dedup lock
  - Text2SQL semaphore

db
  - PostgreSQL
  - pgvector
  - Django 모델 데이터
  - DocumentChunk/ChunkEmbedding/SearchJob 저장

celery-worker
  - parse, embed queue 처리
  - Docling 파싱, 청킹, BGE-M3 임베딩

celery-search-worker
  - search queue 처리
  - query embedding 생성
  - pgvector 검색
  - SearchJob.results 저장

celery-text2sql-worker
  - text2sql queue 처리

celery-beat
  - recover-document-pipeline-backlog 주기 실행
  - 누락/실패/stale parse/embed 작업 재큐잉

llm-parser
  - text2sql 계열 LLM 서버
```

`document_ai`는 별도 HTTP 서버가 아니라 같은 Django 코드베이스 안의 앱입니다. 무거운 실행만 Celery worker 컨테이너에서 처리합니다.

## 2. 실행 구성

운영:

```bash
docker compose up -d --build
```

개발:

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

개발 접속:

```text
http://localhost:8888/
```

운영 접속:

```text
http://localhost/
```

현재 `SearchJob` 추가로 migration이 필요합니다.

```bash
docker compose -f docker-compose.dev.yml exec app python manage.py migrate
```

## 3. 의존성 구조

```text
app/requirements.web.txt
  - Django/Gunicorn/DRF/Celery client/DB/pgvector 등 웹 서버 최소 의존성

app/requirements.ai.txt
  - Docling, Torch 계열, FlagEmbedding, HWP 파서 등 AI worker 의존성

app/requirements.dev.txt
  - pytest, pytest-django 등 개발/테스트 의존성

app/requirements.txt
  - 로컬 전체 설치용 aggregate 파일
```

Docker build arg:

```text
INSTALL_AI_DEPS=0
  - app, celery-beat, text2sql worker 기본

INSTALL_AI_DEPS=1
  - celery-worker, celery-search-worker

INSTALL_DEV_DEPS=1
  - docker-compose.dev.yml 개발 컨테이너
```

## 4. 검색 아키텍처

기존 동기 검색은 app 웹 요청 안에서 query embedding을 생성해 AI 의존성 분리와 충돌했습니다. 현재는 `SearchJob` 기반 비동기 검색입니다.

```text
POST /api/document-ai/v1/search/
  -> SearchJob 생성
  -> perform_vector_search task를 search queue에 등록
  -> 202 Accepted + job_id 반환

celery-search-worker
  -> SearchJob 조회
  -> query embedding 생성
  -> VectorRetriever로 pgvector 검색
  -> SearchJob.results 저장

GET /api/document-ai/v1/search/jobs/<job_id>/
  -> pending/processing/completed/failed 상태 및 결과 반환
```

프론트는 `job_id`를 받아 polling합니다.

RabbitMQ는 아직 적용하지 않았습니다. Redis가 broker, 복구 lock, semaphore 역할을 같이 하므로 RabbitMQ 전환은 broker 역할과 Redis lock 역할을 분리하는 시점에 검토합니다.

## 5. AI 처리 상태 UX

파일 목록에서 긴 AI 상태 텍스트 대신 작은 아이콘 배지를 표시합니다.

표시 기준:

- 업로드 완료: 파일이면 항상 표시
- 파싱 완료: `ai_status.parse_status === "completed"`
- 임베딩 완료: `ai_status.embedding_completed` 또는 `ai_status.embedding_status === "completed"`

배지 SVG:

```text
app/static/assets/status/uploaded.svg
app/static/assets/status/parsed.svg
app/static/assets/status/embedded.svg
```

그리드 뷰:

- 카드 오른쪽 아래에 배지 표시

리스트 뷰:

- 배지를 액션 버튼과 분리된 `list-action-cell`에 둠
- 배지는 항상 보임
- 다운로드/삭제 버튼만 hover 시 표시

## 6. 파일 관리 UX

현재 반영된 UX:

- 그리드/리스트 뷰 선택을 `localStorage`에 저장
- 새로고침 후 마지막 선택 뷰 유지
- AI 검색은 job polling 방식
- AI 처리 완료 상태를 작은 배지로 표시

뷰 모드 저장 키:

```text
openshelf:file-list:view
```

## 7. 핵심 로컬 파일 경로

Compose / 배포:

```text
docker-compose.yml
docker-compose.dev.yml
app/Dockerfile
nginx/default.conf
nginx/dev.conf
.env.example
```

Django 설정:

```text
app/config/settings.py
app/config/celery.py
app/config/urls.py
app/config/enums.py
```

파일 앱:

```text
app/files/models.py
app/files/services/file_service.py
app/files/services/storage.py
app/files/views/page_views.py
app/files/views/healthcheck.py
app/files/api_v1/file_views.py
app/files/templates/files/file_list.html
app/files/templates/files/file_detail.html
app/files/templates/files/upload.html
app/files/templates/files/base.html
```

Document AI:

```text
app/document_ai/models.py
app/document_ai/tasks.py
app/document_ai/signals.py
app/document_ai/urls.py
app/document_ai/parsers/config.py
app/document_ai/parsers/docling_parser.py
app/document_ai/parsers/text_utils.py
app/document_ai/embedding/embeding_models.py
app/document_ai/search/retriever.py
app/document_ai/search/views.py
app/document_ai/search/serializers.py
```

Migrations:

```text
app/document_ai/migrations/0004_recovery_metadata.py
app/document_ai/migrations/0005_searchjob.py
```

의존성:

```text
app/requirements.web.txt
app/requirements.ai.txt
app/requirements.dev.txt
app/requirements.txt
```

Static assets:

```text
app/static/assets/icons/
app/static/assets/status/
```

테스트 실행:

```text
docker compose -f docker-compose.dev.yml exec app python -m pytest
```

`No module named pytest`가 나오면 운영용 compose 또는 로컬 Python으로 테스트가 실행된 것입니다. dev 이미지가 오래된 경우 `docker compose -f docker-compose.dev.yml build app` 후 다시 실행합니다.

GitHub Actions unit test:

```text
.github/workflows/pytest-unit.yml
  - cp .env.example .env.dev
  - docker compose -f docker-compose.dev.yml build celery-core-worker
  - docker compose -f docker-compose.dev.yml run --rm celery-core-worker python -m pytest -m "unit"
```

unit test collection에서 `document_ai.parsers.docling_parser`가 import되므로 CI test image에는 dev 의존성과 AI 의존성이 모두 필요합니다.

문서:

```text
README.md
STATUS.md
codex_context_backup.md
AGENTS.md
```

## 8. 자주 쓰는 명령

개발 컨테이너 시작:

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

개발 migration:

```bash
docker compose -f docker-compose.dev.yml exec app python manage.py migrate
```

로그 확인:

```bash
docker compose -f docker-compose.dev.yml logs -f app
docker compose -f docker-compose.dev.yml logs -f celery-worker
docker compose -f docker-compose.dev.yml logs -f celery-search-worker
docker compose -f docker-compose.dev.yml logs -f celery-beat
```

수동 복구 큐잉:

```bash
docker compose -f docker-compose.dev.yml exec app python manage.py shell -c "from document_ai.tasks import recover_document_pipeline_backlog; recover_document_pipeline_backlog.delay()"
```

전체 재파싱 큐잉:

```bash
docker compose -f docker-compose.dev.yml exec app python manage.py reparse_documents --queue
```

## 9. 남은 Todo List

최우선:

- 검색 job polling UX 개선
- 검색 timeout, 실패 표시, retry 정책 정리
- 오래된 SearchJob 정리 command 또는 beat task 추가
- app 컨테이너가 AI 의존성 없이 비-AI 기능을 안정적으로 제공하는지 smoke test
- dev/prod compose에서 `docker compose config` 검증

파일 관리 UX:

- 업로드 진행률 개선
- 드래그 앤 드롭 업로드
- 다중 선택
- 일괄 삭제/복원/이동
- 폴더 breadcrumb 개선
- 정렬 옵션 확대: 이름, 크기, 업로드일, 수정일, AI 처리 상태
- 파일 미리보기: 이미지, PDF, 텍스트, Markdown
- 즐겨찾기/태그/최근 파일 UX 정리

AI 상태 UX:

- 상세 화면에도 단계별 상태 표시
- 실패 원인 요약 표시
- “다시 분석” 버튼
- “AI 분석 제외” 옵션
- 분석 대상 확장자/파일 크기 정책 표시

검색 UX:

- 일반 검색과 AI 검색 UI 분리
- AI 검색 결과 evidence 표시 개선
- 검색 결과에서 파일 열기/다운로드/위치 이동 동선 개선
- 검색 job 취소/재시도 UI
- 특정 폴더/파일 유형 범위 검색

운영/복구:

- queue/load 기반 recovery throttling
- worker 상태 확인 페이지
- 실패 파일 목록
- stale 파일 목록
- 전체 재큐잉 버튼
- worker 메모리 제한과 재시작 정책
- 대용량 파일 처리 정책

테스트:

- 업로드 -> 파싱 -> 청킹 -> 임베딩 -> 검색 E2E
- SearchJob API 테스트
- celery-search-worker task 테스트
- 파일 목록 UX JS 회귀 테스트 또는 최소 smoke test

장기:

- RabbitMQ 전환 검토
- 운영 백업/복구 문서화
- 로그/메트릭/알림 체계
- 공유 링크, 만료 링크, 읽기 전용 공유
- 팀 공유/권한 모델/감사 로그
