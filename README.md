# OpenShelf

OpenShelf는 개인 또는 소규모 팀이 직접 운영할 수 있는 오픈소스 파일 스토리지입니다.
파일 업로드, 폴더 관리, 휴지통, 로그인 기반 접근 제어를 기본으로 제공하고, 문서 파싱/청킹/임베딩을 통해 AI 검색 기능을 확장하는 것을 목표로 합니다.

현재 구현은 웹 서버 안정성을 우선합니다. 파일 저장/다운로드를 담당하는 `app` 컨테이너는 가볍게 유지하고, 파싱/청킹/임베딩처럼 무거운 작업은 Celery worker 컨테이너에서 처리합니다.

---

## 현재 구현 상태

### 구현됨

- 세션 기반 회원가입/로그인/로그아웃
- 이메일 인증 여부에 따른 파일 API 접근 제한
- 파일 및 폴더 업로드/조회/이동/삭제
- 휴지통, 복원, 7일 보관 기간 기반 정리 메타데이터
- 사용자별 저장 공간 사용량 표시
- PostgreSQL + pgvector 기반 문서 임베딩 저장 구조
- Docling 기반 문서 파싱, 청킹, HWP/HWPX 일부 처리
- BGE-M3 기반 dense/sparse 임베딩 생성
- Celery 기반 비동기 파싱/임베딩 파이프라인
- Celery 기반 비동기 AI 검색 job
- Celery beat 기반 파이프라인 복구 작업
- 파일 상세/목록 화면의 AI 처리 상태 표시
- Docker Compose 기반 운영/개발 실행 구성
- 웹 서버와 AI worker 의존성 분리

### 제한 사항

- AI 검색은 job 기반 비동기 처리로 동작합니다. 검색 요청 직후 결과가 즉시 반환되지 않고, 화면에서 job 상태를 polling해 완료된 결과를 표시합니다.
- Celery broker는 아직 Redis를 사용합니다. RabbitMQ 전환은 queue 정책과 복구 lock 구조를 함께 정리한 뒤 진행하는 것이 좋습니다.
- 파싱/임베딩 파이프라인은 비동기 처리되므로, 파일 업로드 직후에는 AI 검색 결과가 바로 나오지 않을 수 있습니다.
- 전체 업로드 -> 파싱 -> 청킹 -> 임베딩 -> 검색까지의 E2E 테스트는 아직 부족합니다.
- 운영 보안 설정, 백업/복구 절차, 모니터링은 별도 점검이 필요합니다.

---

## 기술 스택

- Web: Django, Gunicorn
- Reverse proxy: Nginx
- Database: PostgreSQL, pgvector
- Queue: Redis, Celery, Celery beat
- Document AI: Docling, BGE-M3, FlagEmbedding
- Runtime: Docker Compose

---

## 서비스 구성

```text
nginx
  -> app
      - 웹 요청 처리
      - 파일 저장/다운로드/목록
      - AI 작업 큐잉

redis
  -> Celery broker

celery-worker
  - parse, embed queue 처리
  - 문서 파싱/청킹/임베딩 실행

celery-search-worker
  - search queue 처리
  - 검색 query embedding 생성
  - pgvector 검색 결과 저장

celery-text2sql-worker
  - text2sql queue 처리

celery-beat
  - stale 상태의 파싱/임베딩 작업 복구

db
  - Django 데이터
  - pgvector 임베딩
```

`document_ai`는 별도 HTTP 서버가 아니라, 같은 Django 코드베이스 안의 앱입니다. 다만 무거운 실행은 `app`이 아니라 `celery-worker` 컨테이너에서 수행합니다.

---

## 의존성 구조

의존성은 컨테이너 역할별로 분리되어 있습니다.

- `app/requirements.web.txt`: 웹 서버용 최소 의존성
- `app/requirements.ai.txt`: 파싱/청킹/임베딩/검색 worker용 AI 의존성
- `app/requirements.dev.txt`: 개발/테스트 의존성
- `app/requirements.txt`: 로컬 전체 설치용 aggregate 파일

Docker 빌드 인자는 다음처럼 사용됩니다.

```text
운영 app
  INSTALL_AI_DEPS=0
  INSTALL_DEV_DEPS=0

운영 celery-worker
  INSTALL_AI_DEPS=1
  INSTALL_DEV_DEPS=0

개발 compose
  INSTALL_DEV_DEPS=1
```

이 구조 덕분에 웹 서버는 Docling, Torch, FlagEmbedding 같은 무거운 패키지를 설치하지 않고 빠르게 시작할 수 있습니다.

---

## 시작하기

### 1. 사전 설치

