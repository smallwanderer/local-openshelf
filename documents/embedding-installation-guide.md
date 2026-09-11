# 임베딩 설치 가이드

이 가이드를 따라 하면 카탈로그에 없는 임베딩 모델을 등록해서 `install.py` 마법사의 선택지에 나타나게 만들 수 있습니다. 등록을 마치면 그 모델을 고르는 순간 필요한 값이 자동으로 채워집니다.

이미 카탈로그에 있는 모델로 바꾸거나 외부 엔드포인트를 연결하려면 [임베딩 런타임 가이드](./embedding-guide.md)를 참고하세요. 외부 엔드포인트는 카탈로그 등록 없이 연결할 수 있습니다.

---

## Prerequisites

- 카탈로그는 `app/llm_installation/embedding_catalog/` 아래에 있으며, 한 모델을 **모델 파일**과 **프로필 파일** 두 개로 나누어 기술합니다. 나누는 이유는 같은 모델을 여러 방식으로 서빙할 수 있기 때문입니다(로컬 실행 프로필과 외부 엔드포인트 호출 프로필을 각각 등록할 수 있습니다).
- Dotori는 미리 만들어둔 pgvector 컬럼에 벡터를 저장하므로, 아래 다섯 개 차원만 등록할 수 있습니다.

  | 차원 | 스토어 이름 |
  |---|---|
  | 384 | `pgvector_chunk_384` |
  | 640 | `pgvector_chunk_640` |
  | 768 | `pgvector_chunk_768` |
  | 1024 | `pgvector_chunk_1024` |
  | 1536 | `pgvector_chunk_1536` |

