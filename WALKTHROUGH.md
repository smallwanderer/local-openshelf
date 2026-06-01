# OpenShelf Walkthrough

이 문서는 OpenShelf를 서버 또는 Windows 환경에서 실행하는 절차를 설명합니다.

## 1. 실행 방식 선택

OpenShelf에는 두 가지 Compose 실행 방식이 있습니다.

| 목적 | Compose 파일 | 접속 주소 |
| --- | --- | --- |
| 서버/운영에 가까운 실행 | `docker-compose.yml` | `http://localhost/` 또는 서버 도메인 |
| Windows/로컬 개발 실행 | `docker-compose.dev.yml` | `http://localhost:8888/` |

첫 설치나 기능 확인은 개발 실행 방식이 더 편합니다. 실제 서버에 올릴 때는 기본 `docker-compose.yml`을 사용합니다.

## 2. 공통 준비

필요한 도구:

- Git
- Docker Engine 또는 Docker Desktop
- Docker Compose v2
- Hugging Face token

프로젝트를 받습니다.

```bash
git clone https://github.com/smallwanderer/local-openshelf.git
cd local-openshelf
```

릴리즈 tag로 실행하려면 다음처럼 checkout합니다.

```bash
git fetch --tags
git checkout v0.1.1
```

## 3. 서버에서 실행하기

Ubuntu 계열 서버 기준 예시입니다.

### 3.1 환경 파일 생성

```bash
cp .env.example .env
```

반드시 확인할 값:

```env
DJANGO_SECRET_KEY=change-me
DJANGO_ALLOWED_HOSTS=your-domain.example.com,server-ip
DJANGO_CSRF_TRUSTED_ORIGINS=https://your-domain.example.com,http://server-ip
POSTGRES_USER=change-me
POSTGRES_PASSWORD=change-me
HF_TOKEN=hf_your_token_here
```

도메인 없이 IP로만 테스트한다면 `DJANGO_ALLOWED_HOSTS`에 서버 IP를 넣습니다.

### 3.2 실행

```bash
docker compose up -d --build
```

상태 확인:

```bash
docker compose ps
docker compose logs -f app
```

### 3.3 관리자 계정 생성

`app` 컨테이너는 시작 시 migrate를 수행합니다. 관리자 계정은 별도로 생성합니다.

```bash
docker compose exec app python manage.py createsuperuser
```

접속:

```text
http://your-domain.example.com/
http://your-domain.example.com/admin/
```

도메인이 없으면:

```text
http://server-ip/
http://server-ip/admin/
```

### 3.4 문서 업로드 후 AI 검색/RAG 사용

1. 웹 UI에 로그인합니다.
2. 문서를 업로드합니다.
3. 파일 목록에서 AI 처리 상태가 완료될 때까지 기다립니다.
4. 일반 검색 또는 AI 검색을 실행합니다.
5. 좌측 RAG 질문 화면에서 문서 기반 질문을 입력합니다.

파싱과 임베딩은 비동기 작업이므로 업로드 직후에는 검색되지 않을 수 있습니다.

### 3.5 서버 운영 명령

```bash
docker compose logs -f app
docker compose logs -f celery-core-worker
docker compose logs -f celery-search-worker
docker compose logs -f celery-llm-rag-worker
```

재시작:

```bash
docker compose restart app nginx celery-search-worker celery-llm-rag-worker
```

환경변수 변경 후 반영:

```bash
docker compose up -d --force-recreate app celery-search-worker celery-llm-rag-worker nginx
```

전체 중지:

```bash
docker compose down
```

## 4. Windows에서 실행하기

권장 방식은 Docker Desktop + WSL2입니다.

### 4.1 Windows 준비

1. Docker Desktop 설치
2. Docker Desktop Settings에서 WSL2 backend 활성화
3. Ubuntu WSL 설치 권장
4. WSL 터미널에서 프로젝트 clone

WSL 내부 예시:

```bash
cd ~
git clone https://github.com/smallwanderer/local-openshelf.git
cd local-openshelf
```

Windows 파일 시스템 경로(`/mnt/c/...`)에서도 실행할 수 있지만, 대량 파일과 Docker volume 성능을 고려하면 WSL 내부 홈 디렉터리를 권장합니다.

### 4.2 개발 환경 파일 생성

