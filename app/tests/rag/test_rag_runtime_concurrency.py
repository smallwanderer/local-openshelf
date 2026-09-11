"""
[RAG 런타임 동시성(Serving Concurrency) 및 상태 검증 테스트]

1. 로컬 GPU 런타임의 안전 상한(safe_concurrency_ceiling)과 실측 운용 동시성(serving_concurrency) 정규화.
2. 얕은 헬스체크(/health) 프로브 동작.
3. RAG Admission 슬롯 수가 임의 환경변수가 아닌 llm_runtime.json의 serving_concurrency에서 파생되는지 검증.
"""

import json
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from document_ai.services.rag_runtime_config import (
    get_server_rag_serving_concurrency,
    normalize_serving_profile,
    probe_server_rag_runtime,
    target_from_persisted_config,
)


pytestmark = pytest.mark.unit


class _ProbeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_runtime_probe_uses_shallow_health_endpoint(monkeypatch):
    """
    [핵심 검증 1: 얕은(Shallow) 헬스체크 /health 엔드포인트 호출]
    
    RAG 런타임이 살아있는지 확인할 때 무거운 추론을 하지 않고
    빠른 /health 엔드포인트(타임아웃 3초)로 가볍게 점검하는지 확인합니다.
    """
    observed = {}

    def fake_urlopen(request, timeout):
        observed.update(url=request.full_url, timeout=timeout)
        return _ProbeResponse()

    monkeypatch.setattr(
        "document_ai.services.rag_runtime_config.urlopen",
        fake_urlopen,
    )

    available = probe_server_rag_runtime(
        SimpleNamespace(base_url="http://rag-runtime:8080/v1/"),
        timeout=10,
    )

    assert available is True
    assert observed == {"url": "http://rag-runtime:8080/v1/health", "timeout": 3.0}


def test_runtime_probe_returns_false_when_endpoint_is_unreachable(monkeypatch):
    """
    [핵심 검증 2: 엔드포인트 접속 불가 시 False 안전 반환]
    """
    monkeypatch.setattr(
        "document_ai.services.rag_runtime_config.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )

    assert probe_server_rag_runtime(SimpleNamespace(base_url="http://rag-runtime:8080")) is False


def test_legacy_concurrency_becomes_safe_ceiling_with_conservative_default():
    """
    [핵심 검증 3: 동시성 초기값의 보수적 정규화]
    
    캘리브레이션 실측값이 없는 초기 상태에서는 안전 상한이 4여도
    실제 운용 동시성(serving_concurrency)은 가장 안전한 '1'로 시작해야 합니다.
    """
    profile = normalize_serving_profile(
        {
            "context_length": 8192,
            "concurrency": 4,
            "parallel": 4,
            "max_num_seqs": 4,
            "server_ctx_size": 32768,
            "max_num_batched_tokens": 32768,
        }
    )

    assert profile["safe_concurrency_ceiling"] == 4
    assert profile["serving_concurrency"] == 1
    assert profile["calibration_status"] == "pending"
    assert profile["concurrency"] == 1
    assert profile["parallel"] == 1
    assert profile["max_num_seqs"] == 1
    assert profile["server_ctx_size"] == 8192
    assert profile["max_num_batched_tokens"] == 8192


def test_serving_concurrency_is_clamped_to_safe_ceiling():
    """
    [핵심 검증 4: 실측 동시성이 안전 상한(Ceiling)을 초과하지 못하도록 Clamp]
    
    측정값이 아무리 높게 나와도 메모리 안전 상한(ceiling=2)을 넘어서면 안 됩니다.
    """
    profile = normalize_serving_profile(
        {
            "context_length": 4096,
            "safe_concurrency_ceiling": 2,
            "serving_concurrency": 8,
            "calibration_status": "calibrated",
        }
    )

    assert profile["safe_concurrency_ceiling"] == 2
    assert profile["serving_concurrency"] == 2
    assert profile["calibration_status"] == "calibrated"
    assert profile["server_ctx_size"] == 8192


def test_active_runtime_profile_is_the_server_admission_source(monkeypatch, tmp_path):
    """
    [핵심 검증 5: RAG Admission 토큰이 config 파일에서 직접 유도되는지 확인]
    
    임의의 환경변수(RAG_SEMAPHORE_COUNT=99)가 있더라도 무시하고,
    저장된 llm_runtime.json의 serving_concurrency=3을 단일 진실의 원천(SSOT)으로 사용해야 함.
    """
    config_path = tmp_path / "llm_runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "target": {
                    "endpoint_name": "Server runtime",
                    "base_url": "http://rag-runtime:8080",
                    "model": "test-model",
                    "runtime": "llama.cpp",
                    "serving_profile": {
                        "context_length": 4096,
                        "safe_concurrency_ceiling": 4,
                        "serving_concurrency": 3,
                        "calibration_status": "calibrated",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("RAG_SEMAPHORE_COUNT", "99")

    target = target_from_persisted_config()

    assert target is not None
    assert target.serving_profile["serving_concurrency"] == 3
    assert get_server_rag_serving_concurrency() == 3