- Docker Engine 또는 Docker Desktop
- Docker Compose v2

Windows에서는 Docker Desktop의 WSL2 연동이 필요할 수 있습니다.

### 2. 프로젝트 클론

```bash
git clone https://github.com/smallwanderer/local-openshelf.git
cd local-openshelf
```

### 3. 환경변수 설정

```bash
cp .env.example .env
```

운영 환경에서는 `.env`의 기본값을 반드시 점검하세요. 특히 `DJANGO_SECRET_KEY`, DB 계정, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`는 실제 환경에 맞게 변경해야 합니다.

### 4. 운영 형태로 실행

기본 `docker-compose.yml`은 운영에 가까운 구성입니다.

```bash
docker compose up -d --build
```

접속 주소:

```text
http://localhost/
```

초기 DB 설정:

```bash
docker compose exec app python manage.py migrate
docker compose exec app python manage.py createsuperuser
```

관리자 페이지:

```text
http://localhost/admin/
```

### 5. 개발 형태로 실행

개발 서버는 별도 compose 파일을 사용합니다.

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

접속 주소:

```text
http://localhost:8888/
```

개발 DB 설정:

```bash
docker compose -f docker-compose.dev.yml exec app python manage.py migrate
docker compose -f docker-compose.dev.yml exec app python manage.py createsuperuser
```

테스트:

```bash
docker compose -f docker-compose.dev.yml exec app python -m pytest
```

`No module named pytest`가 나오면 운영용 `app` 이미지나 로컬 Python에서 테스트가 실행된 것입니다. 테스트는 dev 이미지에서만 실행합니다.

```bash
docker compose -f docker-compose.dev.yml build app
docker compose -f docker-compose.dev.yml up -d app
docker compose -f docker-compose.dev.yml exec app python -m pytest
```

GitHub Actions의 unit test는 `docling_parser` import 수집 때문에 AI 의존성이 필요합니다. CI에서는 dev compose의 `celery-core-worker` 이미지로 실행합니다.

```bash
cp .env.example .env.dev
docker compose -f docker-compose.dev.yml build celery-core-worker
docker compose -f docker-compose.dev.yml run --rm celery-core-worker python -m pytest -m "unit"
```

---

## 주요 운영 명령

```bash
# 운영 로그 확인
docker compose logs -f app
docker compose logs -f celery-worker
docker compose logs -f celery-search-worker

# 개발 로그 확인
docker compose -f docker-compose.dev.yml logs -f app
docker compose -f docker-compose.dev.yml logs -f celery-core-worker
docker compose -f docker-compose.dev.yml logs -f celery-search-worker

# 운영 중지
docker compose down

# 개발 중지
docker compose -f docker-compose.dev.yml down
```

---

## 디렉터리 구조

```text
app/
  config/        Django 설정, Celery 설정
  accounts/      계정, 인증, 이메일 인증
  files/         파일/폴더/휴지통/저장소 기능
  document_ai/   파싱, 청킹, 임베딩, 검색, 복구 작업
  search_engine/ 자연어 파일 검색 쿼리 해석 실험 코드
  templates/     공통 템플릿

data/
  uploads/       업로드 파일
  pgdata/        PostgreSQL 데이터
  logs/          애플리케이션 로그
  staticfiles/   collectstatic 결과

nginx/
  default.conf   운영용 Nginx 설정
  dev.conf       개발용 Nginx 설정
```

---

## 개발 메모

- `app` 컨테이너는 웹 요청 안정성을 위해 AI 의존성을 설치하지 않습니다.
- 파일 업로드 후 파싱/임베딩은 Celery worker가 비동기로 처리합니다.
- AI 검색 query embedding도 `celery-search-worker`가 비동기로 처리합니다.
- `celery-worker`가 중단되어도 파일 저장/다운로드는 계속 동작해야 합니다.
- AI 파이프라인 상태는 DB에 저장되며, stale 상태는 Celery beat 복구 작업이 재큐잉합니다.
- 파일 목록의 그리드/리스트 선택은 브라우저 `localStorage`에 저장됩니다.
- 파일 목록의 AI 상태 배지는 `app/static/assets/status/`의 SVG를 사용합니다.
- `data/uploads/`, `data/pgdata/`, `data/logs/`, `data/staticfiles/`는 런타임 데이터입니다.

---

## 향후 계획

- 검색 job UX 개선과 timeout/retry 정책 정리
- 파싱/임베딩 worker의 queue/load 기반 throttling 추가
- 업로드부터 검색까지의 E2E 테스트 확장
- 관리자용 AI 파이프라인 상태/복구 화면 추가
- 운영 보안, 백업/복구, 모니터링 문서화
