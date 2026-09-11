import json
from math import ceil

import pytest

from document_ai.services.rag_runtime_config import (
    LLMRuntimeNotConfigured,
    get_configured_server_rag_target,
    target_from_persisted_config,
)
from llm_installation.catalog import (
    catalog_rows,
    get_catalog_entry,
    get_rag_model_catalog,
)
from llm_installation.catalog.loader import _resolve_candidates
from llm_installation.catalog.models import (
    ArtifactCatalogEntry,
    ModelCatalogEntry,
)
from llm_installation.config_store import write_llm_runtime_config
from llm_installation.planner import (
    _place_cpu,
    _pool_fit,
    _resolve_llamacpp_batch_pair,
    assess_catalog_entry,
    build_serving_plan,
    estimate_model_memory,
    select_runtime,
)
from llm_installation.router import resolve_server_rag_target
from llm_installation.runtime_probe import (
    EndpointStatus,
    ServerRuntimeProfile,
    SmokeTestStatus,
)

pytestmark = pytest.mark.unit


GGUF_MODEL_ID = "qwen2.5-7b-instruct-q4_k_m"
AWQ_MODEL_ID = "qwen2.5-7b-instruct-awq"
BF16_MODEL_ID = "qwen2.5-7b-instruct-bf16"


def _profile(
    *,
    ram_mb=32768,
    disk_free_mb=102400,
    gpu_vram_mb=0,
    gpu_vram_list=None,
    gpu_vram_free_list=None,
    cluster_mode=False,
):
    gpu_vram_list = gpu_vram_list or ([] if not gpu_vram_mb else [gpu_vram_mb])
    gpu_vram_free_list = gpu_vram_free_list or list(gpu_vram_list)
    total_vram = sum(gpu_vram_list) if gpu_vram_list else gpu_vram_mb
    return ServerRuntimeProfile(
        cpu_count=8,
        ram_mb=ram_mb,
        has_gpu=bool(total_vram),
        gpu_name="Test GPU" if total_vram else "",
        gpu_vram_mb=total_vram,
        gpu_vram_free_mb=sum(gpu_vram_free_list),
        gpu_count=len(gpu_vram_list),
        gpu_vram_list=gpu_vram_list,
        gpu_vram_free_list=gpu_vram_free_list,
        cuda_available=bool(total_vram),
        platform="test",
        cluster_mode=cluster_mode,
        disk_free_mb=disk_free_mb,
    )


def test_catalog_loads_structured_entries():
    catalog = get_rag_model_catalog()

    assert {entry.id for entry in catalog} >= {GGUF_MODEL_ID, AWQ_MODEL_ID, BF16_MODEL_ID}
    assert "qwen2.5-7b-instruct" in {entry.model_id for entry in catalog}


def test_catalog_rejects_artifact_with_unknown_model_reference():
    model = ModelCatalogEntry.model_validate(
        {
            "id": "known-model",
            "display_name": "Known model",
            "model_metadata": {
                "parameter_count_b": 1,
                "hidden_size": 128,
                "num_hidden_layers": 2,
                "num_attention_heads": 2,
                "num_key_value_heads": 2,
            },
        }
    )
    artifact = ArtifactCatalogEntry.model_validate(
        {
            "id": "orphan-artifact",
            "model_id": "missing-model",
            "hf": {"repo_id": "test/orphan"},
            "artifact": {
                "format": "gguf",
                "quant": "Q4_K_M",
                "download_size_mb": 100,
            },
            "backend_profiles": ["llamacpp-cpu"],
        }
    )

    with pytest.raises(ValueError, match="unknown model"):
        _resolve_candidates([model], [artifact])


def test_catalog_rejects_backend_incompatible_with_artifact_format():
    with pytest.raises(ValueError, match="incompatible with artifact format gguf"):
        ArtifactCatalogEntry.model_validate(
            {
                "id": "invalid-gguf",
                "model_id": "known-model",
                "hf": {"repo_id": "test/invalid-gguf"},
                "artifact": {
                    "format": "gguf",
                    "quant": "Q4_K_M",
                    "download_size_mb": 100,
                },
                "backend_profiles": ["vllm-cuda"],
            }
        )


