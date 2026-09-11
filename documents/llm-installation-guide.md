# LLM 설치 가이드

이 가이드를 따라 하면 Dotori의 RAG(검색 증강 생성) 기능이 사용할 로컬 LLM을 설정하고, 나중에 모델을 바꾸거나 상태를 확인할 수 있습니다.

Dotori는 하드웨어를 자동으로 진단해 맞는 모델과 실행 파라미터를 고릅니다. 운영자가 직접 고르는 것은 우선순위(speed / balanced / quality) 하나뿐이며, 컨텍스트 길이·동시 처리 수·배치 크기·양자화 방식은 모두 자동으로 결정됩니다.

지원 런타임은 두 가지이며, 동시에 띄우지 않습니다. 선택한 엔진에 맞춰 단일 컨테이너(`dotori-llm`)의 이미지와 실행 인자가 교체됩니다.

| 런타임 | 사용 조건 | 설명 |
|--------|-----------|------|
| `llama.cpp` | CPU 전용 또는 GPU 부분 오프로드 | GGUF 포맷 모델. RAM이 충분하면 CPU에서 실행. GPU가 있으면 레이어를 VRAM에 올림. |
| `vLLM` | NVIDIA GPU + CUDA | AWQ/GPTQ/safetensors 포맷. GPU 전용. RAM으로 스필 없음. |

---

## Prerequisites

- 운영 모드가 `[1] Full 로컬 AI RAG`여야 LLM 답변 생성이 활성화됩니다. [설치 가이드](./installation-guide.md)에서 먼저 서버를 설치하세요.
- vLLM을 쓰려면 NVIDIA 드라이버와 NVIDIA Container Toolkit이 필요합니다. 조건이 없으면 llama.cpp만 선택 가능합니다.
- 모델 다운로드에는 인터넷 연결이 필요하며, 접근 제한(gated) 모델은 Hugging Face 토큰이 필요합니다.

---

## Install

### 방법 A: install.py 마법사 (권장)

`start.bat`을 실행하거나:

```bash
python install.py
```

화면 안내에 따라 운영 모드·임베딩 모델·RAG 우선순위를 선택하면, Docker 서비스 구동 후 마법사가 컨테이너 안에서 자동으로 LLM 런타임을 감지하고 설정합니다. 선택지의 의미는 [설치 가이드](./installation-guide.md)를 참고하세요.

### 방법 B: Docker 컨테이너 직접 실행 후 수동 설정

```bash
# 1. Docker 서비스 구동
docker compose up -d --build

# 2. LLM 런타임 설정 (interactive 마법사)
docker compose exec app python manage.py detect_llm_runtime --interactive
```

---

## Configure

### 어떤 모델을 쓸 수 있는지 확인하기

설정 전에 내 서버에서 어떤 모델을 쓸 수 있는지 미리 확인할 수 있습니다.

```bash
docker compose exec app python manage.py llm_model_catalog list
```

```
#   Model                    Quant     Size   Device  Logical  Pool Req RAM Req Backend    Speed    Safety  Fit
--- ------------------------ --------- ------ ------- -------- -------- ------- ---------- -------- ------- -------
1   Qwen2.5 7B Instruct      Q4_K_M    7B     CPU     4893MB   5117MB   5117MB  llamacpp-c standard safe    FIT
2   Qwen2.5 7B Instruct AWQ  AWQ       7B     GPU     5632MB   7000MB   0MB     vllm-cuda  fast     safe    NOFIT
```

| 컬럼 | 설명 |
|------|------|
| Logical | 모델 가중치 + KV 캐시 + 오버헤드의 논리적 합계 (MB) |
| Pool Req | 실제로 필요한 메모리 풀 (GPU 사용 시 VRAM, CPU 사용 시 RAM) |
| RAM Req | RAM에서 필요한 양 (GPU 모델은 0일 수 있음) |
| Fit | `FIT` (충분), `RISKY` (여유 없음), `NOFIT` (불가) |

