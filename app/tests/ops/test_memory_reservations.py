import json

import pytest

from llm_installation.embedding_footprint import (
    build_embedding_claim,
    estimate_embedding_components,
    resolve_embedding_device,
)
from llm_installation.memory_reservations import (
    CLAIM_EMBEDDING,
    CLAIM_LLM,
    DEVICE_CPU,
    DEVICE_CUDA,
    DEVICE_REMOTE,
    SOURCE_PLANNER,
    MemoryClaim,
    build_llm_claim,
    clear_claim,
    get_memory_reservations_path,
    get_total_reservation,
    read_reservations,
    write_claim,
)


pytestmark = pytest.mark.unit


def _llm_claim(**overrides):
    defaults = dict(
        workload=CLAIM_LLM,
        generation_id="gen-llm",
        device=DEVICE_CUDA,
        ram_mb=512,
        vram_per_gpu_mb=(4000,),
        components={"model_weights": 3500, "kv_cache": 500},
        source=SOURCE_PLANNER,
    )
    defaults.update(overrides)
    return MemoryClaim(**defaults)


def _embedding_claim(**overrides):
    defaults = dict(
        workload=CLAIM_EMBEDDING,
        generation_id="gen-embedding",
        device=DEVICE_CUDA,
        ram_mb=256,
        vram_per_gpu_mb=(3148,),
        components={"model_weights": 1136, "cuda_context": 1500},
        bounds={"max_batch": 16, "max_tokens": 1280},
    )
    defaults.update(overrides)
    return MemoryClaim(**defaults)


def _write_llm_generation(root, generation_id, placement):
    generation_dir = (
        root
        / "data"
        / "config"
        / "runtime_scopes"
        / "production"
        / "generations"
        / generation_id
    )
    generation_dir.mkdir(parents=True, exist_ok=True)
    (generation_dir / "runtime.json").write_text(
        json.dumps({"target": {"serving_profile": {"memory_placement": placement}}}),
        encoding="utf-8",
    )


def test_unclaimed_scope_reserves_nothing(tmp_path):
    total = get_total_reservation("production", repo_root=tmp_path)

    assert total.ram_mb == 0
    assert total.vram_per_gpu_mb == ()
    assert total.workloads == ()


def test_each_workload_claim_survives_the_other_being_written(tmp_path):
    write_claim(_llm_claim(), scope="production", repo_root=tmp_path)
    write_claim(_embedding_claim(), scope="production", repo_root=tmp_path)

    claims = read_reservations("production", repo_root=tmp_path)

    assert set(claims) == {CLAIM_LLM, CLAIM_EMBEDDING}
    assert claims[CLAIM_LLM].components["model_weights"] == 3500
    assert claims[CLAIM_EMBEDDING].bounds == {"max_batch": 16, "max_tokens": 1280}


def test_total_sums_each_pool_separately(tmp_path):
    write_claim(_llm_claim(), scope="production", repo_root=tmp_path)
    write_claim(_embedding_claim(), scope="production", repo_root=tmp_path)

    total = get_total_reservation("production", repo_root=tmp_path)

    assert total.ram_mb == 512 + 256
    assert total.vram_per_gpu_mb == (4000 + 3148,)


def test_total_pads_gpu_lists_of_different_lengths(tmp_path):
    write_claim(
        _llm_claim(vram_per_gpu_mb=(4000, 4000)), scope="production", repo_root=tmp_path
    )
    write_claim(
        _embedding_claim(vram_per_gpu_mb=(3148,)), scope="production", repo_root=tmp_path
    )

    total = get_total_reservation("production", repo_root=tmp_path)

    assert total.vram_per_gpu_mb == (7148, 4000)
    assert total.vram_for_gpu(1) == 4000
    assert total.vram_for_gpu(5) == 0


def test_replanning_a_workload_excludes_its_own_previous_claim(tmp_path):
    write_claim(_llm_claim(), scope="production", repo_root=tmp_path)
    write_claim(_embedding_claim(), scope="production", repo_root=tmp_path)

    total = get_total_reservation(
        "production", exclude=CLAIM_LLM, repo_root=tmp_path
    )

    assert total.ram_mb == 256
    assert total.vram_per_gpu_mb == (3148,)
    assert total.workloads == (CLAIM_EMBEDDING,)