def test_runtime_is_resolved_from_artifact_and_deployment_mode():
    gguf = get_catalog_entry(GGUF_MODEL_ID)
    awq = get_catalog_entry(AWQ_MODEL_ID)
    bf16 = get_catalog_entry(BF16_MODEL_ID)

    assert select_runtime(gguf, _profile()).runtime == "llama.cpp"
    assert select_runtime(awq, _profile(gpu_vram_mb=24000)).runtime == "vllm"
    assert select_runtime(bf16, _profile(gpu_vram_mb=24000)).runtime == "vllm"
    assert select_runtime(bf16, _profile(gpu_vram_mb=24000, cluster_mode=True)).runtime == "vllm"


def test_gpu_host_does_not_fallback_from_gguf_to_cpu_profile():
    plan = build_serving_plan(
        get_catalog_entry(GGUF_MODEL_ID),
        _profile(ram_mb=65536, gpu_vram_mb=256),
        "quality",
    )

    assert plan.fit_status == "NOFIT"
    assert plan.backend_profile == "llamacpp-gpu-offload"
    assert plan.gpu_layers == 0


def test_speed_resolves_parallel_slots_within_policy_cap():
    entry = get_catalog_entry(GGUF_MODEL_ID)

    plan = build_serving_plan(entry, _profile(ram_mb=32768), "speed")

    assert plan.parallel == 2
    assert plan.concurrency == 2
    assert plan.max_num_seqs == 2
    assert plan.context_length == 4096
    assert plan.server_ctx_size == 8192
    assert plan.cache_type_k == "q8_0"


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        ("speed", (2048, 512)),
        ("balanced", (1024, 256)),
        ("quality", (512, 128)),
    ],
)
def test_llamacpp_batch_pair_uses_preset_priority_order(preset, expected):
    assert _resolve_llamacpp_batch_pair(preset) == expected


def test_llamacpp_threads_use_physical_core_count():
    profile = _profile(ram_mb=32768)
    profile = ServerRuntimeProfile(
        **{**profile.__dict__, "physical_cpu_cores": 4, "cpu_count": 16}
    )

    speed = build_serving_plan(get_catalog_entry(GGUF_MODEL_ID), profile, "speed")
    balanced = build_serving_plan(get_catalog_entry(GGUF_MODEL_ID), profile, "balanced")

    assert speed.threads == 3
    assert balanced.threads == 2


def test_llamacpp_context_candidates_respect_server_context_cap():
    profile = _profile(ram_mb=32768)
    profile = ServerRuntimeProfile(
        **{**profile.__dict__, "configured_context_cap": 4096}
    )

    plan = build_serving_plan(get_catalog_entry(GGUF_MODEL_ID), profile, "quality")

    assert plan.ctx_size_per_slot == 4096
    assert plan.configured_context_cap == 4096


def test_llamacpp_places_kv_in_ram_when_kv_offload_is_unsupported():
    profile = _profile(ram_mb=32768, gpu_vram_mb=16000)
    profile = ServerRuntimeProfile(
        **{**profile.__dict__, "llamacpp_kv_offload_supported": False}
    )

    plan = build_serving_plan(get_catalog_entry(GGUF_MODEL_ID), profile, "balanced")

    assert plan.kv_cache_placement == "ram"
    assert plan.memory_placement["ram_components_mb"]["kv_cache"] > 0
    assert plan.memory_placement["vram_components_per_gpu_mb"][0]["kv_cache"] == 0