특정 모델 상세 보기, 검색, JSON 출력도 지원합니다.

```bash
docker compose exec app python manage.py llm_model_catalog show qwen2.5-7b-instruct-q4_k_m
docker compose exec app python manage.py llm_model_catalog search awq
docker compose exec app python manage.py llm_model_catalog list --json-output
```

### 자동 선택 (권장)

서버 하드웨어에 맞는 최적 모델과 파라미터를 자동으로 선택하고 저장합니다.

```bash
# balanced 우선순위로 자동 선택 후 저장
docker compose exec app python manage.py detect_llm_runtime --write

# 우선순위 지정
docker compose exec app python manage.py detect_llm_runtime --write --priority speed

# 저장하지 않고 미리보기만 (dry-run)
docker compose exec app python manage.py detect_llm_runtime
```

> `--write` 없이 실행하면 어떤 모델이 선택될지 미리 볼 수 있습니다. 실제 저장은 되지 않습니다.

우선순위는 모델 선택과 실행 파라미터에 함께 반영됩니다.

| 우선순위 | 선택 기준 | 주요 효과 |
|---------|-----------|----------|
| `speed` | 개별 응답 지연 우선 | 컨텍스트 감소, 엄격한 latency 기준 |
| `balanced` | 속도/품질/메모리 균형 (기본값) | 중간 컨텍스트, 처리량과 latency 균형 |
| `quality` | 답변 품질 최우선 | 컨텍스트 최대화, 더 큰 모델과 메모리 여유 우선 |

> 처음 설치라면 `balanced`를 권장합니다. 하드웨어에 여유가 있는 경우에만 `quality`를 선택하세요.

설치 시 우선순위에 따라 컨텍스트 길이와 메모리 안전 동시성 상한을 계산하지만, 실제 운용 동시성은 성능 calibration 전까지 `1`로 유지합니다.

### Interactive 마법사

```bash
docker compose exec app python manage.py detect_llm_runtime --interactive
```

마법사는 4단계로 진행됩니다.

**Step 1 — 하드웨어 진단 출력**
```
CPU Model: Intel Core i7-12700 (20 Cores)
RAM: Total 32768 MB, Available 24576 MB
GPU: NVIDIA RTX 3080 (10240 MB VRAM, 8192 MB Free)
```

**Step 2 — 운영 정책 선택**
```
1) Speed   2) Balanced   3) Quality
Enter choice (1-3, default: 2):
```

**Step 3 — 모델 순위 및 선택**
```
1) Automatic recommendation (default)
2) Choose from the assessed model catalog
```

자동을 선택하면 최상위 FIT 모델이 자동 선택됩니다. 수동을 선택하면 카탈로그 목록이 표시되고 번호로 직접 고를 수 있습니다.

> **RISKY** 모델을 수동 선택하면 확인 프롬프트가 나타납니다. `y`를 입력해야만 선택이 확정됩니다. 메모리 여유가 없을 수 있다는 의미입니다. 서버가 RAG 전용이고 다른 부하가 없다면 선택할 수 있지만, 일반 운영 서버라면 `FIT` 모델을 선택하는 것이 안전합니다.

**Step 4 — 컨테이너 재시작 및 헬스체크**

설정이 저장된 후 런타임 컨테이너를 자동으로 시작하고 30초간 헬스체크를 수행합니다.

### 엔드포인트 검증 옵션

```bash
# /health 엔드포인트 확인 후 선택
docker compose exec app python manage.py detect_llm_runtime --write --check-endpoint

# 실제 채팅 완성 요청으로 스모크 테스트까지 수행
docker compose exec app python manage.py detect_llm_runtime --write --smoke-test
```

### 고급: 클러스터 모드

여러 GPU에 모델을 분산하여 실행하는 환경에서 사용합니다.

