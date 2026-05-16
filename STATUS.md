# STATUS

기준일: 2026-05-16  
브랜치: `main`

## 1. 전체 상태

OpenShelf는 현재 파일 스토리지의 핵심 기능과 문서 AI 파이프라인의 1차 구현이 들어간 상태입니다.

가장 최근 변경의 핵심은 **웹 서버와 무거운 AI 실행 환경의 분리**입니다. `app` 컨테이너는 파일 저장/다운로드/목록/인증 같은 웹 기능을 빠르게 처리하도록 가볍게 유지하고, 파싱/청킹/임베딩은 `celery-worker` 컨테이너에서 실행하도록 정리했습니다.

## 2. 구현 완료

### 2.1 파일 스토리지

- 사용자 계정, 로그인, 로그아웃
- 이메일 인증 상태 기반 접근 제어
- 파일 및 폴더 업로드/조회/이동/삭제
- 사용자별 저장 공간 사용량 계산
- 휴지통 이동, 복원, 하위 폴더 전파 처리
- 휴지통 7일 보관 기준 메타데이터 표시
- 휴지통 파일의 검색 결과 제외

### 2.2 문서 AI 파이프라인

- 파일 업로드 시 Celery task 큐잉
- Docling 기반 문서 파싱
- 텍스트/마크다운/일부 HWP/HWPX 문서 처리
- 청크 생성 및 메타데이터 저장
- BGE-M3 기반 dense/sparse 임베딩 생성
- pgvector 기반 임베딩 저장
- 파일 상세/목록 화면에서 parse/embedding 상태 표시
- 파일 목록의 AI 상태를 업로드/파싱/임베딩 완료 배지로 표시
- 그리드/리스트 뷰 선택을 브라우저 `localStorage`에 저장
- `parse`, `embed`, `text2sql` queue 구성
- `search` queue와 검색 전용 Celery worker 구성
- AI 검색 query embedding을 웹 서버에서 worker로 분리
- 검색 요청을 `SearchJob`으로 저장하고 polling API로 결과 조회
- Celery beat 기반 backlog 복구 작업
- stale 상태의 parse/embed 작업 재큐잉
- Redis lock 기반 중복 복구 큐잉 완화
- 복구 시도 횟수와 마지막 복구 시각 메타데이터 저장

### 2.3 실행/배포 구조

- 기본 `docker-compose.yml`을 운영에 가까운 구성으로 정리
- `docker-compose.dev.yml`을 개발용 구성으로 추가
- 기존 `docker-compose.prod.yml`, `nginx/prod.conf` 제거
- 운영 기본 실행에서 `prod` 접미사 제거
- 개발 실행은 `docker compose -f docker-compose.dev.yml ...` 사용
- Nginx 운영/개발 설정 분리
- 정적 파일 `collectstatic` 및 `/static/` 제공 경로 정리

### 2.4 의존성 분리

- `requirements.web.txt`: 웹 서버 최소 의존성
- `requirements.ai.txt`: Docling, Torch 계열, FlagEmbedding, HWP 파서 등 AI worker 의존성
- `requirements.dev.txt`: 테스트 의존성, pytest/pytest-django 포함
- `requirements.txt`: 로컬 전체 설치용 aggregate 파일
- Dockerfile에 `INSTALL_AI_DEPS`, `INSTALL_DEV_DEPS` 빌드 인자 추가
- 운영 `app` 이미지는 AI 의존성 없이 빌드
- 운영 `celery-worker` 이미지만 AI 의존성을 포함

### 2.5 안정화/테스트

- storage helper 테스트
- 파일/휴지통 동작 테스트
- HWP 파싱 회귀 테스트
- retriever hybrid ranking 테스트
- document pipeline recovery 테스트
- embedding backend 테스트 일부
- pytest 설정 정리
- 테스트 실행 기준을 dev compose의 `app` 컨테이너로 고정
- GitHub Actions unit test는 AI 의존성이 포함된 dev `celery-core-worker` 이미지에서 실행하도록 수정

## 3. 현재 제약

### 3.1 AI 검색 UX/운영 정책 미완성

현재 파일 업로드 후 문서 파싱/임베딩과 AI 검색 query embedding은 worker로 분리되었습니다. 검색 요청은 `SearchJob`으로 저장되고, `celery-search-worker`가 query embedding과 pgvector 검색을 수행합니다.