def test_pool_neutral_memory_estimate_uses_quant_and_metadata():
    entry = get_catalog_entry(GGUF_MODEL_ID)

    estimate = estimate_model_memory(entry, context_length=4096, concurrency=1)

    assert estimate.weight_memory_mb == 4157
    assert estimate.kv_cache_memory_mb == 224
    assert estimate.runtime_overhead_mb == 512
    assert estimate.logical_total_memory_mb == 4893
    assert estimate.weight_estimation_source == "quant_bytes_per_param"


def test_unknown_quant_uses_conservative_f32_fallback():
    entry = get_catalog_entry(GGUF_MODEL_ID).model_copy(
        update={
            "artifact": get_catalog_entry(GGUF_MODEL_ID).artifact.model_copy(
                update={"quant": "unknown-quant"}
            ),
            "model_metadata": get_catalog_entry(GGUF_MODEL_ID).model_metadata.model_copy(
                update={"parameter_count_b": 1.0}
            ),
        }
    )

    estimate = estimate_model_memory(entry, context_length=4096, concurrency=1)

    assert estimate.quant_bytes_per_param == 4.0
    assert estimate.weight_memory_mb == 4096


def test_quality_uses_8k_only_when_memory_has_safety_margin():
    entry = get_catalog_entry(GGUF_MODEL_ID)

    constrained = build_serving_plan(entry, _profile(ram_mb=6200), "quality")
    roomy = build_serving_plan(entry, _profile(ram_mb=32768), "quality")

    assert constrained.context_length == 4096
    assert constrained.fit_status == "FIT"
    assert roomy.context_length == 16384
    assert roomy.kv_cache_memory_mb > constrained.kv_cache_memory_mb


def test_quality_16k_uses_installation_capacity_not_transient_free_ram():
    profile = _profile(ram_mb=8192)
    profile = ServerRuntimeProfile(
        **{**profile.__dict__, "ram_available_mb": 1024}
    )

    plan = build_serving_plan(get_catalog_entry(GGUF_MODEL_ID), profile, "quality")

    assert plan.ctx_size_per_slot == 16384
    assert plan.fit_status == "FIT"
    assert plan.planning_ram_mb == 8192


def test_vllm_fit_is_checked_per_gpu_not_by_aggregate_vram():
    plan = build_serving_plan(
        get_catalog_entry(AWQ_MODEL_ID),
        _profile(
            ram_mb=32768,
            gpu_vram_list=[12000, 2000],
            gpu_vram_free_list=[12000, 2000],
            cluster_mode=True,
        ),
        "balanced",
    )

    assert plan.runtime == "vllm"
    assert len(plan.required_vram_per_gpu_mb) == 2
    assert plan.required_vram_per_gpu_mb[0] == plan.required_vram_per_gpu_mb[1]
    assert plan.fit_status == "NOFIT"
    assert "GPU 1" in plan.reason


def test_cpu_ram_hard_and_soft_fit_boundaries_are_distinct():
    entry = get_catalog_entry(GGUF_MODEL_ID)
    estimate = estimate_model_memory(entry, context_length=4096, concurrency=1)
    placement = _place_cpu(estimate)

    risky, _ = _pool_fit(
        placement,
        _profile(ram_mb=estimate.logical_total_memory_mb),
        use_planning_capacity=True,
    )
    fit, _ = _pool_fit(
        placement,
        _profile(ram_mb=ceil(estimate.logical_total_memory_mb * 1.25)),
        use_planning_capacity=True,
    )

    assert placement.required_ram_mb == estimate.logical_total_memory_mb
    assert risky == "RISKY"
    assert fit == "FIT"


def test_router_allows_operator_selected_risky_llamacpp_plan():
    entry = get_catalog_entry(GGUF_MODEL_ID)
    estimate = estimate_model_memory(
        entry,
        context_length=8192,
        concurrency=1,
        cache_type_k="q8_0",
        cache_type_v="q8_0",
    )

    target = resolve_server_rag_target(
        profile=_profile(ram_mb=estimate.logical_total_memory_mb),
        catalog=[entry],
        check_endpoint=False,
        risky_confirmed=True,
        priority_preset="balanced",
    )

    assert target.fallback_used is False
    assert target.serving_profile["fit_status"] == "RISKY"


