# OpenShelf

**OpenShelf는 개인 또는 소규모 팀이 직접 운영할 수 있는 가벼운 문서 검색 및 RAG 시스템입니다.**

<p align="center">
 <img src = "https://github.com/user-attachments/assets/263cba6d-04f6-49ba-9ccb-85481157539a", width="80%">
</p>

<p align="center">
 <img src = "https://github.com/user-attachments/assets/d240cf34-1dfb-462e-8366-3f0d3e4a435f", width="80%">
</p>

파일 저장소, 문서 파싱, 하이브리드 벡터 검색, RAG 답변 생성을 하나의 Docker Compose 환경에서 실행할 수 있도록 구성되어 있습니다. 서버에서도 실행할 수 있고, Windows에서는 Docker Desktop과 WSL2 기반으로 실행할 수 있습니다.

영문 문서는 [README.md](README.md), 단계별 실행 가이드는 [WALKTHROUGH.md](WALKTHROUGH.md)를 참고하세요.

## 무엇을 하는 시스템인가요?

OpenShelf는 개인 문서나 팀 문서를 웹 화면에서 관리하고, 업로드된 문서에 대해 검색과 질문 답변을 수행하는 시스템입니다.

- 파일과 폴더를 웹 UI에서 관리합니다.
- 문서를 비동기 worker가 파싱하고 임베딩합니다.
- PostgreSQL + pgvector에 문서 벡터를 저장합니다.
- dense/sparse hybrid retriever로 문서를 검색합니다.
- RAG 화면에서 질문하고, 근거 문서와 함께 답변을 확인합니다.
- Docker Compose로 서버 또는 Windows 환경에서 실행할 수 있습니다.

## 현재 릴리즈

`0.1.0-alpha`는 pre-release입니다. 핵심 RAG와 검색 흐름은 사용할 수 있지만, 일부 고급 기능은 아직 실험 단계입니다.

이 릴리즈는 source-based release입니다. 아직 Docker 이미지를 별도로 배포하지 않습니다. release tag를 checkout한 뒤 Docker Compose로 직접 build해서 실행합니다.

## 구조

```text
nginx
  -> app
      Django 웹 UI, 파일 API, 작업 큐잉

redis
  Celery broker

db
  PostgreSQL + pgvector

celery-core-worker
  문서 파싱, 청킹, 임베딩

celery-search-worker
  query embedding, hybrid retrieval, 검색 job 처리

celery-llm-rag-worker
  RAG 답변 생성

celery-query-worker
  실험적 query parsing

celery-text2sql-worker
  실험적 Text2SQL 작업

llm-parser
  llama.cpp 호환 로컬 LLM endpoint
```

웹 컨테이너는 가볍게 유지하고, 파싱/임베딩/검색/LLM 작업은 worker 컨테이너에서 처리합니다.

## 주요 기능

- 로그인 기반 파일 및 폴더 관리
- 비동기 문서 AI 처리 파이프라인
- BGE-M3 기반 dense/sparse hybrid 검색
- 근거 문서가 표시되는 RAG 질문 화면
- 검색/RAG evidence에 대한 embedding 기반 contextual compression
- Shelf-Sync 연동을 위한 서버 측 sync API 기반
- 운영 상태 확인을 위한 Django admin 확장
- 검증과 fallback을 포함한 실험적 QueryDSL parser 경로

## 요구 사항

- Docker Engine 또는 Docker Desktop
- Docker Compose v2
- Git
- 모델 다운로드에 필요한 Hugging Face token

Windows에서는 Docker Desktop의 WSL2 backend 사용을 권장합니다.

## 빠른 시작

```bash
git clone https://github.com/smallwanderer/local-openshelf.git
cd local-openshelf
cp .env.example .env
docker compose up -d --build
docker compose exec app python manage.py createsuperuser
```

접속:

```text
http://localhost/
```

관리자 페이지:

```text
http://localhost/admin/
```

Windows 또는 로컬 개발 환경:

```bash
cp .env.example .env.dev
docker compose -f docker-compose.dev.yml up -d --build
```

접속:

```text
http://localhost:8888/
```

서버와 Windows 환경별 자세한 실행 순서는 [WALKTHROUGH.md](WALKTHROUGH.md)를 참고하세요.

## 주요 설정

대부분의 설정은 `.env` 또는 `.env.dev`에서 변경합니다.

| 변수 | 설명 |
| --- | --- |
| `DJANGO_SECRET_KEY` | Django secret key. 실제 운영에서는 반드시 변경해야 합니다. |
| `DJANGO_ALLOWED_HOSTS` | 접속을 허용할 도메인 또는 IP |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | 브라우저 요청을 신뢰할 origin |
| `POSTGRES_*` | PostgreSQL 접속 정보 |
| `HF_TOKEN` | 모델 다운로드에 필요한 Hugging Face token |
| `EMBEDDING_MODEL` | 임베딩 모델. 기본값은 BGE-M3 |
| `EMBEDDING_DISTANCE_STRATEGY` | dense 검색 거리 계산 방식 |
| `EMBEDDING_HYBRID_DENSE_WEIGHT` | dense score 가중치 |
| `EMBEDDING_HYBRID_SPARSE_WEIGHT` | sparse score 가중치 |
| `CONTEXTUAL_COMPRESSION_ENABLED` | evidence 압축 사용 여부 |
| `RAG_SEARCH_TOP_K` | RAG에서 기본으로 검색할 문서 수 |
| `RAG_RETRIEVAL_THRESHOLD` | RAG dense similarity threshold |
| `RAG_EVIDENCE_LIMIT` | RAG prompt에 넣을 최대 근거 수 |
| `QUERY_FRONTEND_MODE` | Query parser 사용 방식. 기본은 passthrough |

## 테스트

CI와 같은 unit test:

```bash
docker compose -f docker-compose.dev.yml run --rm celery-core-worker python -m pytest -m "unit"
```

개발 환경 전체 테스트:

```bash
docker compose -f docker-compose.dev.yml exec app python -m pytest
```

## 운영 메모

- 업로드 파일과 DB 데이터는 `data/` 아래에 저장됩니다.
- 기본 Compose 파일은 운영에 가까운 `docker-compose.yml`입니다.
- 개발용 Compose 파일은 `docker-compose.dev.yml`이며 `http://localhost:8888/`에서 실행됩니다.
- RAG와 Text2SQL은 `llm-parser` 컨테이너를 사용합니다.
- LLM의 reasoning/thinking 출력은 기본적으로 요청하거나 저장하지 않습니다. 자세한 내용은 [LLM_REASONING_POLICY.md](LLM_REASONING_POLICY.md)를 참고하세요.
- QueryDSL parser는 실험 기능입니다. 기본 검색/RAG 경로는 semantic query passthrough를 사용합니다.

## 0.1.0-alpha 요약

- Full-stack RAG 통합
- Hybrid retriever 개선
- Evidence contextual compression 추가
- 서버 측 sync API 기반 추가
- Django admin 운영 모니터링 확장
- LLM reasoning/thinking 출력 정책 문서화

## 앞으로의 계획

- golden set 기반 검색 성능 평가 확대
- RAG 답변 품질 평가와 citation 이동 개선
- Text2SQL workflow 확장
- QueryDSL parser 평가 후 선택적 활성화
- 운영 보안, 백업, 모니터링 문서화