def test_clearing_one_claim_leaves_the_other_intact(tmp_path):
    write_claim(_llm_claim(), scope="production", repo_root=tmp_path)
    write_claim(_embedding_claim(), scope="production", repo_root=tmp_path)

    clear_claim(CLAIM_LLM, scope="production", repo_root=tmp_path)

    claims = read_reservations("production", repo_root=tmp_path)
    assert set(claims) == {CLAIM_EMBEDDING}
    assert clear_claim(CLAIM_LLM, scope="production", repo_root=tmp_path) is None


def test_unreadable_file_reads_as_unclaimed_rather_than_raising(tmp_path):
    path = get_memory_reservations_path("production", repo_root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert read_reservations("production", repo_root=tmp_path) == {}
    assert get_total_reservation("production", repo_root=tmp_path).ram_mb == 0


def test_llm_claim_is_derived_from_the_planner_placement(tmp_path):
    _write_llm_generation(
        tmp_path,
        "gen-1",
        {
            "required_ram_mb": 3304,
            "required_vram_per_gpu_mb": [],
            "ram_components_mb": {
                "model_weights": 2376,
                "kv_cache": 416,
                "runtime_overhead": 512,
            },
        },
    )

    claim = build_llm_claim("production", "gen-1", repo_root=tmp_path)

    assert claim.ram_mb == 3304
    assert claim.device == DEVICE_CPU
    assert claim.components["model_weights"] == 2376
    assert claim.source == SOURCE_PLANNER


def test_llm_claim_on_gpu_records_vram_and_merges_gpu_components(tmp_path):
    _write_llm_generation(
        tmp_path,
        "gen-gpu",
        {
            "required_ram_mb": 512,
            "required_vram_per_gpu_mb": [5000, 5000],
            "ram_components_mb": {"runtime_overhead": 512},
            "vram_components_per_gpu_mb": [
                {"model_weights": 4000, "kv_cache": 1000},
                {"model_weights": 4000, "kv_cache": 1000},
            ],
        },
    )

    claim = build_llm_claim("production", "gen-gpu", repo_root=tmp_path)

    assert claim.device == DEVICE_CUDA
    assert claim.vram_per_gpu_mb == (5000, 5000)
    assert claim.components["model_weights"] == 8000


def test_missing_generation_yields_no_llm_claim(tmp_path):
    assert build_llm_claim("production", "absent", repo_root=tmp_path) is None


class _Footprint:
    def __init__(self, parameter_count_m, peak_activation_mb=0):
        self.parameter_count_m = parameter_count_m
        self.peak_activation_mb = peak_activation_mb


class _Entry:
    def __init__(self, provider, footprint=None):
        self.provider = provider
        self.footprint = footprint


def test_embedding_weights_double_on_cpu_because_it_runs_fp32():
    entry = _Entry("bgem3_hybrid", _Footprint(568))

    cuda = estimate_embedding_components(entry, device=DEVICE_CUDA)
    cpu = estimate_embedding_components(entry, device=DEVICE_CPU)

    assert cuda["model_weights"] == 1136
    assert cpu["model_weights"] == 2272
    assert "cuda_context" in cuda
    assert "cuda_context" not in cpu


def test_embedding_claim_lands_in_the_pool_it_runs_on():
    entry = _Entry("bgem3_hybrid", _Footprint(568))

    on_gpu = build_embedding_claim(entry, generation_id="g", device=DEVICE_CUDA)
    on_cpu = build_embedding_claim(entry, generation_id="g", device=DEVICE_CPU)

    assert on_gpu.ram_mb == 0
    assert on_gpu.vram_per_gpu_mb == (1136 + 1500 + 512,)
    assert on_cpu.vram_per_gpu_mb == ()
    assert on_cpu.ram_mb == 2272 + 512


def test_embedding_claim_targets_only_its_own_gpu():
    entry = _Entry("bgem3_hybrid", _Footprint(568))

    claim = build_embedding_claim(
        entry, generation_id="g", device=DEVICE_CUDA, gpu_index=1, gpu_count=3
    )

    assert claim.vram_per_gpu_mb == (0, 3148, 0)


def test_remote_provider_claims_nothing_locally():
    entry = _Entry("openai_compatible")

    assert resolve_embedding_device("openai_compatible", has_gpu=True) == DEVICE_REMOTE

    claim = build_embedding_claim(entry, generation_id="g", device=DEVICE_REMOTE)
    assert claim.ram_mb == 0
    assert claim.vram_per_gpu_mb == ()
    assert claim.components == {}


def test_entry_without_a_declared_footprint_still_claims_process_overhead():
    claim = build_embedding_claim(
        _Entry("sentence_transformers"), generation_id="g", device=DEVICE_CPU
    )

    assert claim.ram_mb > 0
    assert "model_weights" not in claim.components