다만 검색 UX는 초기 polling 방식입니다. timeout, 취소, 재시도, 중복 검색 dedup, 오래된 job 정리 정책은 아직 정리되지 않았습니다. Celery broker도 Redis를 유지하고 있어 RabbitMQ 전환 여부는 별도 검토가 필요합니다.

### 3.2 E2E 검증 부족

단위/회귀 테스트는 늘었지만 다음 흐름을 한 번에 검증하는 E2E 테스트는 아직 부족합니다.

```text
업로드 -> 파싱 -> 청킹 -> 임베딩 -> 검색 -> 휴지통 -> 복원 -> 복구
```

### 3.3 운영 관측성 부족

- worker queue 길이와 처리량을 보는 대시보드 없음
- 파일별 AI 파이프라인 이력 화면 없음
- 복구 작업 결과를 운영자가 쉽게 확인하는 UI 없음
- 로그는 남지만 관리자용 상태 확인 기능은 제한적

### 3.4 운영 보안/백업 미정리

- 운영 도메인, CSRF, ALLOWED_HOSTS 설정은 환경별 점검 필요
- DB/업로드 파일 백업 정책 필요
- 비밀값 관리 방식 정리 필요
- 배포 후 모니터링/알림 구성 필요

## 4. 주요 리스크

- `celery-worker`가 중단되면 파일 저장/다운로드는 유지되지만 AI 처리는 지연됩니다.
- 긴 파싱/임베딩 작업이 같은 worker pool을 점유하면 여러 파일의 AI 처리 대기 시간이 길어질 수 있습니다.
- AI 검색은 비동기 job 기반으로 동작하므로 검색 결과 표시까지 지연이 발생할 수 있습니다.
- `celery-search-worker`가 중단되면 파일 저장/다운로드는 유지되지만 AI 검색 결과가 완료되지 않습니다.
- 복구 로직은 중복 큐잉을 완화하지만, queue load 기반 throttle은 아직 없습니다.
- 실제 대용량 파일/다중 업로드 상황의 부하 테스트가 부족합니다.

## 5. 다음 작업 우선순위

### 5.1 최우선

- 검색 job polling UX 개선
- 검색 job timeout, 재시도, 실패 표시 정책 정리
- 웹 `app` 컨테이너가 AI 의존성 없이도 모든 비-AI 기능을 안정적으로 제공하는지 검증
- 파일 목록/상세 화면에서 AI 처리 상태를 사용자가 이해하고 조작할 수 있게 개선

### 5.2 단기

- 파일 업로드 진행률 표시
- 파일 목록 다중 선택과 일괄 삭제/복원/이동
- 일반 검색과 AI 검색 UI 분리
- AI 처리 실패 파일에 대한 재시도 버튼 추가
- 오래된 SearchJob 정리 정책 추가
- Celery queue/load 기반 복구 throttling 추가
- parse/embed worker 분리 여부를 실제 부하 기준으로 결정
- 업로드 -> 파싱 -> 임베딩까지의 통합 테스트 추가
- Docker Compose 환경에서 `docker compose config` 및 smoke test 자동화

### 5.3 중기

- 관리자용 AI 파이프라인 상태 화면 추가
- 파일별 파싱/임베딩 실패 원인과 재시도 버튼 제공
- 대용량 파일 처리 정책 정리
- worker 메모리 제한과 재시작 정책 조정
- 파일 미리보기, 즐겨찾기, 태그 기능 추가
- 검색 결과에서 파일 열기/다운로드/위치 이동 동선 개선

### 5.4 장기

- 운영 백업/복구 문서화
- 로그/메트릭/알림 체계 구축
- 검색 품질 평가 데이터셋 확장
- 팀 공유, 권한 모델, 감사 로그 확장
- 공유 링크, 만료 링크, 읽기 전용 공유 기능 검토

## 6. 사용성 개선 계획

현재 백엔드 기능은 어느 정도 갖춰졌지만, 사용자가 상태를 이해하고 직접 조작하는 UX는 아직 부족합니다. 다음 개선은 “파일 저장소로서의 기본 사용성”과 “AI 파이프라인의 투명성”을 우선합니다.