```bash
docker compose exec app python manage.py detect_llm_runtime --interactive --cluster-mode
# 또는
python install.py --change-llm --cluster-mode
```

클러스터 모드에서는 vLLM의 tensor parallel이 활성화되며, 각 GPU에 모델 레이어를 균등 분산합니다. GGUF 포맷은 클러스터 모드에서 선택되지 않습니다.

---

## Verify

### 저장된 설정 읽기 (하드웨어 탐지 없이)

```bash
docker compose exec app python manage.py inspect_llm_runtime
```

```
Persisted LLM runtime config
path: /app/data/config/runtime_scopes/production/llm_runtime.json
exists: True
generated_at: 2026-07-04T12:00:00Z

Configured RAG target
endpoint_name: Qwen2.5 7B Instruct (CPU full)
base_url: http://rag-runtime:8080
model: qwen2.5-7b-instruct-q4_k_m
runtime: llama.cpp
priority_preset: balanced
serving_profile: {'context_length': 8192, 'safe_concurrency_ceiling': 4, 'serving_concurrency': 1, 'calibration_status': 'pending', ...}
```

`base_url`은 선택된 엔진과 무관하게 항상 같은 네트워크 별칭(`rag-runtime`)을 가리킵니다. `safe_concurrency_ceiling`은 선택한 우선순위와 메모리 계산으로 확인한 calibration 상한이고, `serving_concurrency`는 실제 runtime과 RAG admission에 적용되는 값이며 성능 calibration 전에는 안전한 기본값 `1`을 사용합니다.

### 현재 서버 상태와 함께 확인 (라이브 탐지)

```bash
docker compose exec app python manage.py inspect_llm_runtime --live
```

> 일상적인 상태 확인에는 `inspect_llm_runtime`(하드웨어 탐지 없음)을 사용하세요. `--live`는 하드웨어 재탐지가 필요한 경우에만 사용합니다.

---

## 다음 단계

### 모델 변경

```bash
# install.py에서 변경 마법사 호출
python install.py --change-llm

# 또는 Docker 컨테이너에서 직접
docker compose exec app python manage.py detect_llm_runtime --interactive
```

변경 후 자동으로 일어나는 일입니다.

1. 후보 설정을 `data/config/runtime_scopes/<scope>/generations/<generation-id>/`에 먼저 기록합니다(아직 활성 설정에는 반영되지 않음).
2. 단일 런타임 컨테이너(`dotori-llm`)를 새 이미지/인자로 재생성하고 헬스체크를 통과할 때까지 대기합니다 — docker-compose 서비스가 아니라 Docker CLI로 직접 관리됩니다.
3. 헬스체크를 통과해야만 `runtime_scopes/<scope>/llm_runtime.json`을 새 설정으로 원자적으로 교체합니다.
4. 헬스체크에 실패하면 이전 컨테이너로 롤백하고 기존 설정을 그대로 유지합니다.
5. 이전에 선택했던 모델과 다른 모델로 바꾼 경우, 이전 모델의 다운로드된 가중치 파일을 캐시에서 삭제합니다.

기존 모델을 다시 쓸 계획이 있어 가중치를 남겨두고 싶다면 `--keep-weights`를 추가합니다.

```bash
python install.py --change-llm --keep-weights
```

현재 설정된 런타임을 완전히 제거(컨테이너 중지+삭제, 캐시된 가중치 삭제, 설정 파일 삭제)하려면:

```bash
python install.py --remove-llm
python install.py --remove-llm --yes  # 확인 프롬프트 없이 즉시 실행
```

### 참고: 서비스 전환 구조

Dotori는 런타임 컨테이너를 **하나만** 운영합니다. llama.cpp와 vLLM을 동시에 띄우지 않고, 선택한 엔진에 맞는 이미지로 같은 컨테이너를 교체합니다.

| scope | 컨테이너 이름 | 네트워크 | 사용 시점 |
|-------|--------------|---------|----------|
| `production` | `dotori-llm` | `dotori-runtime` | `python install.py`(기본 배포, `docker-compose.yml`) |

