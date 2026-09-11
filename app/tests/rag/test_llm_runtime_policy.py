from types import SimpleNamespace

import pytest

from llm_installation.planner import (
    RuntimeConfigUnresolvable,
    assess_catalog_entry,
    build_serving_plan,
)
from llm_installation.runtime_handoff import (
    build_runtime_policy_input,
)
from llm_installation.selection import (
    SelectionCandidate,
    SelectionResult,
)

pytestmark = pytest.mark.unit


def _entry():
    data = {
        "id": "qwen-gguf",
        "model_id": "qwen",
        "priority": 90,
        "backend_profiles": ["llamacpp-gpu-offload", "llamacpp-cpu"],
        "artifact": {
            "format": "gguf",
            "quant": "Q4_K_M",
            "dtype": None,
            "download_size_mb": 4680,
            "disk_required_mb": 5200,
        },
        "model_metadata": {
            "parameter_count_b": 7.0,
            "max_context_length": 32768,
            "hidden_size": 3584,
            "num_hidden_layers": 28,
            "num_attention_heads": 28,
            "num_key_value_heads": 4,
            "head_dim": 128,
        },
    }
    entry = SimpleNamespace(
        id=data["id"],
        model_id=data["model_id"],
        priority=data["priority"],
        backend_profiles=data["backend_profiles"],
        artifact=SimpleNamespace(**data["artifact"]),
        model_metadata=SimpleNamespace(**data["model_metadata"]),
    )
    entry.model_dump = lambda mode="json": data
    return entry


def _profile(*, ram_mb=32768, gpu_vram_mb=0):
    gpu_values = [gpu_vram_mb] if gpu_vram_mb else []
    return SimpleNamespace(
        cpu_count=8,
        physical_cpu_cores=4,
        ram_mb=ram_mb,
        ram_available_mb=ram_mb,
        container_memory_limit_mb=0,
        gpu_vram_list=gpu_values,
        gpu_vram_free_list=gpu_values,
        gpu_vram_mb=gpu_vram_mb,
        cuda_available=bool(gpu_values),
        cluster_mode=False,
        disk_free_mb=102400,
        llamacpp_gpu_offload_supported=None,
        llamacpp_kv_offload_supported=True,
        configured_context_cap=16384,
        gpu_mem_bandwidth_gb_s=0,
        cpu_features=[],
    )


def _handoff(entry, profile, preset="balanced"):
    assessment = assess_catalog_entry(entry, profile)
    fit_evaluation = SimpleNamespace(fit_status=assessment.fit_status)
    selection = SelectionResult(
        selection_status="SELECTED",
        selection_mode="automatic",
        priority_preset=preset,
        selected_candidate=SelectionCandidate(
            entry,
            assessment,
            fit_evaluation,
        ),
        ranked_artifact_ids=[entry.id],
        reason_code=f"SELECTED_FIT_BY_{preset.upper()}",
        risky_confirmed=assessment.fit_status == "RISKY",
    )
    return build_runtime_policy_input(selection, profile)


def test_stage7_uses_handoff_and_resolves_balanced_cpu_parameters():
    plan = build_serving_plan(_handoff(_entry(), _profile()))

    assert plan.backend_profile == "llamacpp-cpu"
    assert plan.context_length == 8192
    assert plan.parallel == 4
    assert plan.server_ctx_size == 32768
    assert plan.cache_type_k == "q8_0"


def test_stage7_preserves_fixed_gpu_backend():
    plan = build_serving_plan(
        _handoff(_entry(), _profile(gpu_vram_mb=16000))
    )

    assert plan.backend_profile == "llamacpp-gpu-offload"
    assert plan.gpu_layers == 28


def test_stage7_rejects_tampered_handoff():
    handoff = _handoff(_entry(), _profile())
    object.__setattr__(handoff, "backend_profile", "vllm-cuda")

    with pytest.raises(
        RuntimeConfigUnresolvable,
        match="RUNTIME_HANDOFF_INTEGRITY_ERROR",
    ):
        build_serving_plan(handoff)