def test_runtime_policy_preserves_risky_catalog_status():
    entry = get_catalog_entry(GGUF_MODEL_ID)
    estimate = estimate_model_memory(
        entry,
        context_length=8192,
        concurrency=1,
        cache_type_k="q8_0",
        cache_type_v="q8_0",
    )
    profile = _profile(ram_mb=estimate.logical_total_memory_mb)

    assessment = assess_catalog_entry(entry, profile)
    plan = build_serving_plan(entry, profile, "balanced")

    assert assessment.fit_status == "RISKY"
    assert plan.fit_status == "RISKY"


def test_disk_shortage_is_a_hard_blocker_independent_of_memory_fit():
    entry = get_catalog_entry(GGUF_MODEL_ID)

    plan = build_serving_plan(
        entry,
        _profile(ram_mb=32768, disk_free_mb=entry.artifact.disk_required_mb - 1),
        "balanced",
    )

    assert plan.fit_status == "NOFIT"
    assert "Disk needs" in plan.reason


def test_disk_headroom_produces_risky_status():
    entry = get_catalog_entry(GGUF_MODEL_ID)

    plan = build_serving_plan(
        entry,
        _profile(ram_mb=32768, disk_free_mb=entry.artifact.disk_required_mb),
        "balanced",
    )

    assert plan.fit_status == "RISKY"
    assert "15% headroom" in plan.reason


def test_disk_requirement_prefers_catalog_value_over_policy_multiplier():
    entry = get_catalog_entry(AWQ_MODEL_ID)

    assessment = assess_catalog_entry(entry, _profile(ram_mb=32768, gpu_vram_mb=24000))

    assert assessment.disk_required_mb == entry.artifact.disk_required_mb


def test_router_uses_llamacpp_on_cpu_only_host():
    target = resolve_server_rag_target(
        profile=_profile(ram_mb=16384),
        check_endpoint=False,
        priority_preset="balanced",
    )

    assert target.model == GGUF_MODEL_ID
    assert target.runtime == "llama.cpp"
    assert target.priority_preset == "balanced"
    assert target.serving_profile["safe_concurrency_ceiling"] == 4
    assert target.serving_profile["serving_concurrency"] == 1
    assert target.serving_profile["concurrency"] == 1
    assert target.serving_profile["calibration_status"] == "pending"


def test_speed_priority_selects_awq_vllm_candidate():
    target = resolve_server_rag_target(
        profile=_profile(ram_mb=32768, gpu_vram_mb=24000),
        catalog=[get_catalog_entry(GGUF_MODEL_ID), get_catalog_entry(AWQ_MODEL_ID)],
        check_endpoint=False,
        priority_preset="speed",
    )

    assert target.model == AWQ_MODEL_ID
    assert target.runtime == "vllm"


def test_router_does_not_reselect_after_endpoint_failure(monkeypatch):
    def fake_check(base_url, expected_model=""):
        if expected_model == AWQ_MODEL_ID:
            return EndpointStatus(base_url=base_url, ok=False, check_name="models", message="not ready")
        return EndpointStatus(base_url=base_url, ok=True, check_name="models", message="ok")

    monkeypatch.setattr(
        "llm_installation.router.check_endpoint_health",
        lambda base_url: EndpointStatus(base_url=base_url, ok=True, check_name="health", message="ok"),
    )
    monkeypatch.setattr(
        "llm_installation.router.check_openai_compatible_endpoint",
        fake_check,
    )

    with pytest.raises(LLMRuntimeNotConfigured, match="failed validation"):
        resolve_server_rag_target(
            profile=_profile(ram_mb=32768, gpu_vram_mb=24000),
            catalog=[get_catalog_entry(GGUF_MODEL_ID), get_catalog_entry(AWQ_MODEL_ID)],
            check_endpoint=True,
            priority_preset="speed",
        )


