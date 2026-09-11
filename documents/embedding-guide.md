# 임베딩 런타임 가이드

이 가이드를 따라 하면 Dotori의 활성 임베딩 모델을 바꾸고, 기존 문서를 새 모델로 재임베딩하고, 전환이 제대로 끝났는지 확인할 수 있습니다.

업로드된 문서는 벡터로 변환되어 저장되며, 검색과 RAG는 그 벡터를 사용합니다. 어떤 모델로 변환할지는 서버 전체에 **한 번에 하나만** 활성화되고, 이를 임베딩 런타임이라고 부릅니다.

카탈로그 목록에 없는 모델을 새로 추가하려면 [임베딩 설치 가이드](./embedding-installation-guide.md)를 먼저 참고하세요.

---

## Prerequisites

- 운영 모드가 `[1] Full 로컬 AI RAG` 또는 `[2] Hybrid/Search AI`여야 합니다. `[3] 기본 모드`는 임베딩을 쓰지 않으므로 전환이 거부됩니다.
- 서버 컨테이너가 기동된 상태여야 합니다. 전환 과정에서 데이터베이스를 조회합니다.
- 모든 명령은 저장소 최상위 디렉터리에서, 호스트 셸로 실행합니다.
- 전환이 시작되면 문서 처리 파이프라인이 잠시 중지되었다가 다시 기동됩니다. 전환 도중에는 업로드를 피하세요.
- 임베딩 전환에는 되돌리는 명령이 없습니다. 청크 벡터는 활성 계약 하나만 보관하므로, 이전 모델로 돌아가려면 그 모델로 다시 전환해 전량 재임베딩하거나 전환 이전 시점의 데이터베이스 백업을 복원해야 합니다.

전환에 실패하면 이전 런타임이 그대로 유지됩니다. 새 세대가 전량 임베딩을 마쳐야만 활성 포인터가 교체되므로, 실패가 검색 중단으로 이어지지 않습니다.

---

## 전환하기

```bash
python install.py --change-embedding
```

현재 활성 런타임이 먼저 표시되고, 소스를 고릅니다.

```text
============================================================
 Change Embedding Runtime (production)
============================================================
Current Active Embedding Runtime:
  • Model: BAAI/bge-m3 (dim: 1024, provider: bgem3_hybrid)
  • Generation: production-embedding-bge-m3-hybrid-5617a9f-1789037713

Select embedding source:
[1] System Built-in Catalog Models (Local & Recommended)
[2] External OpenAI-compatible Endpoint (Ollama, vLLM, OpenAI, etc.)
[3] Cancel
```