- 카탈로그 파일은 컨테이너 이미지 안에 포함됩니다. 등록 후에는 이미지를 다시 만들어야 반영됩니다.
- 서버 기동 시 로더가 두 디렉터리를 모두 읽어 하나의 카탈로그 항목으로 합칩니다. 파일 하나라도 형식에 맞지 않으면 카탈로그 로딩 전체가 실패하므로, 등록 후에는 반드시 [Verify](#verify)의 확인 절차를 거치세요.

---

## 등록하기

### 모델 파일 작성

`models/<제작사>/<모델이름>.json` 경로에 파일을 만듭니다. 디렉터리 깊이는 자유이며 로더가 하위 전체를 훑습니다.

```json
{
  "id": "bge-m3",
  "display_name": "BGE-M3",
  "description": "Multilingual dense and sparse embedding model used by Dotori hybrid retrieval.",
  "license": "mit",
  "repo_id": "BAAI/bge-m3",
  "revision": "5617a9f",
  "tokenizer_id": "BAAI/bge-m3",
  "tokenizer_revision": "5617a9f",
  "dimension": 1024,
  "model_input_max_tokens": 8192,
  "languages": ["multilingual"],
  "footprint": {
    "parameter_count_m": 568,
    "peak_activation_mb": 0
  }
}
```

**필드**

`id` (문자열, 필수)
카탈로그 안에서 모델을 가리키는 이름입니다. 프로필 파일이 이 값으로 모델을 참조합니다. 중복되면 로딩이 실패합니다.

`display_name` (문자열, 필수)
설치 마법사 목록에 표시되는 이름입니다.

`description` (문자열, 선택)
마법사에서 이름 아래 한 줄로 표시됩니다.

`license` (문자열, 선택)
모델 라이선스 식별자입니다.

`repo_id` (문자열, 필수)
Hugging Face 저장소 주소입니다. 이 값으로 모델을 내려받습니다.

`revision` (문자열, 필수, 7자 이상)
내려받을 리비전입니다. 커밋 해시를 권장합니다. 브랜치 이름을 쓰면 저장소가 갱신될 때 같은 설정이 다른 모델을 가리키게 됩니다.

`tokenizer_id`, `tokenizer_revision` (문자열, 필수)
토크나이저 저장소와 리비전입니다. 보통 모델과 같지만, 토크나이저를 공유하는 모델이라면 다르게 지정합니다. 문서를 청크로 나눌 때 이 토크나이저로 토큰 수를 셉니다.

`dimension` (정수, 필수)
출력 벡터의 길이입니다. [지원 차원](#prerequisites) 다섯 개 중 하나여야 합니다.

`model_input_max_tokens` (정수, 필수)
모델이 한 번에 받을 수 있는 최대 토큰 수입니다.

`languages` (문자열 배열, 선택)
지원 언어 목록입니다. 다국어 모델은 `["multilingual"]`로 씁니다.

`footprint` (객체, 선택)
모델을 메모리에 올려둘 때의 비용입니다. 생략하면 메모리 예약에 프로세스 오버헤드만 잡힙니다.

- `parameter_count_m` (숫자, 필수): 파라미터 수(백만 단위). 568M 모델이면 `568`입니다.
- `peak_activation_mb` (정수, 선택, 기본 0): 추론 중 활성화 메모리 최대치. 실제로 측정하기 전까지는 0으로 둡니다.

### 프로필 파일 작성

`profiles/<프로바이더>/<모델이름>.json` 경로에 파일을 만듭니다.

```json
{
  "id": "bge-m3-hybrid",
  "model_id": "bge-m3",
  "provider": "bgem3_hybrid",
  "store": "pgvector_chunk_1024",
  "dimension": 1024,
  "supports_sparse": true,
  "normalize_embeddings": true,
  "distance_strategy": "inner_product",
  "query_prefix": "",
  "document_prefix": "",
  "availability": "supported",
  "priority": 100,
  "presets": ["speed", "balanced", "quality"]
}
```

**필드**

`id` (문자열, 필수)
설치 시 `--catalog-id`로 지정하는 값입니다. 운영자가 실제로 입력하는 이름이므로 모델 이름과 서빙 방식을 함께 담는 것이 좋습니다(`bge-m3-hybrid`).

`model_id` (문자열, 필수)
[모델 파일 작성](#모델-파일-작성)에서 만든 모델 파일의 `id`입니다. 존재하지 않는 값을 쓰면 로딩이 실패합니다.

`provider` (문자열, 필수)
모델을 실행할 백엔드입니다.

| 값 | 동작 |
|---|---|
| `bgem3_hybrid` | BGE-M3 전용. 밀집 벡터와 희소 벡터를 함께 생성합니다. |
| `sentence_transformers` | sentence-transformers로 로컬 실행합니다. |
| `openai_compatible` | 외부 OpenAI 호환 엔드포인트를 호출합니다. |

`store` (문자열, 필수)
벡터를 저장할 pgvector 스토어입니다. `dimension`과 짝이 맞아야 합니다.

`dimension` (정수, 필수)
모델 파일의 `dimension`과 **같은 값**이어야 합니다. 두 파일이 어긋나면 로딩이 실패합니다.

`supports_sparse` (불리언, 필수)
희소 벡터를 함께 생성하는지 여부입니다. `true`인 프로필만 하이브리드 검색을 사용합니다. 현재 `bgem3_hybrid`만 `true`입니다.

`normalize_embeddings` (불리언, 필수)
벡터를 단위 길이로 정규화할지 여부입니다.

`distance_strategy` (`inner_product` | `cosine` | `l2`, 필수)
검색 시 사용할 거리 함수입니다.

`query_prefix`, `document_prefix` (문자열, 선택)
질의와 문서에 각각 붙일 접두사입니다. 비대칭 인코딩을 요구하는 모델(`query: `, `passage: ` 등)에 사용합니다.

`availability` (`supported` | `experimental` | `unavailable`, 필수)
`supported`만 설치 마법사에 나타납니다. 나머지는 카탈로그에 남되 선택지에서 제외됩니다.

`priority` (정수, 선택, 기본 0)
목록 정렬 순서입니다. 큰 값이 먼저 나옵니다.

`presets` (배열, 선택)
이 프로필이 담당할 프리셋입니다. `speed`, `balanced`, `quality` 중에서 고릅니다.

> 하나의 프리셋은 하나의 프로필만 가질 수 있습니다. 이미 다른 프로필이 쓰고 있는 프리셋을 지정하면 로딩이 실패합니다. `availability`가 `supported`가 아닌 프로필에는 프리셋을 지정할 수 없습니다.

> 현재 세 프리셋은 모두 `bge-m3-hybrid`가 가지고 있습니다. 새 모델에 프리셋을 주려면 `bge-m3-hybrid`의 `presets`에서 해당 값을 먼저 빼야 합니다. 프리셋 없이 등록해도 `--catalog-id`로는 언제나 선택할 수 있습니다.

**검증 규칙**

로더는 카탈로그를 읽으면서 다음을 확인하고, 하나라도 어긋나면 예외를 던집니다.

| 규칙 | 실패 시 메시지 |
|---|---|
| 모델 `id` 중복 없음 | `Duplicate embedding model id` |
| 프로필 `id` 중복 없음 | `Duplicate embedding profile id` |
| 프로필의 `model_id`가 실재함 | `references unknown model` |
| 프로필 차원 == 모델 차원 | `dimension does not match model` |
| `provider`가 지원 목록에 있음 | `references unknown provider` |
| `store` 차원 == 프로필 차원 | `is incompatible with store` |
| 프리셋 소유가 겹치지 않음 | `is assigned to both` |

`provider`와 `store` 검사는 `availability`가 `supported`인 프로필에만 적용됩니다.

---

## Verify

### 카탈로그 재빌드

```bash
python install.py --rebuild
```

### 등록 확인

```bash
docker compose exec app python manage.py embedding_model_catalog
```

출력 예시:

```
bge-m3-hybrid: model=BAAI/bge-m3@5617a9f provider=bgem3_hybrid store=pgvector_chunk_1024 dimension=1024 sparse=True availability=supported
harrier-270m: model=microsoft/harrier-oss-v1-270m@main000 provider=sentence_transformers store=pgvector_chunk_640 dimension=640 sparse=False availability=supported
gte-qwen2-1.5b: model=Alibaba-NLP/gte-Qwen2-1.5B-instruct@main000 provider=sentence_transformers store=pgvector_chunk_1536 dimension=1536 sparse=False availability=supported
granite-278m: model=ibm-granite/granite-embedding-278m-multilingual@main000 provider=sentence_transformers store=pgvector_chunk_768 dimension=768 sparse=False availability=supported
openai-text-embedding-3-small: model=openai/text-embedding-3-small@main000 provider=openai_compatible store=pgvector_chunk_1536 dimension=1536 sparse=False availability=supported
```

`experimental`과 `unavailable` 항목까지 보려면 `--all`을, 기계 판독용 출력이 필요하면 `--json`을 붙입니다. 새 항목이 목록에 보이면 등록이 끝난 것입니다.

---

## 다음 단계

### 등록한 모델 활성화하기

```bash
python install.py --change-embedding --catalog-id <프로필 id>
```

이후 절차는 [임베딩 런타임 가이드](./embedding-guide.md)를 참고하세요.

### 참고: 선택 시 자동으로 결정되는 값

운영자가 고르는 것은 프로필 `id` 하나뿐이고, 나머지는 카탈로그에서 파생됩니다.

**런타임 계약** — 두 카탈로그 파일의 값이 합쳐져 런타임 계약이 됩니다.

| 값 | 출처 |
|---|---|
| `model_id`, `model_revision` | 모델 파일의 `repo_id`, `revision` |
| `tokenizer_id`, `tokenizer_revision` | 모델 파일 |
| `dimension`, `languages` | 모델 파일 |
| `provider`, `store` | 프로필 파일 |
| `supports_sparse`, `normalize_embeddings` | 프로필 파일 |
| `distance_strategy` | 프로필 파일 |
| `query_prefix`, `document_prefix` | 프로필 파일 |
| `catalog_id`, `catalog_revision` | 프로필 `id`와 모델 `revision` |
| `generation_id` | `<스코프>-embedding-<프로필 id>-<리비전 12자>-<타임스탬프>` |
| `runtime_fingerprint` | 위 값 전체의 SHA256 |
| `resolved_at` | 활성화 시각 |

`runtime_fingerprint`는 계약 내용이 바뀌었는지 판별하는 값입니다. 재임베딩된 문서마다 이 값이 함께 저장되고, 검색은 `generation_id`와 `runtime_fingerprint`가 **모두** 현재 런타임과 일치하는 문서만 결과에 포함합니다.

**메모리 예약** — 모델을 올려둘 때 필요한 메모리를 계산해 서버의 메모리 예약 장부에 기록합니다. 이 장부는 LLM 런타임의 예약분과 같은 파일에서 관리되므로, 두 워크로드가 서로의 몫을 알 수 있습니다.

가중치는 실행 디바이스에 따라 달라집니다. 같은 모델이라도 CPU에서는 fp32, CUDA에서는 fp16으로 동작하므로 CPU 쪽이 두 배 듭니다.

| 항목 | 계산 |
|---|---|
| `model_weights` | `parameter_count_m` × 2 MB (CUDA) 또는 × 4 MB (CPU) |
| `peak_activation` | `footprint.peak_activation_mb` |
| `runtime_overhead` | 프로세스 상주 비용 |
| `cuda_context` | CUDA 컨텍스트 비용 (CUDA일 때만) |

디바이스는 `provider`가 결정합니다. `openai_compatible`은 남의 하드웨어에서 동작하므로 로컬 메모리를 잡지 않고, 나머지는 GPU 탐지 결과에 따라 CUDA 또는 CPU로 배치됩니다. BGE-M3(568M) 기준으로 CPU에서 약 2.7 GB, CUDA에서 약 3.1 GB입니다.

현재 예약 상태는 `python install.py --status`의 `memory_reservations` 항목에서 확인합니다. 예약 파일이 비어 있거나 실제 런타임과 어긋난다면 다음 명령으로 현재 활성 런타임을 기준으로 다시 기록합니다.

```bash
docker compose exec app python manage.py sync_memory_reservations --scope production
```

**청크 토큰 한계** — 문서를 나눌 때 쓰는 토큰 한계는 카탈로그가 아니라 `.env`에서 옵니다.

```
CHUNK_MAX_TOKENS = EMBEDDING_MAX_TOKENS - EMBEDDING_TOKEN_HEADROOM
```

기본값은 `1280 - 128 = 1152`입니다. 값 조정은 [운영 가이드](./operation-guide.md)를 참고하세요.

### 참고: 생성되는 산출물

모델을 활성화하면 여섯 곳에 기록이 남습니다.

| 산출물 | 위치 | 성격 |
|---|---|---|
| 세대 계약 | `data/config/runtime_scopes/<스코프>/embedding_generations/<세대 id>/runtime.json` | 한 번 쓰고 바뀌지 않습니다 |
| 활성 포인터 | `data/config/runtime_scopes/<스코프>/embedding_runtime.json` | 현재 세대를 가리킵니다 |
| 메모리 예약 | `data/config/runtime_scopes/<스코프>/memory_reservations.json` | LLM 예약분과 함께 관리됩니다 |
| 세대 레코드 | `EmbeddingGeneration` 테이블 | 상태와 진행률을 담습니다 |
| 문서별 계약 | `DocumentParseResult`의 세대 ID와 지문 | 검색 가능 여부를 결정합니다 |
| HNSW 인덱스 | `chunk_embedding_active_hnsw_idx` | 서버당 하나이며 교체됩니다 |

활성 포인터는 임시 파일에 쓴 뒤 교체하므로, 전환 도중 서버가 멈춰도 반쯤 쓰인 파일이 남지 않습니다.

세대 상태는 `PREPARING → EMBEDDING → READY → ACTIVE` 순서로 바뀌며, 이전 세대는 `RETIRED`로 남습니다. 재임베딩이 전량 완료되지 않으면 세대는 `FAILED`로 끝나고 기존 런타임이 그대로 유지됩니다. 현재 상태는 다음 명령으로 확인합니다.

```bash
docker compose exec app python manage.py inspect_embedding_runtime --scope production
```

### 문제가 있다면

| 증상 | 확인 사항 |
|---|---|
| `Invalid embedding catalog file: <경로>` | 해당 파일의 JSON 문법과 필수 필드를 확인합니다. 정의되지 않은 필드가 하나라도 있으면 거부됩니다. |
| `dimension does not match model` | 모델 파일과 프로필 파일의 `dimension`이 다릅니다. |
| `is incompatible with store` | `store` 이름의 숫자와 `dimension`이 다릅니다. 1024차원 모델은 `pgvector_chunk_1024`를 씁니다. |
| `references unknown model` | 프로필의 `model_id`에 해당하는 모델 파일이 없습니다. 파일 이름이 아니라 모델 파일 안의 `id` 값이어야 합니다. |
| `is assigned to both` | 두 프로필이 같은 프리셋을 가지고 있습니다. 한쪽에서 빼세요. |
| 새 모델이 목록에 없음 | `availability`가 `supported`인지 확인하고, `app` 컨테이너를 다시 만들었는지 확인합니다. |
| 외부 엔드포인트에서 `Dimension ... is not supported` | 그 모델의 출력 차원이 지원 차원 다섯 개에 없습니다. 다른 모델을 쓰거나 차원 축소를 지원하는 엔드포인트를 사용하세요. |

임베딩 전환은 되돌리는 명령이 없습니다. 청크 벡터는 활성 계약 하나만 보관하므로, 포인터만 이전 세대로 바꿔도 벡터가 맞지 않습니다. 이전 모델로 돌아가려면 그 모델로 다시 전환해 전량 재임베딩하거나, 전환 이전 시점의 데이터베이스 백업을 복원합니다.

---

## 더 보기

- [임베딩 런타임 가이드](./embedding-guide.md) — 등록된 모델로 전환하기, 외부 엔드포인트 연결
- [LLM 설치 가이드](./llm-installation-guide.md) — 로컬 LLM 선택과 설치
- [운영 가이드](./operation-guide.md) — 임베딩 동시성과 타임아웃 조정
- [설치 가이드](./installation-guide.md) — 서버 설치와 운영 모드