### 6.1 파일 관리 UX

현재 기능:

- 파일/폴더 업로드
- 파일/폴더 목록 조회
- 파일 상세 보기
- 파일 다운로드
- 파일/폴더 삭제
- 휴지통 이동 및 복원
- 저장 공간 사용량 표시

개선 사항:

- 드래그 앤 드롭 업로드
- 업로드 진행률 표시
- 다중 선택
- 일괄 삭제, 복원, 이동
- 폴더 breadcrumb 개선
- 정렬 옵션 확대: 이름, 크기, 업로드일, 수정일, AI 처리 상태
- 보기 모드 추가: 리스트, 그리드
- 파일 미리보기: 이미지, PDF, 텍스트, Markdown

추가 기능 후보:

- 최근 파일
- 즐겨찾기
- 태그
- 파일 설명/메모
- 파일 버전 관리
- 공유 링크
- 만료되는 공유 링크
- 읽기 전용 공유
- 폴더 단위 공유

### 6.2 AI 처리 상태 UX

현재 기능:

- 업로드 후 파싱 task 큐잉
- 파싱/청킹/임베딩 비동기 처리
- 파일별 parse/embedding 상태 표시
- 파일 목록에서 업로드 완료, 파싱 완료, 임베딩 완료 배지 표시
- stale 상태 복구 task

개선 사항:

- 상세 화면에 단계별 진행 상태 표시
- 상태 구분 명확화: 파싱 대기, 파싱 중, 임베딩 중, 완료, 실패
- 실패 원인 요약 표시
- “다시 분석” 버튼
- “AI 분석 제외” 옵션
- 분석 대상 확장자/파일 크기 정책 표시

추가 기능 후보:

- 파일 요약
- 폴더 단위 요약
- 문서 Q&A
- 여러 문서 대상 질의응답
- 자동 태그 추천
- 유사 문서 찾기
- 중복/유사 파일 탐지
- 문서 언어 감지
- OCR 처리 상태 표시

### 6.3 검색 UX

현재 기능:

- 파일명 기반 검색
- AI 본문 검색 UI 일부
- pgvector 기반 문서 임베딩 검색 구조
- `SearchJob` 기반 비동기 AI 검색
- `celery-search-worker` 기반 query embedding 처리

개선 사항:

- 일반 검색과 AI 검색 모드 분리
- 일반 검색은 파일명, 확장자, 날짜, 상태 중심으로 정리
- AI 검색은 문서 본문 의미 검색 중심으로 정리
- AI 검색 결과에 evidence 표시
- 검색 결과에서 파일 열기, 다운로드, 위치 이동 제공
- 검색 실패/AI worker 미준비 상태 안내
- 검색 job timeout, 취소, 재시도 UX

추가 기능 후보:

- 검색 필터 저장
- 최근 검색어
- 고급 검색 패널
- 특정 폴더 안에서만 검색
- 특정 파일 유형만 검색

### 6.4 복구/운영 UX

현재 기능:

- Celery beat 기반 복구
- Redis lock 기반 중복 복구 완화
- 복구 시도 횟수와 마지막 복구 시각 저장

개선 사항:

- 관리자/디버그 페이지에서 AI 파이프라인 상태 표시
- 실패 파일 목록
- stale 파일 목록
- 파일별 재시도 버튼
- 전체 재큐잉 버튼
- worker 동작 여부 표시
- queue 길이와 처리량 표시

추가 기능 후보:

- Celery task 취소/재시도 UI
- 작업 이력 로그
- 실패 원인별 필터
- 운영자 알림
- 로그 다운로드

## 7. 현재 권장 운영 방식

운영:

```bash
docker compose up -d --build
```

개발:

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

운영에서 우선 확인할 컨테이너:

```bash
docker compose logs -f app
docker compose logs -f celery-worker
docker compose logs -f celery-search-worker
docker compose logs -f celery-beat
```

현재 목표는 `app`의 생존성을 AI 작업 부하와 분리하는 것입니다. 따라서 `app`이 먼저 안정적으로 뜨고 파일 저장/다운로드가 유지되는지 확인한 뒤, `celery-worker`의 파싱/임베딩 처리량을 별도로 조정하는 방식이 적절합니다.