이 컨테이너는 `docker-compose.yml`의 서비스가 아니라, `RuntimeLifecycleManager`(`app/llm_installation/runtime_lifecycle.py`)가 Docker CLI(`docker run`/`rm`/`update`)로 직접 생성·교체·삭제합니다. **`docker compose` 명령으로는 이 컨테이너를 제어할 수 없으므로, 반드시 `python install.py` 명령을 사용하세요.** 컨테이너에는 `com.dotori.*` 라벨(managed/component/scope/runtime/generation)이 붙어 있어, 이름이 같아도 다른 배포가 소유한 컨테이너는 건드리지 않습니다.

선택한 `runtime` 값에 따라 빌드되는 이미지가 달라집니다.

| `runtime` 값 | 이미지 | Dockerfile |
|-------------|--------|-----------|
| `llama.cpp` | `dotori/llama-rag` | `llm-runtime/llama.Dockerfile` |
| `vllm` | `dotori/vllm-rag` | `llm-runtime/vllm.Dockerfile` |

어떤 엔진을 선택하든 앱에서는 항상 같은 네트워크 별칭으로 접근합니다: `http://rag-runtime:8080`.

설정은 scope별로 분리되며, 신규 후보는 헬스체크를 통과해야만 활성 설정으로 원자적 교체됩니다.

| 파일 | 설명 |
|------|------|
| `data/config/runtime_scopes/<scope>/llm_runtime.json` | 현재 **활성** 런타임 스냅샷. 모델명, URL, 파라미터 전체 포함. |
| `data/config/runtime_scopes/<scope>/runtime_status.json` | 마지막 헬스체크 결과와 실패 사유(`reason_code`). |
| `data/config/runtime_scopes/<scope>/generations/<generation-id>/runtime.json` | 검증 대기 중인 **후보** 설정. |
| `data/config/runtime_scopes/<scope>/generations/<generation-id>/runtime.args` | 후보 서버 실행 인자(엔진에 따라 `--ctx-size`/`--parallel` 또는 `--model`/`--quantization` 등). |

> `detect_llm_runtime --write`(비대화형)는 generation 단계를 건너뛰고 `runtime_scopes/<scope>/llm_runtime.json`과 인자 파일(`llama_rag.args`/`vllm_rag.args`)을 즉시 기록합니다. 실패 시 자동 롤백이 필요하다면 `--interactive` 마법사를 사용하세요.

### 문제가 있다면

| 증상 | 확인 사항 |
|---|---|
| `LLM runtime is not configured` 오류 | RAG 질의 시 이 오류가 발생하면 LLM 런타임이 아직 설정되지 않은 것입니다. `docker compose exec app python manage.py detect_llm_runtime --interactive`를 실행하세요. |
| 카탈로그 모든 모델이 NOFIT | RAM 또는 VRAM이 부족한 경우입니다. `docker compose exec app python manage.py llm_model_catalog show <모델-id>`의 `reason` 필드에서 원인을 확인하세요. |
| 헬스체크 실패 후 서비스가 시작되지 않음 | 런타임 컨테이너는 docker-compose 서비스가 아니므로 `docker compose logs`/`restart`로 제어할 수 없습니다. `docker logs dotori-llm`으로 로그를, `python install.py --status`로 마지막 실패 사유를 확인한 뒤 `python install.py --retry-llm`으로 재시도하세요. 컨테이너를 `docker restart`로 직접 재시작하면 헬스체크·롤백 로직을 거치지 않습니다. |

---

## 더 보기

- [설치 가이드](./installation-guide.md) — 서버 설치와 운영 모드
- [임베딩 설치 가이드](./embedding-installation-guide.md) — 임베딩 모델 등록
- [운영 가이드](./operation-guide.md) — 서비스 시작/중지, 계정 관리