def test_router_does_not_reselect_after_smoke_failure(monkeypatch):
    def fake_smoke(base_url, model):
        if model == AWQ_MODEL_ID:
            return SmokeTestStatus(base_url=base_url, model=model, ok=False, message="completion failed")
        return SmokeTestStatus(base_url=base_url, model=model, ok=True, message="ok")

    monkeypatch.setattr(
        "llm_installation.router.smoke_test_chat_completion",
        fake_smoke,
    )

    with pytest.raises(LLMRuntimeNotConfigured, match="failed validation"):
        resolve_server_rag_target(
            profile=_profile(ram_mb=32768, gpu_vram_mb=24000),
            catalog=[get_catalog_entry(GGUF_MODEL_ID), get_catalog_entry(AWQ_MODEL_ID)],
            check_endpoint=False,
            smoke_test=True,
            priority_preset="speed",
        )


def test_catalog_rows_include_assessment_without_runtime_parameters():
    rows = catalog_rows(_profile(ram_mb=32768))
    row = next(item for item in rows if item["id"] == GGUF_MODEL_ID)

    assert row["fit_status"] == "FIT"
    assert row["catalog_status"] == "ACTIVE"
    assert row["auto_selectable"] is True
    assert row["manual_selectable"] is True
    assert row["context_length"] == 4096
    assert row["concurrency"] == 1
    assessment = row["catalog_assessment"]
    assert assessment["memory_estimate"]["logical_total_memory_mb"] > 0
    assert assessment["memory_placement"]["required_ram_mb"] > 0
    assert "required_vram_per_gpu_mb" in assessment["memory_placement"]
    assert "server_ctx_size" not in assessment
    assert "parallel" not in assessment
    assert "threads" not in assessment


def test_router_loads_persisted_config_without_live_probe(monkeypatch, tmp_path):
    config_path = tmp_path / "llm_runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "target": {
                    "endpoint_name": "Server auto",
                    "base_url": "http://configured-rag:8080/v1",
                    "model": "configured-model",
                    "runtime": "llama.cpp",
                    "reason": "test config",
                    "fallback_used": False,
                    "priority_preset": "quality",
                    "serving_profile": {"context_length": 8192, "concurrency": 1},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(
        "llm_installation.router.probe_server_runtime",
        lambda: pytest.fail("normal routing must not probe hardware"),
    )

    target = target_from_persisted_config()

    assert target.model == "configured-model"
    assert target.base_url == "http://configured-rag:8080"
    assert target.priority_preset == "quality"
    assert target.serving_profile["context_length"] == 8192
    assert target.serving_profile["safe_concurrency_ceiling"] == 1
    assert target.serving_profile["serving_concurrency"] == 1


