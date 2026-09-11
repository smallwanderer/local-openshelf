# Dotori 현재 API 계약 v1

> 기준일: 2026-08-29
> 상태: SPA 전환 MVP인 0–8단계와 사용자 CLI(9단계) 계약 적용 완료. 이 문서는 현재 Django 구현과 template·SPA·CLI가 실제로 사용하는 계약을 기록합니다.

이 문서는 `web/` SPA와 `clients/dotori-cli/` CLI가 실제로 사용하는 HTTP 계약을 고정한 참조 자료입니다. Dotori 서버에 대해 직접 HTTP 클라이언트를 작성하려는 경우에도 이 문서가 기준입니다.
실제 사용 절차가 필요하다면 [설치 가이드](./installation-guide.md), [운영 가이드](./operation-guide.md), CLI 사용법(`clients/dotori-cli/README.md`)을 참고하세요.

## 목차

1. [목적과 적용 범위](#목적과-적용-범위)
2. [소비자와 인증 경계](#소비자와-인증-경계)
3. [SPA shell과 HTTP 라우팅 경계](#spa-shell과-http-라우팅-경계)
4. [현재 endpoint 목록](#현재-endpoint-목록)
5. [대표 데이터 모델](#대표-데이터-모델)
6. [기존 template UI가 의존하는 필드](#기존-template-ui가-의존하는-필드)
7. [변경 관리 규칙](#변경-관리-규칙)

---

## 목적과 적용 범위

이 문서는 `web/` SPA 연결 전에 현재 HTTP 경계를 고정하기 위한 자료입니다. 현재 동작을 그대로 재사용할 부분과 후속 단계에서 의도적으로 바꿀 부분을 구분하며, Django 내부 서비스나 데이터베이스 모델을 외부 계약으로 간주하지 않습니다.

- SPA, 기존 Django template, 사용자 CLI가 사용하는 HTTP 계약을 대상으로 합니다.
- 폴더 동기화 API와 검색 tuning API는 별도 소비자 경계로 기록합니다.
- Django Admin, management command, 설치 스크립트는 HTTP 사용자 API에 포함하지 않습니다.
- 외부 문서 식별자는 UUID 문자열인 `uid` 또는 `node_id`를 기준으로 합니다.
- 이 기준선의 핵심 동작은 `app/tests/test_web_api_contract.py`에서 검증합니다.

---

## 소비자와 인증 경계

| 구분 | 현재 인증 | CSRF | 적용 방향 |
|---|---|---|---|
| `/api/accounts/v1/session/` | 공개 bootstrap | GET 응답에서 cookie 발급 | SPA 핵심 API |
| `/api/accounts/v1/login/`, `/logout/` | Django session | 필요 | 로그인 필수 배포의 SPA 인증 API |
| `/files/api/v1/*` | Django session, 로그인 및 이메일 인증 decorator | 변경 요청에 필요 | SPA 핵심·보조 API로 재사용 |
| `/api/document-ai/v1/search/` | DRF session, 로그인 및 이메일 인증 | browser session 요청에서 token 전송 | SPA 핵심 API로 재사용 |
| `/api/document-ai/v1/rag/stream/` | Django session, 로그인 및 이메일 인증 | 필요 | SPA 핵심 API로 재사용 |
| `/api/document-ai/v1/server-policy/` | Django session, 로그인 및 이메일 인증 | GET | SPA 읽기 전용 서버 정책 요약 |
| `/api/document-ai/v1/operations/*` | 검증된 staff Django session | GET/POST 수집에 필요 | 서버 전체 운영 요약 전용 |
| `/api/document-ai/v1/tuning/` | DRF session | browser session 요청에서 token 전송 | 운영·실험 sandbox 전용 |
| `/api/sync/v1/*` | `Authorization: Bearer <APIToken>` | 면제 | 폴더 동기화 클라이언트 전용 |
| `/api/cli/v1/*` | `Authorization: Bearer <CLIToken>` | 면제 | 독립 사용자 CLI 전용 |
| `/accounts/*` | Django session/form | 필요 | 기존 HTML 계정 화면으로 유지 |

---

## SPA shell과 HTTP 라우팅 경계

| 경로 | 응답 | cache | 비고 |
|---|---|---|---|
| `/` | 인증된 SPA HTML shell | `no-store` | `DOTORI_SPA_ENABLED=0`이면 `/files/`로 임시 redirect |
| `/workspace/` | 인증된 SPA HTML shell | `no-store` | 기본 문서 화면 |
| `/workspace/<client-path>/` | 인증된 SPA HTML shell | `no-store` | home, documents, search, chat, settings, operations 직접 접근·새로고침 |
| `/static/spa/*` | Vite fingerprinted JS·CSS | `public, immutable` | Django WhiteNoise가 같은 origin에서 제공 |
| `/files/*`의 기존 HTML route | Django template | 기존 정책 | 전환 중 실행 가능한 rollback UI |

SPA fallback은 `/workspace/` 아래로 제한되며 전역 catch-all이 아닙니다. 따라서 `/api/*`, `/files/api/*`, 파일 다운로드, `/accounts/*`, `/admin/*`, `/swagger/*`, `/redoc/*`는 기존 Django route가 먼저 처리되는 것이 아니라 애초에 fallback 범위 밖에 있습니다. 로그인 필수 배포에서 미인증 shell 요청은 `/accounts/login/?next=...`로 이동하고, JSON API는 기존 JSON `401` 계약을 유지합니다.

운영 Compose는 Node.js 서버를 실행하지 않습니다. Node.js는 이미지 build 중 Vite 산출물을 만드는 임시 stage에서만 사용하며 최종 `app` 이미지에는 존재하지 않습니다.

1단계에서 SPA가 사용하는 session 인증 오류 경계를 다음과 같이 통일했습니다.

- `files`, 검색, RAG API의 미인증 요청은 JSON `401`과 `AUTHENTICATION_REQUIRED`를 반환합니다.
- 이메일 미인증 요청은 JSON `403`과 `EMAIL_VERIFICATION_REQUIRED`를 반환합니다.
- Django와 DRF의 API CSRF 실패는 JSON `403`과 `CSRF_FAILED`를 반환합니다.
- API 400/403/404/500 handler는 HTML 대신 공통 JSON 오류를 반환합니다.
- sync API는 인증 실패를 `{"ok": false, "errors": [...]}` 형태의 JSON `401` 또는 `403`으로 반환합니다.

기존 file business validation과 sync API의 `errors[]` 응답은 호환성을 위해 아직 유지합니다. SPA의 공통 HTTP adapter는 공통 `error`를 우선 해석하고 과도기 응답을 보조적으로 처리합니다.

## 현재 endpoint 목록

### SPA session API

| Method | Path | 현재 입력/응답 요약 | 인증 모드 |
|---|---|---|---|
| `GET` | `/api/accounts/v1/session/` | 현재 auth mode·사용자 정보 반환, CSRF cookie 발급 | 공개 |
| `POST` | `/api/accounts/v1/login/` | JSON `email`, `password`; 인증 후 session과 갱신된 CSRF cookie | `LOGIN_REQUIRED=1` |
| `POST` | `/api/accounts/v1/logout/` | 현재 session 종료 | `LOGIN_REQUIRED=1` |
| `GET` | `/api/accounts/v1/tokens/` | 현재 사용자의 CLI·폴더 동기화 token 종류·prefix·scope·상태 통합 목록 | 검증된 session |
| `POST` | `/api/accounts/v1/tokens/` | JSON `name`, `token_type=cli\|sync`; CLI는 `access_level=read_only\|read_write`를 사용하고 생략 시 `read_only`. 선택한 종류의 원문 secret을 한 번만 반환 | 검증된 session + CSRF |
| `DELETE` | `/api/accounts/v1/tokens/<token_type>/<id>/` | 현재 사용자 소유 token 폐기 | 검증된 session + CSRF |
| `GET` | `/api/accounts/v1/cli-tokens/` | 현재 사용자의 CLI token prefix·scope·상태 목록 | 검증된 session |
| `POST` | `/api/accounts/v1/cli-tokens/` | JSON `name`, `access_level=read_only\|read_write`; 원문 secret을 한 번만 반환. `enable_sync=true` 혼합 발급은 거부 | 검증된 session + CSRF |
| `DELETE` | `/api/accounts/v1/cli-tokens/<uid>/` | 현재 사용자 소유 CLI token 폐기 | 검증된 session + CSRF |

bootstrap 응답:

```json
{
  "ok": true,
  "auth": {
    "mode": "required",
    "login_required": true,
    "authenticated": false
  },
  "user": null
}
```

인증된 경우 `user`는 `email`, `display_name`, `email_verified`, `is_staff`를 포함합니다. `LOGIN_REQUIRED=0`에서는 `LocalProfileMiddleware`가 서버의 활성 로컬 프로필을 준비하므로 `mode`는 `local`, `authenticated`는 `true`입니다. 이 모드의 password login과 logout은 각각 `LOGIN_NOT_REQUIRED`, `LOGOUT_NOT_AVAILABLE` 오류로 거부됩니다.

### 문서·폴더 API

모든 `/files/api/v1/*` endpoint는 session 로그인과 이메일 인증을 요구합니다. `GET` 이외의 요청에는 같은 origin CSRF token을 전송합니다. `/files/healthcheck/`는 이 인증 경계 밖의 운영 probe입니다.

| Method | Path | 현재 입력/응답 요약 | 소비자 | 상태 |
|---|---|---|---|---|
| `GET` | `/files/api/v1/files/` | query: `q`, `parent_id`, `tag`, `page`, `limit`; `ok`, `files`, `page`, `limit`, `total`, `has_next` | SPA/template | 재사용, pagination 보강 완료 |
| `POST` | `/files/api/v1/upload/` | multipart: `file`, `parent_id?`, `description?`, `ai_processing_enabled?`; `file`, `status`, `warnings` | SPA/template | 재사용 |
| `POST` | `/files/api/v1/create_folder/` | form: `name`, `parent_id?`; `folder` | SPA/template | 재사용 |
| `GET` | `/files/api/v1/folders/` | `folders[{uid,name,path}]` | SPA/template | 재사용 |
| `GET` | `/files/api/v1/rag/scope-nodes/` | query: `q?`, `limit?`; `nodes`, `count`, `limit` | SPA/template | 재사용 |
| `GET` | `/files/api/v1/storage/` | 사용자 저장공간 사용량 | SPA 보조 | 재사용 |
| `GET` | `/files/api/v1/ai/readiness/` | parsing/embedding 준비 상태 집계 | SPA/template | 재사용 |
| `GET` | `/files/api/v1/ai/search-history/` | query: `limit?`; 완료된 RAG 기록 | SPA/template | 재사용 |
| `POST` | `/files/api/v1/bulk/delete/` | JSON: `uids[]`; 선택 항목 휴지통 이동 | SPA/template | 재사용 |
| `POST` | `/files/api/v1/bulk/restore/` | JSON: `uids[]`; 부분 실패 시 `207` | SPA/template | 재사용 |
| `POST` | `/files/api/v1/bulk/move/` | JSON: `uids[]`, `parent_id`; 부분 실패 시 `207` | SPA/template | 재사용 |
| `GET` | `/files/api/v1/<uid>/` | `file` 상세 | SPA | 재사용, 상세 필드 보강 완료 |
| `GET` | `/files/api/v1/<uid>/parsed_text/` | `text` 또는 준비되지 않은 경우 `404` | SPA/template | 재사용 |
| `POST` | `/files/api/v1/<uid>/meta/` | JSON: `summary?`, `auto_tags?` | template | 쓰기 유지, 상세 읽기 계약 보강 완료 |
| `POST` | `/files/api/v1/<uid>/rename/` | JSON/form: `name`; `file` | SPA/template | 재사용 |
| `POST` | `/files/api/v1/<uid>/move/` | JSON/form: `parent_id`; `"root"` 또는 빈 문자열은 root | SPA/template | 재사용 |
| `POST` | `/files/api/v1/<uid>/ai/enabled/` | JSON/form: `enabled`; 처리 활성화 상태 | SPA/template | 재사용 |
| `POST` | `/files/api/v1/<uid>/ai/retry/` | 실패한 parse/embed 재등록; 처리 중이면 `409` | SPA/template | 재사용 |
| `GET` | `/files/api/v1/<uid>/download/` | 파일 body; `?inline=1`이면 inline | SPA/template | 재사용 |
| `DELETE`, `POST` | `/files/api/v1/<uid>/delete/` | 휴지통 이동 | SPA/template | 재사용 |
| `GET` | `/files/api/v1/recent/` | `files[]` | SPA/template | 재사용 |
| `GET` | `/files/api/v1/starred/` | `files[]` | SPA/template | 재사용 |
| `GET` | `/files/api/v1/trash/` | `files[]` | SPA/template | 재사용 |
| `DELETE`, `POST` | `/files/api/v1/trash/empty/` | 휴지통 전체 영구 삭제 | SPA/template | 재사용 |
| `POST` | `/files/api/v1/<uid>/restore/` | 휴지통 복구 | SPA/template | 재사용 |
| `DELETE`, `POST` | `/files/api/v1/<uid>/permanent_delete/` | 영구 삭제 | SPA/template | 재사용 |
| `POST` | `/files/api/v1/toggle_star/<uid>/` | `starred`의 변경 후 값 | SPA/template | 재사용 |
| `GET` | `/files/healthcheck/` | `{"status":"ok"}` | Docker/운영 | 사용자 SPA에서 사용하지 않음 |

`parent_id`라는 이름은 현재 요청에서는 부모 UUID를 받지만, `Node.to_dict()` 응답에서는 데이터베이스 정수 FK를 반환합니다. 외부 계약을 UUID로 통일할 때까지 이 필드는 transitional이며 SPA는 탐색에 `uid`와 URL query의 UUID만 사용합니다.

### 검색·RAG API

| Method | Path | 현재 입력/응답 요약 | 소비자 | 상태 |
|---|---|---|---|---|
| `POST` | `/api/document-ai/v1/search/` | JSON 검색 요청; 동기 `results`, `performance_metrics`, `query_plan` | SPA/template | `mode=basic`은 직접 검색, `mode=advanced`는 query-understanding 적용 |
| `POST` | `/api/document-ai/v1/rag/stream/` | JSON 질문; NDJSON event stream | SPA/template | 핵심 재사용 |
| `GET` | `/api/document-ai/v1/server-policy/` | 서버 검색 정책과 RAG·임베딩 런타임 안전 요약 | SPA | 읽기 전용, 운영 변경 기능 없음 |
| `POST` | `/api/document-ai/v1/tuning/` | 저수준 retrieval 가중치와 후보 설정 | sandbox | 일반 SPA 제외 |
| `GET` | `/api/document-ai/sandbox/` | template HTML | sandbox | SPA API 아님 |

검색 endpoint는 명시적 `mode=basic|advanced` 계약을 사용하며, 검색 tuning 파라미터는 일반 사용자 설정 UI로 노출하지 않습니다. sandbox의 용도와 접근 시 주의사항은 [운영 가이드](./operation-guide.md#하이브리드-검색-튜닝)를 참고하세요.

### 서버 운영 API

모든 endpoint는 인증되고 이메일이 검증된 `is_staff` 세션을 요구합니다. 일반 계정은 `403 PERMISSION_DENIED`, 이메일 미검증 staff는 `403 EMAIL_VERIFICATION_REQUIRED`, 로그인 필수 모드의 미인증 요청은 `401 AUTHENTICATION_REQUIRED`를 공통 JSON 형식으로 받습니다. `LOGIN_REQUIRED=0` 로컬 모드에서는 middleware가 준비한 서버 기본 관리자 세션을 사용합니다.

| Method | Path | 응답 요약 |
|---|---|---|
| `GET` | `/api/document-ai/v1/operations/status/` | App·DB·임베딩·RAG 상태, parse/embed 상태 건수와 오래된 작업, 최근 실패, 읽기 전용 서버 맥락 |
| `GET` | `/api/document-ai/v1/operations/metrics/?window=24h` | `1h`, `24h`, `7d` 기간의 업로드·검색·RAG 성공률과 5개 파이프라인 측정치 |
| `GET` | `/api/document-ai/v1/operations/events/?window=24h&limit=10` | 느린 작업 상위 5건과 최근 실패 5건을 중복 제거한 구조적 목록; 최대 20건 |
| `GET` | `/api/document-ai/v1/operations/traces/<trace_id>/` | 같은 trace의 파일·parse·embed·search·RAG 구조적 기록과 안전한 metric·metadata |
| `GET` | `/api/document-ai/v1/operations/resources/` | 서비스별 가장 최근 `ResourceSnapshot` |
| `POST` | `/api/document-ai/v1/operations/resources/collect/` | DB 연결과 uploads/logs/config 디스크 여유를 즉시 수집 |

기간 집계는 서버 전체 기록을 대상으로 합니다. 응답에는 검색어, RAG 질문, 문서 본문, 결과 본문, SQL/bind 값이 포함되지 않으며 오류 요약의 password·secret·token·API key 형태는 제거합니다. 과거 미계측 레코드는 0으로 환산하지 않고 각 metric의 `measured_count`와 `total_count`로 구분합니다. CPU·RAM·GPU와 admission active/rejection은 아직 영속 수집하지 않으므로 `null` 또는 `available: false`로 반환합니다.

### 폴더 동기화 API

모든 endpoint는 기존 sync 전용 Bearer token 또는 `sync` scope가 있는 CLI token을 요구합니다. POST endpoint는 CSRF를 사용하지 않습니다.

| Method | Path | 현재 입력/응답 요약 |
|---|---|---|
| `GET` | `/api/sync/v1/ping/` | token 검증; `ok`, `message` |
| `GET` | `/api/sync/v1/identity/` | sync credential의 실제 account, token type·scope, 예약 workspace `null` |
| `POST` | `/api/sync/v1/diff/` | `root_name`, client manifest와 서버 tree 비교; `actions`, `sync_id`, 기존 root의 `root_uid` (새 root는 빈 값). 읽기 전용 |
| `POST` | `/api/sync/v1/upload/` | multipart file, `rel_path`, `root_uid?`, `root_name`; `node_uid`, `action`, 확정된 `root_uid` |
| `POST` | `/api/sync/v1/mkdir/` | JSON `rel_path`, `root_uid?`, `root_name`; `node_uid`, `created`, 확정된 `root_uid` |
| `POST` | `/api/sync/v1/delete/` | JSON `node_uids[]`와 권장 `root_uid` 또는 `root_name`; 선택한 sync root 내부 노드만 휴지통 이동. 이전 client의 selector 없는 요청은 단일 `/sync/<root>` 내부일 때만 허용 |
| `POST` | `/api/sync/v1/confirm/` | JSON `sync_id`, `results[]`; 성공/실패 집계 |

기존 `APIToken`은 local-folder connector 호환을 위한 sync 전용 credential로 유지하며 일반 CLI 권한으로 확장하지 않습니다. 별도 `CLIToken`은 원문 대신 hash를 저장합니다. `read_only`는 문서 읽기·검색·RAG·상태 조회 scope, `read_write`는 여기에 일반 문서 쓰기 scope를 추가하며 두 종류 모두 `sync`를 포함하지 않습니다. 통합 token API의 `token_type=sync`만 신규 sync 전용 token을 발급합니다. 과거에 이미 발급된 `sync` scope 포함 CLI token은 인증 호환을 위해 계속 사용할 수 있지만 신규 혼합 발급은 허용하지 않습니다.

### 사용자 CLI API

모든 endpoint는 `dtr_cli_` prefix의 CLI Bearer token을 요구합니다. 각 endpoint는 필요한 scope를 다시 검사하고 token 소유자를 기존 파일·검색·RAG 서비스의 사용자로 전달합니다. cookie session과 CSRF는 사용하지 않습니다.

| Method | Path | 필요 scope | 현재 입력/응답 요약 |
|---|---|---|---|
| `GET` | `/api/cli/v1/identity/` | 유효한 CLI token | 실제 account ID·email·display name, token type·scope, 예약 workspace `null` |
| `GET` | `/api/cli/v1/status/` | `status:read` | 서버 operation mode와 RAG·embedding 읽기 전용 상태 |
| `GET` | `/api/cli/v1/documents/` | `documents:read` | `q`, `parent_id`, `page`, `limit`; 현재 사용자 문서·폴더 목록 |
| `POST` | `/api/cli/v1/upload/` | `documents:write` | multipart file, `parent_id?`; 업로드한 문서 정보 |
| `POST` | `/api/cli/v1/search/` | `search` | `query`, `mode`, `node_ids`, `top_k`; 동기 검색 결과와 근거 |
| `POST` | `/api/cli/v1/ask/stream/` | `rag` | 질문과 범위; 기존 event contract의 NDJSON stream |

실제 설치·설정·명령어 사용법은 `clients/dotori-cli/README.md`를 참고하세요. CLI package는 `clients/dotori-cli/`에 독립적으로 위치하며 서버 내부 모듈이나 데이터베이스를 import하지 않습니다. `remote`, `account`, `connect`, `profiles`, `status`, `list`, `upload`, `search`, `ask`, `sync`를 제공합니다. 공개 context는 schema v2의 `remote → account → workspace(null)` 구조이며 account 아래에 이름 있는 credential metadata를 두고 원문 token은 별도 credential 파일에 저장합니다. 서버 URL은 remote에 한 번만 저장하며 account 등록 시 identity endpoint로 실제 token owner와 scope를 확인합니다. `connect`와 `--profile`은 이전 client 호환 경로입니다. `sync`는 local manifest를 만든 뒤 기존 `/api/sync/v1/*`만 사용하며 기본 dry-run, 명시적 `--apply`, 별도 `--delete` 경계를 적용합니다. 서버는 `root_uid`가 가리키는 `/sync/<root_name>` 내부에서만 변경과 삭제를 허용합니다.

## 대표 데이터 모델

### 공통 API 오류

```json
{
  "ok": false,
  "error": {
    "code": "AUTHENTICATION_REQUIRED",
    "message": "Authentication is required.",
    "details": {}
  }
}
```

SPA TypeScript 계약은 `web/src/api/types.ts`의 `ApiErrorResponse`, `SessionBootstrapResponse`에 정의합니다. `code`는 프로그램 분기에 사용하고 `message`는 사용자 표시 가능한 기본 설명, `details`는 validation field나 복구 metadata에 사용합니다.

### `FileNode` 현재 응답

`Node.to_dict()`가 목록과 상세에서 공통으로 반환하는 현재 필드는 다음과 같습니다.

```json
{
  "id": 12,
  "uid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
  "name": "report.pdf",
  "ext": ".pdf",
  "node_type": "file",
  "description": "",
  "path": "/reports/report.pdf",
  "status": "ready",
  "starred": false,
  "trashed": false,
  "ai_processing_enabled": true,
  "deleted_at": null,
  "restore_until": null,
  "days_until_purge": null,
  "created_at": "2026-08-24T10:00:00+00:00",
  "updated_at": "2026-08-24T10:00:00+00:00",
  "parent_id": 4,
  "parent_uid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
  "size": 1024,
  "size_mb": 0.0,
  "mime_type": "application/pdf",
  "language": "en",
  "ai_status": {},
  "summary": "문서 요약",
  "auto_tags": ["계약", "운영"]
}
```

- 폴더에는 file 전용 `size`, `mime_type`, `language`, `ai_status`가 없습니다.
- 상세 응답은 parse result의 `summary`와 `auto_tags`를 포함하며, parse result가 없으면 각각 빈 문자열과 빈 배열을 반환합니다.
- `parent_uid`는 SPA가 사용하는 공개 부모 식별자입니다. 기존 소비자 호환을 위한 `id`와 정수 `parent_id`는 과도기 필드이며 새 SPA 타입의 장기 계약으로 고정하지 않습니다.

### 파일 목록

```json
{
  "ok": true,
  "files": [],
  "page": 1,
  "limit": 50,
  "total": 0,
  "has_next": false
}
```

`limit`은 1–100 범위로 보정되며 `total`은 현재 필터에 맞는 전체 항목 수입니다. 두 값은 기존 소비자가 무시할 수 있는 호환 필드로 추가했습니다.

### 검색 요청과 응답

```json
{
  "mode": "basic",
  "query": "계약 해지 조건",
  "top_k": 5,
  "threshold": 0.4,
  "node_ids": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
}
```

- `mode`: `basic` 또는 `advanced`. `basic`은 입력을 직접 검색하고 `advanced`만 query-understanding과 해석된 문서 조건을 적용합니다. 이전 소비자 호환을 위해 생략 시 `advanced`입니다.
- `query`: 필수 문자열
- `top_k`: 기본 5, 범위 1–50
- `threshold`: 선택 숫자
- `node_ids`: 선택 UUID 배열. 파일과 폴더를 모두 받을 수 있으며 서버에서 소유권을 확인하고 파일 UUID로 확장합니다.

```json
{
  "results": [
    {
      "node_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      "node_name": "contract.pdf",
      "file_ext": ".pdf",
      "doc_score": 0.91,
      "evidences": [
        {
          "chunk_id": 10,
          "text": "...",
          "context_text": "...",
          "section": "해지",
          "pages": "3",
          "distance": -0.91
        }
      ]
    }
  ],
  "performance_metrics": {},
  "query_plan": {
    "mode": "basic",
    "source": "direct",
    "retrieval_query": "계약 해지 조건",
    "intent": "",
    "confidence": null,
    "warnings": [],
    "filters": [],
    "sorts": []
  }
}
```

`advanced` 응답의 `query_plan`은 query-understanding의 `source`, 해석 질의, intent, confidence, warning, filter, sort를 반환합니다. `compressed_text`, `compression`, 개별 score와 `score_details`는 설정 및 검색 단계에 따라 선택적으로 존재합니다.

### RAG 요청과 NDJSON event

```json
{
  "question": "이 계약의 해지 조건을 알려줘",
  "top_k": 3,
  "threshold": 0.4,
  "language": "ko",
  "node_ids": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
}
```

- `question`: 필수 문자열
- `top_k`: 기본 서버 설정값(`RAG_SEARCH_TOP_K`, [운영 가이드](./operation-guide.md#rag-파라미터-조정) 참고), 범위 1–10
- `threshold`: 선택, 0 이상
- `language`: `ko` 또는 `en`, 기본 `ko`
- `node_ids`: 선택 UUID 배열. 검색과 같은 소유권·폴더 확장 규칙을 사용합니다.

성공 응답의 content type은 `application/x-ndjson`이며 각 줄은 독립 JSON object입니다.

```text
{"type":"started","job_id":7,"llm_target":"server","llm_model":"model-name"}
{"type":"sources","citations":[]}
{"type":"token","text":"답변"}
{"type":"completed","job_id":7,"answer":"답변","citations":[],"performance_metrics":{}}
```

event 규칙:

- 시작 순서는 `started` 다음 `sources`입니다.
- `token`은 0개 이상이며 완료 전에 반복될 수 있습니다.
- terminal event는 `completed`, `canceled`, `error` 중 하나입니다.
- 동시 처리 용량이 없으면 stream을 열기 전에 JSON `503`, 오류 code `RAG_CAPACITY_EXCEEDED`, `Retry-After` header를 반환합니다.
- 런타임을 사용할 수 없으면 JSON `503`, 오류 code `LLM_RUNTIME_UNAVAILABLE`을 반환합니다.

### 서버 정책·런타임 읽기 요약

`GET /api/document-ai/v1/server-policy/`는 현재 session 사용자가 화면에 표시할 서버 전역 정책만 반환합니다.

```json
{
  "ok": true,
  "operation_mode": "rag",
  "policy": {
    "search_strategy": "hybrid",
    "search_top_k": 3,
    "retrieval_threshold": 0.25
  },
  "rag": {
    "configured": true,
    "available": true,
    "status": "healthy",
    "model": "model-name",
    "runtime": "llama.cpp",
    "priority_preset": "balanced",
    "selection_mode": "automatic",
    "serving_concurrency": 1
  },
  "embedding": {
    "enabled": true,
    "configured": true,
    "status": "configured",
    "model": "BAAI/bge-m3",
    "provider": "bgem3_hybrid",
    "dimension": 1024,
    "sparse_enabled": true,
    "distance_strategy": "inner_product"
  }
}
```

- 이 API는 서버 상태를 변경하지 않으며 사용자별 모델 선택을 제공하지 않습니다.
- 내부 base URL, runtime 설정 경로, fingerprint, token, 전체 진단 payload는 응답에 포함하지 않습니다.
- RAG 준비 여부는 설정 화면 요청 시 짧은 network timeout을 둔 얕은 `/health` probe로 확인합니다. 운영체제의 DNS 이름 확인 시간은 이 socket timeout보다 길어질 수 있으므로 SPA는 이 요청을 비동기로 처리합니다. 저장된 status 파일은 사유와 갱신 시각의 보조 정보이며 단독으로 `available=true`를 만들지 않습니다.

## 기존 template UI가 의존하는 필드

| 화면 | 현재 사용 API | 직접 사용하는 주요 필드 |
|---|---|---|
| 문서 목록 | files, upload, readiness, search, bulk, move, star, delete | `file.uid/name/ext/node_type/path/size/starred/ai_status`, 준비 상태 집계, 검색 `results` |
| 문서 상세 | download, parsed_text, meta, star, delete, AI enabled | 파일 body, `text`, 변경 성공 여부 |
| 최근·즐겨찾기·휴지통 | recent, starred, trash, restore, permanent delete | `files`, `uid`, `name`, 휴지통 만료 필드 |
| RAG workspace | scope-nodes, readiness, rag stream | scope node 전 필드, 준비 상태, 모든 NDJSON event |
| 검색 sandbox | tuning | result와 evidence score 세부 필드 |

template은 변경 요청에 `X-CSRFToken`을 보내며 RAG stream을 `ReadableStream`과 줄 단위 buffer로 처리합니다. SPA에서도 이 동작을 API adapter로 옮기되 React component가 직접 구현하지 않도록 합니다.

## 알려진 진행 중 항목

- `parent_uid`: SPA는 UUID만 사용하는 방향으로 전환 중이며, 정수 `parent_id`는 기존 소비자가 전환된 뒤 제거될 예정인 과도기 필드입니다.
- 문서 공유, 팀 단위 workspace 관리 API는 아직 없습니다. 관련 UI는 `예정` 상태로만 표시되며, API가 추가되기 전까지 동작하는 기능이나 성공 상태로 나타나지 않습니다.

이 계약이 바뀌는 각 단계의 상세 이력(무엇을 언제 왜 바꿨는지)은 이 문서가 아니라 프로젝트 내부 개발 기록에서 관리합니다. 이 문서는 항상 **현재 시점의 계약**만 반영합니다.

## 변경 관리 규칙

- endpoint를 제거하거나 method, 필수 request 필드, 안정 응답 필드를 변경하면 계약 테스트를 먼저 의도적으로 갱신합니다.
- 응답 필드 추가는 기존 소비자가 무시할 수 있는 호환 변경으로 봅니다.
- 소유권 검사는 view 입력을 신뢰하지 않고 owning service/query에서 계속 적용합니다.
- 검색과 RAG의 무거운 작업을 Django view로 이동하지 않습니다.
- sync/tuning 전용 계약을 편의상 일반 SPA 설정으로 노출하지 않습니다.