두 선택지 중 하나를 고르면 아래 [Configure](#configure)의 해당 절차로 이어집니다. 소스를 고른 뒤에는 기존 문서를 재임베딩할지 묻는 화면으로 이어지며, 이는 [Verify](#verify)가 아니라 이 절차의 마지막 단계입니다 — [재임베딩 여부 선택](#재임베딩-여부-선택)에서 다룹니다.

---

## Configure

### 카탈로그 모델 선택

`[1]`을 고르면 카탈로그에서 `availability`가 `supported`인 프로필이 나열됩니다.

| 프로필 ID | 모델 | 차원 | 프로바이더 | 특징 |
|---|---|---|---|---|
| `bge-m3-hybrid` | BAAI/bge-m3 | 1024 | `bgem3_hybrid` | 밀집 + 희소 하이브리드. 목록에 `[Recommended]`로 표시됩니다. |
| `harrier-270m` | microsoft/harrier-oss-v1-270m | 640 | `sentence_transformers` | 초경량. 입력 32,768토큰까지 처리합니다. |
| `granite-278m` | ibm-granite/granite-embedding-278m-multilingual | 768 | `sentence_transformers` | 다국어 116종. 입력 512토큰. |
| `gte-qwen2-1.5b` | Alibaba-NLP/gte-Qwen2-1.5B-instruct | 1536 | `sentence_transformers` | 카탈로그에서 가장 큰 모델(1.5B). |
| `openai-text-embedding-3-small` | openai/text-embedding-3-small | 1536 | `openai_compatible` | 외부 API를 호출합니다. 로컬 메모리를 쓰지 않습니다. |

목록 마지막 항목은 `Select by Preset`입니다. 모델 이름 대신 `speed`, `balanced`(기본값), `quality` 중 하나를 고르면 해당 프리셋을 가진 프로필이 선택됩니다.

> 현재 세 프리셋은 모두 `bge-m3-hybrid`에 배정되어 있습니다. 따라서 프리셋으로는 어느 쪽을 골라도 같은 모델이 선택됩니다. 프리셋 배정을 바꾸려면 [임베딩 설치 가이드](./embedding-installation-guide.md)를 참고하세요.

전환 시 아래 두 가지가 달라질 수 있습니다.

- **희소 벡터**: 하이브리드 검색은 `supports_sparse`가 `true`인 프로필에서만 동작합니다. 현재는 `bge-m3-hybrid` 하나이며, 다른 모델로 바꾸면 밀집 벡터 검색만 남습니다.
- **청크 크기**: 모델마다 입력 토큰 한계가 다릅니다. `granite-278m`(512토큰)처럼 짧은 모델로 바꾼다면 `.env`의 `EMBEDDING_MAX_TOKENS`도 함께 낮춰야 합니다. 값 조정은 [운영 가이드](./operation-guide.md)를 참고하세요.

`openai-text-embedding-3-small`은 카탈로그 프로필이지만 실행은 외부에서 이루어집니다. 엔드포인트와 키는 `.env`의 `OPENAI_EMBEDDING_BASE_URL`, `OPENAI_EMBEDDING_API_KEY`에서 읽으며, 값이 없으면 `https://api.openai.com`으로 요청합니다.

### 외부 엔드포인트 연결

`[2]`를 고르면 Ollama, vLLM, LocalAI, OpenAI API 등 OpenAI 호환 임베딩 서비스를 연결할 수 있습니다.

입력 항목은 세 가지입니다.

**Endpoint URL** — 호스트를 가리키는 주소입니다.

| 대상 | 예시 |
|---|---|
| 호스트에서 실행 중인 Ollama | `http://host.docker.internal:11434` |
| 사내 vLLM 서버 | `http://192.168.1.100:8000` |
| OpenAI API | `https://api.openai.com` |

경로는 생략합니다. `/v1`이나 `/v1/embeddings`로 끝나지 않으면 호출 시 자동으로 붙습니다.

> `localhost`나 `127.0.0.1`을 입력하면 컨테이너용 주소인 `host.docker.internal`로 바꿔 `.env`에 기록합니다. 컨테이너 안에서 `localhost`는 컨테이너 자신을 가리키기 때문입니다. 반대로 헬스체크는 호스트에서 이루어지므로 `127.0.0.1`로 먼저 시도한 뒤, 실패하면 입력한 주소로 한 번 더 시도합니다.

**API Key** — 로컬 Ollama나 vLLM이라면 엔터로 건너뜁니다. 입력한 키는 `.env`에 저장되고 요청의 `Authorization` 헤더로 전달됩니다.

**Model Name** — 엔드포인트가 인식하는 모델 이름입니다. `text-embedding-3-small`, `bge-m3`, `nomic-embed-text` 등.

입력을 마치면 `POST /v1/embeddings`로 테스트 요청을 보내 응답 상태, 벡터 차원, pgvector 스토어 호환성을 확인합니다. 하나라도 실패하면 전환이 중단되고 `.env`도 바뀌지 않습니다.

```text
• Probing endpoint http://127.0.0.1:11434 with model 'nomic-embed-text'...
✓ Healthcheck passed (HTTP 200, 143ms)
✓ Detected embedding dimension: 768
✓ Store compatibility confirmed: pgvector_chunk_768
```

Dotori는 미리 만들어둔 컬럼에 벡터를 저장하므로, 아래 다섯 차원만 연결할 수 있습니다.

| 차원 | 스토어 |
|---|---|
| 384 | `pgvector_chunk_384` |
| 640 | `pgvector_chunk_640` |
| 768 | `pgvector_chunk_768` |
| 1024 | `pgvector_chunk_1024` |
| 1536 | `pgvector_chunk_1536` |

통과하면 `.env`에 `OPENAI_EMBEDDING_BASE_URL`(및 키를 입력했다면 `OPENAI_EMBEDDING_API_KEY`)이 기록되고, 새 세대의 런타임 계약 파일이 만들어집니다.

> 외부 엔드포인트는 희소 벡터를 지원하지 않습니다(`supports_sparse: false`). 하이브리드 검색을 쓰고 있었다면 밀집 벡터 검색으로 바뀝니다.

### 재임베딩 여부 선택

모델이 바뀌면 기존 벡터는 새 모델과 호환되지 않습니다. 검색은 활성 세대와 지문이 **모두** 일치하는 문서만 결과에 포함하므로, 재임베딩하지 않은 문서는 검색 결과에서 사라집니다.

등록된 문서가 없다면 새 런타임이 곧바로 활성화됩니다. 등록된 문서가 있다면 문서 수와 청크 수를 보여주고 재임베딩 여부를 묻습니다.

```text
• Database check: Found 12 document(s) (184 chunks).
• Model changing from 'BAAI/bge-m3' (1024 dim) to 'text-embedding-3-small' (1536 dim).
Existing documents must be re-embedded to remain searchable with the new model.

Do you want to re-embed all existing documents now? (Y/n):
```

| 선택 | 결과 |
|---|---|
| `Y` (기본값) | 모든 문서를 새 모델로 재임베딩합니다. 전량 완료되면 HNSW 인덱스가 교체되고 새 세대가 활성화됩니다. |
| `n` → `[1]` | 전환을 취소하고 기존 런타임을 유지합니다. |
| `n` → `[2]` | 새 런타임을 활성화하되 기존 문서는 그대로 둡니다. 그 문서들은 재임베딩 전까지 검색되지 않습니다. |

이미 활성화된 모델을 다시 고르면 전환할 것이 없으므로, 같은 모델로 전량 재임베딩할지 되묻습니다(기본값은 아니오).

---

## Verify

### 런타임이 바뀌었는지 확인

```bash
docker compose exec app python manage.py inspect_embedding_runtime --scope production
```

```
catalog_id: bge-m3-hybrid
generation_id: production-embedding-bge-m3-hybrid-5617a9f-1789037713
model_id: BAAI/bge-m3
provider: bgem3_hybrid
store: pgvector_chunk_1024
dimension: 1024
supports_sparse: True
runtime_fingerprint: 55affc3f2772e50ce756d171c8307473641a3a459ce920766de30cad3310f0c9
database_generation: {'status': 'ACTIVE', 'expected_chunks': 69, 'completed_chunks': 69, 'failed_chunks': 0, ...}
```

`generation_id`와 `model_id`가 원하는 값으로 바뀌었는지 확인합니다. 기계 판독용 출력이 필요하면 `--json`을 붙입니다.

### 재임베딩이 끝났는지 확인

같은 명령의 `database_generation` 필드를 봅니다. `status`가 `ACTIVE`이고 `completed_chunks`가 `expected_chunks`와 같으면 전량 완료된 것입니다. `failed_chunks`가 0보다 크면 일부 청크가 실패한 상태이므로 [트러블슈팅](#문제가-있다면)을 확인하세요.

### 프로바이더가 정상인지 확인

모델이 실제로 응답하는지, pgvector 스키마와 차원이 맞는지까지 확인하려면 다음을 실행합니다.

```bash
docker compose exec app python manage.py validate_embedding_provider
```

모델을 적재하지 않고 설정만 검사하려면 `--config-only`를 붙입니다. 임베딩 외에 서버 전반의 상태를 함께 보려면 `python install.py --status`를 사용합니다.

---

## 다음 단계

### 비대화형 명령으로 자동화하기

위 과정을 CI/CD 파이프라인이나 스크립트에서 마법사 없이 실행하려면 플래그로 소스를 지정합니다. `--catalog-id`, `--embedding-priority`, `--external-url`과 `--external-model` 중 하나라도 주어지면 대화형 화면은 나타나지 않습니다.

| 플래그 | 설명 |
|---|---|
| `--catalog-id <프로필 id>` | 카탈로그 프로필을 직접 지정합니다. |
| `--embedding-priority speed\|balanced\|quality` | 프리셋으로 프로필을 고릅니다. |
| `--external-url <URL>` | 외부 엔드포인트 주소. `--external-model`과 함께 씁니다. |
| `--external-model <이름>` | 외부 엔드포인트의 모델 이름. |
| `--external-key <키>` | 외부 엔드포인트 API 키(선택). |
| `--force-reembed` | 확인 없이 전량 재임베딩합니다. 같은 모델이어도 다시 임베딩합니다. |
| `--skip-reembed` | 재임베딩 없이 런타임만 교체합니다. 기존 문서는 검색되지 않습니다. |
| `--yes` | 확인 프롬프트를 생략합니다. |
| `--scope <스코프>` | 대상 스코프. 기본값은 `production`입니다. |

```bash
# 카탈로그 모델로 전환
python install.py --change-embedding --catalog-id harrier-270m --force-reembed --yes

# 프리셋으로 전환
python install.py --change-embedding --embedding-priority speed --yes

# 호스트의 Ollama 연결
python install.py --change-embedding \
  --external-url "http://host.docker.internal:11434" \
  --external-model "nomic-embed-text" \
  --force-reembed \
  --yes

# OpenAI API 연결, 재임베딩은 나중에
python install.py --change-embedding \
  --external-url "https://api.openai.com" \
  --external-model "text-embedding-3-small" \
  --external-key "sk-..." \
  --skip-reembed \
  --yes
```

성공하면 종료 코드 `0`, 실패나 취소면 `1`을 반환합니다.

### 런타임 계약 파일 살펴보기

활성화된 런타임은 호스트의 `data/config/runtime_scopes/<스코프>/` 아래에 파일로 남습니다. 세대별 계약은 `embedding_generations/<세대 id>/runtime.json`에 기록되고, `embedding_runtime.json`이 현재 세대를 가리킵니다.

```json
{
  "catalog_id": "openai-sentence-transformers-all-minilm-l6-v2",
  "catalog_revision": "external",
  "dimension": 384,
  "distance_strategy": "inner_product",
  "document_prefix": "",
  "generation_id": "production-embedding-external-sentence-transformers-all-minilm-l6-v2-1789019863",
  "languages": ["multilingual"],
  "model_id": "sentence-transformers/all-MiniLM-L6-v2",
  "model_revision": "external",
  "normalize_embeddings": true,
  "provider": "openai_compatible",
  "query_prefix": "",
  "resolved_at": "2026-09-10T05:57:43.234629+00:00",
  "runtime_fingerprint": "4d742949255394997ca461126661577007ad878d433c6577844d2434eecda6c8",
  "schema_version": 1,
  "scope": "production",
  "store": "pgvector_chunk_384",
  "supports_sparse": false,
  "tokenizer_id": "sentence-transformers/all-MiniLM-L6-v2",
  "tokenizer_revision": "external"
}
```

외부 엔드포인트로 만든 계약은 `catalog_revision`과 `model_revision`이 `external`입니다. Hugging Face 리비전으로 고정할 수 없기 때문이며, 엔드포인트 쪽에서 모델이 교체되어도 Dotori는 알 수 없습니다. 한 번 쓰인 계약 파일은 바뀌지 않습니다. 각 필드가 어떻게 결정되는지는 [임베딩 설치 가이드](./embedding-installation-guide.md)에 정리되어 있습니다.

### 문제가 있다면

| 증상 | 확인 사항 |
|---|---|
| `Embedding runtime is disabled in Basic mode.` | 운영 모드가 `[3] 기본 모드`입니다. 임베딩을 쓰려면 `[1]` 또는 `[2]`로 설치해야 합니다. |
| `Probe failed: ...` | 엔드포인트가 응답하지 않습니다. 호스트에서 `curl <URL>/v1/embeddings`로 직접 확인하고, 컨테이너에서 호스트로 접근할 때는 `host.docker.internal`을 사용합니다. |
| `Dimension ... is not supported` | 모델의 출력 차원이 [지원 차원](#외부-엔드포인트-연결) 다섯 개에 없습니다. 다른 모델을 쓰거나 차원 축소를 지원하는 엔드포인트를 사용합니다. |
| 전환 후 기존 문서가 검색되지 않음 | 재임베딩을 건너뛴 상태입니다. `--force-reembed`로 다시 실행합니다. |
| 세대 상태가 `FAILED` | 재임베딩이 전량 완료되지 않았습니다. 이전 런타임은 유지되어 있으므로, 원인을 확인한 뒤 다시 전환합니다. 로그 조회는 [모니터링 가이드](./monitoring-and-quality-guide.md)를 참고하세요. |
| 하이브리드 검색이 동작하지 않음 | 활성 프로필의 `supports_sparse`를 확인합니다. `bge-m3-hybrid`가 아니면 밀집 벡터 검색만 동작합니다. |
| 원하는 모델이 카탈로그 목록에 없음 | [임베딩 설치 가이드](./embedding-installation-guide.md)의 절차로 등록한 뒤 `python install.py --rebuild`를 실행합니다. |

---

## 더 보기

- [임베딩 설치 가이드](./embedding-installation-guide.md) — 카탈로그에 새 모델 등록하기
- [설치 가이드](./installation-guide.md) — 서버 설치와 운영 모드
- [LLM 설치 가이드](./llm-installation-guide.md) — 로컬 LLM 선택과 설치
- [운영 가이드](./operation-guide.md) — 임베딩 동시성, 타임아웃, 청크 토큰 한계 조정
- [모니터링 가이드](./monitoring-and-quality-guide.md) — 재임베딩 진행 상황과 검색 품질 확인