def test_configured_router_rejects_missing_persisted_config(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(
        "llm_installation.router.build_serving_plan",
        lambda *args, **kwargs: pytest.fail("request-time code must not run planner"),
    )

    with pytest.raises(LLMRuntimeNotConfigured):
        get_configured_server_rag_target()


def test_write_runtime_config_persists_plan_and_llama_args(tmp_path):
    profile = _profile(ram_mb=16384)
    catalog = get_rag_model_catalog()
    target = resolve_server_rag_target(
        profile=profile,
        catalog=catalog,
        check_endpoint=False,
        priority_preset="balanced",
    )

    config_path = write_llm_runtime_config(
        target=target,
        profile=profile,
        catalog=catalog,
        path=tmp_path / "llm_runtime.json",
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    args = (tmp_path / "llama_rag.args").read_text(encoding="utf-8").splitlines()
    assert payload["target"]["priority_preset"] == "balanced"
    serving_profile = payload["target"]["serving_profile"]
    assert serving_profile["safe_concurrency_ceiling"] == 4
    assert serving_profile["serving_concurrency"] == 1
    assert serving_profile["concurrency"] == 1
    assert serving_profile["calibration_status"] == "pending"
    assert payload["catalog"][0]["model_metadata"]["parameter_count_b"] == 7.0
    assert payload["catalog"][0]["model_id"] == "qwen2.5-7b-instruct"
    assert payload["profile"]["configured_context_cap"] == 16384
    assert payload["profile"]["llamacpp_kv_offload_supported"] is True
    assert args[args.index("--ctx-size") + 1] == "8192"
    assert args[args.index("--parallel") + 1] == "1"
    assert args[args.index("--cache-type-k") + 1] == "q8_0"
    assert args[args.index("--cache-type-v") + 1] == "q8_0"
    assert args[args.index("--alias") + 1] == GGUF_MODEL_ID
    assert args[args.index("--hf-repo") + 1].endswith(":Q4_K_M")


def test_llamacpp_spills_to_ram_when_vram_is_insufficient():
    plan = build_serving_plan(
        get_catalog_entry(GGUF_MODEL_ID),
        _profile(ram_mb=32768, gpu_vram_mb=4096),
        "balanced",
    )

    assert plan.runtime == "llama.cpp"
    assert plan.candidate_type == "GPU partial-offload"
    assert plan.offload == "partial"
    assert 0 < plan.n_gpu_layers < 28
    assert "RAM" in plan.reason
    placement = plan.memory_placement
    assert placement["ram_components_mb"]["model_weights"] < plan.weight_memory_mb
    assert placement["vram_components_per_gpu_mb"][0]["model_weights"] > 0


def test_llamacpp_full_offload_does_not_require_weight_copy_in_ram():
    plan = build_serving_plan(
        get_catalog_entry(GGUF_MODEL_ID),
        _profile(ram_mb=1024, gpu_vram_mb=16000),
        "balanced",
    )

    assert plan.candidate_type == "GPU full-offload"
    assert plan.fit_status == "FIT"
    assert plan.required_ram_mb == 512
    assert plan.parallel == 4
    assert plan.gpu_layers == 28
    assert plan.memory_placement["ram_components_mb"]["model_weights"] == 0
    assert plan.required_vram_per_gpu_mb[0] == sum(
        plan.memory_placement["vram_components_per_gpu_mb"][0].values()
    )


def test_awq_does_not_fall_back_to_ram_when_vram_is_insufficient():
    plan = build_serving_plan(
        get_catalog_entry(AWQ_MODEL_ID),
        _profile(ram_mb=65536, gpu_vram_mb=4096),
        "balanced",
    )

    assert plan.runtime == "vllm"
    assert plan.fit_status == "NOFIT"
    assert "RAM spill is not supported" in plan.reason


def test_write_runtime_config_generates_vllm_args_for_awq(tmp_path):
    profile = _profile(ram_mb=32768, gpu_vram_mb=24000)
    catalog = [get_catalog_entry(AWQ_MODEL_ID)]
    target = resolve_server_rag_target(
        profile=profile,
        catalog=catalog,
        check_endpoint=False,
        priority_preset="speed",
    )

    config_path = write_llm_runtime_config(
        target=target,
        profile=profile,
        catalog=catalog,
        path=tmp_path / "llm_runtime.json",
    )

    args = (tmp_path / "vllm_rag.args").read_text(encoding="utf-8").splitlines()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["version"] == 8
    assert payload["target"]["runtime"] == "vllm"
    serving_profile = payload["target"]["serving_profile"]
    assert serving_profile["logical_total_memory_mb"] > 0
    assert len(serving_profile["required_vram_per_gpu_mb"]) == 1
    assert serving_profile["safe_concurrency_ceiling"] == 2
    assert serving_profile["serving_concurrency"] == 1
    assert args[args.index("--model") + 1] == "Qwen/Qwen2.5-7B-Instruct-AWQ"
    assert args[args.index("--max-num-seqs") + 1] == "1"
    assert args[args.index("--max-num-batched-tokens") + 1] == "4096"
    assert args[args.index("--quantization") + 1] == "awq"