```bash
cp .env.example .env.dev
```

확인할 값:

```env
DJANGO_DEBUG=1
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8888,http://127.0.0.1:8888
HF_TOKEN=hf_your_token_here
```

### 4.3 개발 stack 실행

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

접속:

```text
http://localhost:8888/
```

관리자 계정:

```bash
docker compose -f docker-compose.dev.yml exec app python manage.py createsuperuser
```

관리자 페이지:

```text
http://localhost:8888/admin/
```

### 4.4 Windows에서 로그 확인

```bash
docker compose -f docker-compose.dev.yml logs -f app
docker compose -f docker-compose.dev.yml logs -f celery-core-worker
docker compose -f docker-compose.dev.yml logs -f celery-search-worker
docker compose -f docker-compose.dev.yml logs -f celery-llm-rag-worker
```

### 4.5 Windows에서 테스트 실행

CI와 같은 unit test:

```bash
docker compose -f docker-compose.dev.yml run --rm celery-core-worker python -m pytest -m "unit"
```

개발 app 컨테이너에서 웹/API 중심 테스트:

```bash
docker compose -f docker-compose.dev.yml exec app python manage.py check
docker compose -f docker-compose.dev.yml exec app python -m pytest --reuse-db files/tests.py tests/test_rag_flow.py
```

웹 `app` 컨테이너는 AI 의존성을 가볍게 유지합니다. Docling 기반 테스트는 `celery-core-worker`에서 실행합니다.

## 5. 자주 발생하는 문제

### 5.1 웹이 502를 반환함

확인:

```bash
docker compose ps
docker compose logs -f app
docker compose logs -f nginx
```

개발 환경:

```bash
docker compose -f docker-compose.dev.yml ps
docker compose -f docker-compose.dev.yml logs -f app
docker compose -f docker-compose.dev.yml logs -f nginx
```

대부분은 `app` 컨테이너가 아직 시작 중이거나 migration/env 설정 오류가 있는 경우입니다.

### 5.2 RAG 답변이 생성되지 않음

확인할 컨테이너:

```bash
docker compose ps llm-parser celery-llm-rag-worker celery-search-worker
docker compose logs -f llm-parser
docker compose logs -f celery-llm-rag-worker
```

개발 환경:

```bash
docker compose -f docker-compose.dev.yml ps llm-parser celery-llm-rag-worker celery-search-worker
docker compose -f docker-compose.dev.yml logs -f llm-parser
docker compose -f docker-compose.dev.yml logs -f celery-llm-rag-worker
```

`HF_TOKEN`이 없거나 모델 다운로드가 실패하면 `llm-parser`가 정상 준비되지 않을 수 있습니다.

### 5.3 문서를 업로드했는데 AI 검색이 안 됨

파싱/임베딩 worker 상태를 확인합니다.

```bash
docker compose logs -f celery-core-worker
docker compose logs -f celery-search-worker
```

개발 환경:

```bash
docker compose -f docker-compose.dev.yml logs -f celery-core-worker
docker compose -f docker-compose.dev.yml logs -f celery-search-worker
```

파일 업로드 직후에는 파싱과 임베딩이 아직 완료되지 않았을 수 있습니다.

### 5.4 pytest가 없다는 오류

운영용 `app` 이미지에서는 dev/test 의존성이 없을 수 있습니다. 개발 compose 또는 `celery-core-worker` 테스트 명령을 사용합니다.

```bash
docker compose -f docker-compose.dev.yml run --rm celery-core-worker python -m pytest -m "unit"
```

## 6. 업데이트 절차

소스 release tag 기준으로 업데이트하는 예시입니다.

```bash
git fetch --tags
git checkout v0.1.1
docker compose up -d --build
docker compose exec app python manage.py migrate
```

개발 환경:

```bash
git fetch --tags
git checkout v0.1.1
docker compose -f docker-compose.dev.yml up -d --build
docker compose -f docker-compose.dev.yml exec app python manage.py migrate
```

## 7. 데이터 위치

주요 runtime 데이터:

```text
data/uploads      uploaded files
data/pgdata       PostgreSQL data
data/logs         application logs
data/staticfiles  collected static files
```

운영 전에는 이 경로에 대한 백업 정책을 별도로 준비해야 합니다.
