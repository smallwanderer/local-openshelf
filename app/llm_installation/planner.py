from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Any, Literal

from llm_installation.runtime_handoff import (
    RuntimePolicyInput,
)


PRIORITY_PRESETS = ("speed", "balanced", "quality")
HEADROOM_MULTIPLIER = 1.25
DISK_HEADROOM_MULTIPLIER = 1.15
CPU_RUNTIME_OVERHEAD_MB = 512
GPU_RUNTIME_OVERHEAD_MB = 512
CUDA_CONTEXT_OVERHEAD_MB = 1500

QUANT_BYTES_PER_PARAM = {
    "f32": 4.0,
    "fp32": 4.0,
    "f16": 2.0,
    "fp16": 2.0,
    "bf16": 2.0,
    "bfloat16": 2.0,
    "q8_0": 1.05,
    "q6_k": 0.80,
    "q5_k_m": 0.68,
    "q4_k_m": 0.58,
    "q4_0": 0.58,
    "q3_k_m": 0.48,
    "q2_k": 0.37,
    "awq": 0.50,
    "awq-int4": 0.50,
    "gptq": 0.50,
    "gptq-int4": 0.50,
}

CACHE_BYTES = {
    "f16": 2.0,
    "fp16": 2.0,
    "q8_0": 1.0,
    "q4_0": 0.5,
}


class RuntimeConfigUnresolvable(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeChoice:
    runtime: str
    backend_profile: str | None
    backend_status: Literal["AVAILABLE", "UNAVAILABLE"] = "AVAILABLE"
    reason_code: str = ""
    reason: str = ""


@dataclass(frozen=True)
class PresetPolicy:
    priority_preset: str
    target_context: int
    target_concurrency: int
    memory_policy: str
    engine_preference: str


@dataclass(frozen=True)
class MemoryEstimate:
    weight_memory_mb: int
    kv_cache_memory_mb: int
    runtime_overhead_mb: int
    cuda_context_overhead_mb: int
    logical_total_memory_mb: int
    quant_bytes_per_param: float
    kv_cache_memory_mb_per_token: float
    weight_estimation_source: str
    warnings: tuple[str, ...] = ()

    @property
    def model_weights_mb(self) -> int:
        return self.weight_memory_mb

    @property
    def baseline_kv_cache_mb(self) -> int:
        return self.kv_cache_memory_mb

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "model_weights_mb": self.weight_memory_mb,
            "baseline_kv_cache_mb": self.kv_cache_memory_mb,
        }


@dataclass(frozen=True)
class MemoryPlacement:
    required_ram_mb: int
    required_vram_per_gpu_mb: tuple[int, ...]
    ram_components_mb: dict[str, int]
    vram_components_per_gpu_mb: tuple[dict[str, int], ...]
    device_indices: tuple[int, ...] = ()
    tensor_parallel_size: int = 1
    gpu_layers: int | None = None

    @property
    def base_ram_mb(self) -> int:
        return self.required_ram_mb

    @property
    def base_vram_per_gpu_mb(self) -> tuple[int, ...]:
        return self.required_vram_per_gpu_mb

    def as_dict(self) -> dict[str, Any]:
        return {
            "required_ram_mb": self.required_ram_mb,
            "required_vram_per_gpu_mb": list(self.required_vram_per_gpu_mb),
            "base_ram_mb": self.base_ram_mb,
            "base_vram_per_gpu_mb": list(self.base_vram_per_gpu_mb),
            "ram_components_mb": dict(self.ram_components_mb),
            "vram_components_per_gpu_mb": [
                dict(item) for item in self.vram_components_per_gpu_mb
            ],
            "device_indices": list(self.device_indices),
            "tensor_parallel_size": self.tensor_parallel_size,
            "gpu_layers": self.gpu_layers,
        }


@dataclass(frozen=True)
class CatalogAssessment:
    artifact_id: str
    model_id: str
    runtime: str | None
    backend_profile: str | None
    backend_status: str
    backend_reason_code: str
    backend_reason: str
    context_length: int
    concurrency: int
    memory_estimate: MemoryEstimate | None
    memory_placement: MemoryPlacement | None
    disk_required_mb: int | None
    estimated_decode_tps: float | None
    fit_status: str
    fit_reason: str
    fit_reason_codes: tuple[str, ...]
    selectable: bool
    catalog_entry: Any = field(repr=False)
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        entry = self.catalog_entry
        return {
            "artifact_id": self.artifact_id,
            "model_id": self.model_id,
            "runtime": self.runtime,
            "backend_profile": self.backend_profile,
            "backend_status": self.backend_status,
            "backend_reason_code": self.backend_reason_code,
            "backend_reason": self.backend_reason,
            "context_length": self.context_length,
            "concurrency": self.concurrency,
            "memory_estimate": (
                self.memory_estimate.as_dict() if self.memory_estimate else None
            ),
            "memory_placement": (
                self.memory_placement.as_dict() if self.memory_placement else None
            ),
            "disk_required_mb": self.disk_required_mb,
            "estimated_decode_tps": self.estimated_decode_tps,
            "fit_status": self.fit_status,
            "fit_reason": self.fit_reason,
            "fit_reason_codes": list(self.fit_reason_codes),
            "selectable": self.selectable,
            "warnings": list(self.warnings),
            "catalog_entry": (
                entry.model_dump(mode="json")
                if hasattr(entry, "model_dump")
                else entry
            ),
        }


@dataclass(frozen=True)
class ServingPlan:
    artifact_id: str
    runtime: str
    backend_profile: str
    base_url: str
    device: str
    candidate_type: str
    offload: str
    fit_status: str
    reason: str
    priority_preset: str
    context_length: int
    ctx_size_per_slot: int
    server_ctx_size: int
    configured_context_cap: int
    concurrency: int
    parallel: int
    max_num_seqs: int
    max_num_batched_tokens: int
    cache_type_k: str
    cache_type_v: str
    kv_cache_placement: str
    gpu_layers: int
    n_gpu_layers: int
    threads: int
    batch_size: int
    ubatch_size: int
    tensor_parallel_size: int
    gpu_memory_utilization: float
    weight_memory_mb: int
    kv_cache_memory_mb: int
    runtime_overhead_mb: int
    logical_total_memory_mb: int
    required_ram_mb: int
    required_vram_per_gpu_mb: tuple[int, ...]
    planning_ram_mb: int
    memory_placement: dict[str, Any]

    @property
    def preset(self) -> str:
        return self.priority_preset

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # ``concurrency`` is the preset- and memory-derived calibration
        # ceiling at this planning stage.  ServerRAGTarget normalizes it into
        # a separate initial operating value before persistence.
        payload["safe_concurrency_ceiling"] = self.concurrency
        payload["required_vram_per_gpu_mb"] = list(
            self.required_vram_per_gpu_mb
        )
        return payload


def _attr(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _attr(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_attr(item) for item in value]
    return value


def _quant_key(entry: Any) -> str:
    quant = str(getattr(entry.artifact, "quant", "") or "").strip().lower()
    dtype = str(getattr(entry.artifact, "dtype", "") or "").strip().lower()
    artifact_format = str(entry.artifact.format).strip().lower()
    for key in (quant, dtype, artifact_format):
        if key in QUANT_BYTES_PER_PARAM:
            return key
    return quant


def _cache_bytes(cache_type_k: str, cache_type_v: str) -> tuple[float, float]:
    return (
        CACHE_BYTES.get(cache_type_k.lower(), 2.0),
        CACHE_BYTES.get(cache_type_v.lower(), 2.0),
    )


def _gpu_total_list(profile: Any) -> list[int]:
    values = list(getattr(profile, "gpu_vram_list", None) or [])
    if not values and int(getattr(profile, "gpu_vram_mb", 0) or 0):
        values = [int(profile.gpu_vram_mb)]
    return [int(value) for value in values]


def _gpu_free_list(profile: Any) -> list[int]:
    values = list(getattr(profile, "gpu_vram_free_list", None) or [])
    if not values:
        values = _gpu_total_list(profile)
    return [int(value) for value in values]


def _gpu_count(profile: Any) -> int:
    return len(_gpu_total_list(profile))


def _planning_ram(profile: Any) -> int:
    total = int(getattr(profile, "ram_mb", 0) or 0)
    limit = int(getattr(profile, "container_memory_limit_mb", 0) or 0)
    return min(total, limit) if limit > 0 else total


def _resolved_disk_required_mb(entry: Any) -> int | None:
    explicit_required = int(entry.artifact.disk_required_mb or 0)
    if entry.artifact.download_size_mb:
        return max(explicit_required, math.ceil(entry.artifact.download_size_mb))
    if explicit_required:
        return explicit_required
    return None


def convert_policy(priority_preset: str) -> PresetPolicy:
    if priority_preset not in PRIORITY_PRESETS:
        raise ValueError(f"Unknown priority preset: {priority_preset}")
    return {
        "speed": PresetPolicy(
            "speed", 4096, 2, "latency", "highest estimated decode TPS"
        ),
        "balanced": PresetPolicy(
            "balanced", 8192, 4, "headroom", "catalog recommendation"
        ),
        "quality": PresetPolicy(
            "quality", 16384, 1, "context", "highest model quality"
        ),
    }[priority_preset]


def select_runtime(entry: Any, profile: Any) -> RuntimeChoice:
    artifact_format = str(entry.artifact.format).lower()
    declared = set(getattr(entry, "backend_profiles", []) or [])
    if artifact_format in {"awq", "gptq", "safetensors", "compress-tensors"}:
        if "vllm-cuda" not in declared:
            return RuntimeChoice(
                runtime="",
                backend_profile=None,
                backend_status="UNAVAILABLE",
                reason_code="NO_DECLARED_BACKEND",
                reason="Artifact does not declare vllm-cuda.",
            )
        if not bool(getattr(profile, "cuda_available", False)):
            return RuntimeChoice(
                runtime="",
                backend_profile=None,
                backend_status="UNAVAILABLE",
                reason_code="CUDA_UNAVAILABLE",
                reason="CUDA is unavailable for this artifact.",
            )
        return RuntimeChoice(
            "vllm",
            "vllm-cuda",
            reason_code="SELECTED_VLLM_CUDA",
            reason="Artifact is compatible with vLLM CUDA.",
        )

    if artifact_format == "gguf":
        if bool(getattr(profile, "cluster_mode", False)):
            return RuntimeChoice(
                runtime="",
                backend_profile=None,
                backend_status="UNAVAILABLE",
                reason_code="CLUSTER_REQUIRES_VLLM",
                reason="Cluster mode does not accept standalone GGUF.",
            )
        has_gpu = bool(_gpu_count(profile))
        offload_supported = getattr(
            profile, "llamacpp_gpu_offload_supported", None
        )
        if (
            has_gpu
            and offload_supported is not False
            and "llamacpp-gpu-offload" in declared
        ):
            return RuntimeChoice(
                "llama.cpp",
                "llamacpp-gpu-offload",
                reason_code="SELECTED_LLAMACPP_GPU_OFFLOAD",
                reason="GGUF uses llama.cpp GPU offload.",
            )
        if "llamacpp-cpu" in declared:
            return RuntimeChoice(
                "llama.cpp",
                "llamacpp-cpu",
                reason_code="SELECTED_LLAMACPP_CPU",
                reason="GGUF uses llama.cpp CPU.",
            )
    return RuntimeChoice(
        runtime="",
        backend_profile=None,
        backend_status="UNAVAILABLE",
        reason_code="NO_DECLARED_BACKEND",
        reason="No compatible backend is available.",
    )


def estimate_model_memory(
    entry: Any,
    *,
    context_length: int = 4096,
    concurrency: int = 1,
    cache_type_k: str = "f16",
    cache_type_v: str = "f16",
    use_cuda: bool = False,
) -> MemoryEstimate:
    quant_key = _quant_key(entry)
    quant_bytes = QUANT_BYTES_PER_PARAM.get(quant_key, 4.0)
    warnings = ()
    if quant_key not in QUANT_BYTES_PER_PARAM:
        warnings = ("UNKNOWN_QUANT_F32_FALLBACK",)
    weight_mb = round(
        float(entry.model_metadata.parameter_count_b) * quant_bytes * 1024
    )
    metadata = entry.model_metadata
    layers = int(metadata.num_hidden_layers)
    kv_heads = int(
        metadata.num_key_value_heads or metadata.num_attention_heads
    )
    head_dim = int(
        metadata.head_dim
        or (metadata.hidden_size // metadata.num_attention_heads)
    )
    key_bytes, value_bytes = _cache_bytes(cache_type_k, cache_type_v)
    kv_per_token = (
        layers * kv_heads * head_dim * (key_bytes + value_bytes)
    ) / (1024 * 1024)
    kv_mb = max(
        1,
        round(kv_per_token * int(context_length) * int(concurrency)),
    )
    cuda_overhead = CUDA_CONTEXT_OVERHEAD_MB if use_cuda else 0
    logical_total = (
        weight_mb
        + kv_mb
        + CPU_RUNTIME_OVERHEAD_MB
        + cuda_overhead
    )
    return MemoryEstimate(
        weight_memory_mb=weight_mb,
        kv_cache_memory_mb=kv_mb,
        runtime_overhead_mb=CPU_RUNTIME_OVERHEAD_MB,
        cuda_context_overhead_mb=cuda_overhead,
        logical_total_memory_mb=logical_total,
        quant_bytes_per_param=quant_bytes,
        kv_cache_memory_mb_per_token=kv_per_token,
        weight_estimation_source="quant_bytes_per_param",
        warnings=warnings,
    )


def _place_cpu(estimate: MemoryEstimate, gpu_count: int = 0) -> MemoryPlacement:
    ram = {
        "model_weights": estimate.weight_memory_mb,
        "kv_cache": estimate.kv_cache_memory_mb,
        "runtime_overhead": estimate.runtime_overhead_mb,
    }
    vram = tuple(
        {"model_weights": 0, "kv_cache": 0, "runtime_overhead": 0, "cuda_context": 0}
        for _ in range(gpu_count)
    )
    return MemoryPlacement(
        required_ram_mb=sum(ram.values()),
        required_vram_per_gpu_mb=tuple(0 for _ in range(gpu_count)),
        ram_components_mb=ram,
        vram_components_per_gpu_mb=vram,
        gpu_layers=0,
    )


def _split_evenly(total: int, count: int) -> list[int]:
    if count <= 0:
        return []
    base, remainder = divmod(total, count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


def _place_vllm(estimate: MemoryEstimate, gpu_count: int) -> MemoryPlacement:
    weights = _split_evenly(estimate.weight_memory_mb, gpu_count)
    kv = _split_evenly(estimate.kv_cache_memory_mb, gpu_count)
    components = tuple(
        {
            "model_weights": weights[index],
            "kv_cache": kv[index],
            "runtime_overhead": GPU_RUNTIME_OVERHEAD_MB,
            "cuda_context": CUDA_CONTEXT_OVERHEAD_MB,
        }
        for index in range(gpu_count)
    )
    requirements = tuple(sum(item.values()) for item in components)
    ram = {
        "model_weights": 0,
        "kv_cache": 0,
        "runtime_overhead": CPU_RUNTIME_OVERHEAD_MB,
    }
    return MemoryPlacement(
        required_ram_mb=sum(ram.values()),
        required_vram_per_gpu_mb=requirements,
        ram_components_mb=ram,
        vram_components_per_gpu_mb=components,
        device_indices=tuple(range(gpu_count)),
        tensor_parallel_size=gpu_count,
        gpu_layers=None,
    )


def _place_llamacpp_gpu(
    estimate: MemoryEstimate,
    profile: Any,
    *,
    num_layers: int,
    kv_cache_placement: str,
    force_layers: int | None = None,
) -> MemoryPlacement:
    free = _gpu_free_list(profile)
    count = len(free)
    kv_vram = estimate.kv_cache_memory_mb if kv_cache_placement == "vram" else 0
    fixed_vram = GPU_RUNTIME_OVERHEAD_MB + CUDA_CONTEXT_OVERHEAD_MB + kv_vram
    per_layer = estimate.weight_memory_mb / max(1, num_layers)
    if force_layers is None:
        safe_capacity = math.floor(free[0] / HEADROOM_MULTIPLIER) if free else 0
        layers = math.floor(max(0, safe_capacity - fixed_vram) / per_layer)
    else:
        layers = force_layers
    layers = max(0, min(num_layers, layers))
    weight_vram = (
        estimate.weight_memory_mb
        if layers == num_layers
        else math.ceil(layers * per_layer)
    )
    weight_ram = max(0, estimate.weight_memory_mb - weight_vram)
    ram_components = {
        "model_weights": weight_ram,
        "kv_cache": (
            estimate.kv_cache_memory_mb if kv_cache_placement == "ram" else 0
        ),
        "runtime_overhead": CPU_RUNTIME_OVERHEAD_MB,
    }
    components = [
        {"model_weights": 0, "kv_cache": 0, "runtime_overhead": 0, "cuda_context": 0}
        for _ in range(count)
    ]
    if components:
        components[0] = {
            "model_weights": weight_vram,
            "kv_cache": kv_vram,
            "runtime_overhead": GPU_RUNTIME_OVERHEAD_MB,
            "cuda_context": CUDA_CONTEXT_OVERHEAD_MB,
        }
    return MemoryPlacement(
        required_ram_mb=sum(ram_components.values()),
        required_vram_per_gpu_mb=tuple(sum(item.values()) for item in components),
        ram_components_mb=ram_components,
        vram_components_per_gpu_mb=tuple(components),
        device_indices=(0,) if components else (),
        tensor_parallel_size=1,
        gpu_layers=layers,
    )


def _pool_fit(
    placement: MemoryPlacement,
    profile: Any,
    *,
    use_planning_capacity: bool = False,
) -> tuple[str, str]:
    available_ram = (
        _planning_ram(profile)
        if use_planning_capacity
        else int(getattr(profile, "ram_available_mb", 0) or _planning_ram(profile))
    )
    hard_reasons = []
    risky_reasons = []
    if placement.required_ram_mb > available_ram:
        hard_reasons.append(
            f"RAM needs {placement.required_ram_mb} MB but {available_ram} MB is available."
        )
    elif placement.required_ram_mb * HEADROOM_MULTIPLIER > available_ram:
        risky_reasons.append("RAM has less than 25% safety headroom.")
    free_vram = _gpu_free_list(profile)
    for index, required in enumerate(placement.required_vram_per_gpu_mb):
        available = free_vram[index] if index < len(free_vram) else 0
        if required > available:
            hard_reasons.append(
                f"GPU {index} needs {required} MB but {available} MB is available."
            )
        elif required * HEADROOM_MULTIPLIER > available:
            risky_reasons.append(f"GPU {index} has less than 25% safety headroom.")
    if hard_reasons:
        return "NOFIT", " ".join(hard_reasons)
    if risky_reasons:
        return "RISKY", " ".join(risky_reasons)
    return "FIT", "All memory pools have safety headroom."


def _disk_fit(entry: Any, profile: Any) -> tuple[str, str, int | None]:
    required = _resolved_disk_required_mb(entry)
    if required is None:
        return "FIT", "Disk requirement is unknown.", None
    available = int(getattr(profile, "disk_free_mb", 0) or 0)
    if required > available:
        return (
            "NOFIT",
            f"Disk needs {required} MB but {available} MB is available.",
            required,
        )
    if required * DISK_HEADROOM_MULTIPLIER > available:
        return (
            "RISKY",
            "Disk fits but has less than 15% headroom.",
            required,
        )
    return "FIT", "Disk has safety headroom.", required


def _combine_status(*statuses: str) -> str:
    if "NOFIT" in statuses:
        return "NOFIT"
    if "RISKY" in statuses:
        return "RISKY"
    return "FIT"


def _estimate_decode_tps(
    entry: Any,
    profile: Any,
    choice: RuntimeChoice,
    estimate: MemoryEstimate,
    placement: MemoryPlacement,
) -> float | None:
    if choice.backend_profile == "llamacpp-cpu":
        cores = int(
            getattr(profile, "physical_cpu_cores", 0)
            or max(1, int(getattr(profile, "cpu_count", 1)) // 2)
        )
        features = {str(item).lower() for item in (getattr(profile, "cpu_features", None) or [])}
        factor = (
            1.35 if "amx" in features
            else 1.25 if "avx512" in features
            else 1.0 if "avx2" in features
            else 0.8 if "neon" in features
            else 0.6
        )
        quant_factor = min(
            1.35,
            max(0.45, math.sqrt(0.58 / estimate.quant_bytes_per_param)),
        )
        return round(
            cores * factor * quant_factor * 3.0
            / float(entry.model_metadata.parameter_count_b),
            1,
        )
    bandwidth = float(getattr(profile, "gpu_mem_bandwidth_gb_s", 0) or 0)
    if bandwidth <= 0:
        return None
    effective_read = (
        estimate.weight_memory_mb / 1024
    ) * (1.15 if estimate.quant_bytes_per_param < 1 else 1.0)
    return round(bandwidth / max(effective_read, 0.001), 1)


def assess_catalog_entry(entry: Any, profile: Any) -> CatalogAssessment:
    choice = select_runtime(entry, profile)
    if choice.backend_status == "UNAVAILABLE":
        return CatalogAssessment(
            artifact_id=entry.id,
            model_id=entry.model_id,
            runtime=None,
            backend_profile=None,
            backend_status="UNAVAILABLE",
            backend_reason_code=choice.reason_code,
            backend_reason=choice.reason,
            context_length=min(entry.model_metadata.max_context_length, 4096),
            concurrency=1,
            memory_estimate=None,
            memory_placement=None,
            disk_required_mb=None,
            estimated_decode_tps=None,
            fit_status="NOFIT",
            fit_reason=choice.reason,
            fit_reason_codes=("BACKEND_UNAVAILABLE",),
            selectable=False,
            catalog_entry=entry,
        )
    context = min(int(entry.model_metadata.max_context_length), 4096)
    estimate = estimate_model_memory(
        entry,
        context_length=context,
        concurrency=1,
        use_cuda=choice.backend_profile == "vllm-cuda",
    )
    if choice.backend_profile == "llamacpp-cpu":
        placement = _place_cpu(estimate, _gpu_count(profile))
    elif choice.backend_profile == "vllm-cuda":
        placement = _place_vllm(estimate, _gpu_count(profile))
    else:
        kv_placement = (
            "vram"
            if bool(getattr(profile, "llamacpp_kv_offload_supported", True))
            else "ram"
        )
        placement = _place_llamacpp_gpu(
            estimate,
            profile,
            num_layers=entry.model_metadata.num_hidden_layers,
            kv_cache_placement=kv_placement,
        )
        if placement.gpu_layers == 0:
            placement = _place_llamacpp_gpu(
                estimate,
                profile,
                num_layers=entry.model_metadata.num_hidden_layers,
                kv_cache_placement=kv_placement,
                force_layers=1,
            )
    memory_status, memory_reason = _pool_fit(
        placement, profile, use_planning_capacity=True
    )
    disk_status, disk_reason, disk_required = _disk_fit(entry, profile)
    status = _combine_status(memory_status, disk_status)
    reason = f"{memory_reason} {disk_reason}".strip()
    if choice.backend_profile == "vllm-cuda" and memory_status == "NOFIT":
        reason += " RAM spill is not supported by vLLM."
    reason_codes = []
    if memory_status == "NOFIT":
        reason_codes.append("MEMORY_INSUFFICIENT")
    elif memory_status == "RISKY":
        reason_codes.append("MEMORY_HEADROOM_LOW")
    if disk_status == "NOFIT":
        reason_codes.append("DISK_INSUFFICIENT")
    elif disk_status == "RISKY":
        reason_codes.append("DISK_HEADROOM_LOW")
    return CatalogAssessment(
        artifact_id=entry.id,
        model_id=entry.model_id,
        runtime=choice.runtime,
        backend_profile=choice.backend_profile,
        backend_status=choice.backend_status,
        backend_reason_code=choice.reason_code,
        backend_reason=choice.reason,
        context_length=context,
        concurrency=1,
        memory_estimate=estimate,
        memory_placement=placement,
        disk_required_mb=disk_required,
        estimated_decode_tps=_estimate_decode_tps(
            entry, profile, choice, estimate, placement
        ),
        fit_status=status,
        fit_reason=reason,
        fit_reason_codes=tuple(reason_codes),
        selectable=status in {"FIT", "RISKY"},
        catalog_entry=entry,
        warnings=estimate.warnings,
    )


def model_preference_score(entry: Any, priority_preset: str) -> tuple:
    parameters = float(entry.model_metadata.parameter_count_b)
    priority = int(entry.priority)
    quant = QUANT_BYTES_PER_PARAM.get(_quant_key(entry), 0.0)
    if priority_preset == "quality":
        return parameters, quant, entry.model_metadata.max_context_length, priority
    if priority_preset == "speed":
        return priority, -parameters
    return priority, parameters


def _resolve_llamacpp_batch_pair(priority_preset: str) -> tuple[int, int]:
    return {
        "speed": (2048, 512),
        "balanced": (1024, 256),
        "quality": (512, 128),
    }[priority_preset]


def _threads(profile: Any, preset: str) -> int:
    cores = int(
        getattr(profile, "physical_cpu_cores", 0)
        or getattr(profile, "cpu_count", 1)
        or 1
    )
    usable = max(1, cores - 1)
    if preset == "speed":
        return usable
    return max(1, math.floor(usable * 0.75))


def _context_candidates(entry: Any, profile: Any, preset: str) -> list[int]:
    values = {
        "speed": [4096, 2048],
        "balanced": [8192, 4096, 2048],
        "quality": [16384, 8192, 4096],
    }[preset]
    cap = min(
        int(entry.model_metadata.max_context_length),
        int(getattr(profile, "configured_context_cap", 16384) or 16384),
    )
    filtered = [value for value in values if value <= cap]
    return filtered or [max(1, cap)]


def _plan_llamacpp(
    entry: Any,
    profile: Any,
    preset: str,
    backend_profile: str,
    preserved_fit_status: str | None,
) -> ServingPlan:
    batch_size, ubatch_size = _resolve_llamacpp_batch_pair(preset)
    parallel_cap = {"speed": 2, "balanced": 4, "quality": 1}[preset]
    cache_candidates = (
        [("f16", "f16"), ("q8_0", "q8_0")]
        if preset == "quality"
        else [("q8_0", "q8_0"), ("q4_0", "q4_0")]
    )
    best_risky = None
    for cache_k, cache_v in cache_candidates:
        for context in _context_candidates(entry, profile, preset):
            kv_placements = ["ram"]
            if (
                backend_profile == "llamacpp-gpu-offload"
                and bool(getattr(profile, "llamacpp_kv_offload_supported", True))
            ):
                kv_placements = ["vram", "ram"]
            for kv_placement in kv_placements:
                baseline = estimate_model_memory(
                    entry,
                    context_length=context,
                    concurrency=1,
                    cache_type_k=cache_k,
                    cache_type_v=cache_v,
                )
                if backend_profile == "llamacpp-cpu":
                    baseline_placement = _place_cpu(baseline, _gpu_count(profile))
                else:
                    baseline_placement = _place_llamacpp_gpu(
                        baseline,
                        profile,
                        num_layers=entry.model_metadata.num_hidden_layers,
                        kv_cache_placement=kv_placement,
                    )
                    if baseline_placement.gpu_layers == 0:
                        baseline_placement = _place_llamacpp_gpu(
                            baseline,
                            profile,
                            num_layers=entry.model_metadata.num_hidden_layers,
                            kv_cache_placement=kv_placement,
                            force_layers=1,
                        )
                baseline_status, _ = _pool_fit(
                    baseline_placement,
                    profile,
                    use_planning_capacity=True,
                )
                if baseline_status == "NOFIT":
                    continue
                parallel = parallel_cap
                while parallel >= 1:
                    estimate = estimate_model_memory(
                        entry,
                        context_length=context,
                        concurrency=parallel,
                        cache_type_k=cache_k,
                        cache_type_v=cache_v,
                    )
                    if backend_profile == "llamacpp-cpu":
                        placement = _place_cpu(estimate, _gpu_count(profile))
                    else:
                        placement = _place_llamacpp_gpu(
                            estimate,
                            profile,
                            num_layers=entry.model_metadata.num_hidden_layers,
                            kv_cache_placement=kv_placement,
                        )
                        if placement.gpu_layers == 0:
                            placement = _place_llamacpp_gpu(
                                estimate,
                                profile,
                                num_layers=entry.model_metadata.num_hidden_layers,
                                kv_cache_placement=kv_placement,
                                force_layers=1,
                            )
                    status, reason = _pool_fit(
                        placement,
                        profile,
                        use_planning_capacity=True,
                    )
                    disk_status, disk_reason, _ = _disk_fit(entry, profile)
                    status = _combine_status(status, disk_status)
                    reason = f"{reason} {disk_reason}".strip()
                    if status != "NOFIT":
                        layers = int(placement.gpu_layers or 0)
                        if backend_profile == "llamacpp-cpu":
                            candidate_type = "CPU"
                            offload = "none"
                        elif layers >= entry.model_metadata.num_hidden_layers:
                            candidate_type = "GPU full-offload"
                            offload = "full"
                        else:
                            candidate_type = "GPU partial-offload"
                            offload = "partial"
                            reason += " Remaining model weights are placed in RAM."
                        output_status = preserved_fit_status or status
                        plan = ServingPlan(
                            artifact_id=entry.id,
                            runtime="llama.cpp",
                            backend_profile=backend_profile,
                            base_url="http://rag-runtime:8080",
                            device=(
                                "CPU"
                                if backend_profile == "llamacpp-cpu"
                                else "GPU"
                            ),
                            candidate_type=candidate_type,
                            offload=offload,
                            fit_status=output_status,
                            reason=reason,
                            priority_preset=preset,
                            context_length=context,
                            ctx_size_per_slot=context,
                            server_ctx_size=context * parallel,
                            configured_context_cap=int(
                                getattr(profile, "configured_context_cap", 16384)
                                or 16384
                            ),
                            concurrency=parallel,
                            parallel=parallel,
                            max_num_seqs=parallel,
                            max_num_batched_tokens=context * parallel,
                            cache_type_k=cache_k,
                            cache_type_v=cache_v,
                            kv_cache_placement=kv_placement,
                            gpu_layers=layers,
                            n_gpu_layers=layers,
                            threads=_threads(profile, preset),
                            batch_size=batch_size,
                            ubatch_size=ubatch_size,
                            tensor_parallel_size=1,
                            gpu_memory_utilization=0.9,
                            weight_memory_mb=estimate.weight_memory_mb,
                            kv_cache_memory_mb=estimate.kv_cache_memory_mb,
                            runtime_overhead_mb=estimate.runtime_overhead_mb,
                            logical_total_memory_mb=(
                                placement.required_ram_mb
                                + sum(placement.required_vram_per_gpu_mb)
                            ),
                            required_ram_mb=placement.required_ram_mb,
                            required_vram_per_gpu_mb=placement.required_vram_per_gpu_mb,
                            planning_ram_mb=_planning_ram(profile),
                            memory_placement=placement.as_dict(),
                        )
                        if status == "FIT":
                            return plan
                        if best_risky is None:
                            best_risky = plan
                    parallel -= 1
    if best_risky is not None:
        if preserved_fit_status == "FIT":
            raise RuntimeConfigUnresolvable("RUNTIME_CONFIG_UNRESOLVABLE")
        return best_risky
    # Diagnostic legacy plan for direct inspection; Stage 6 path raises below.
    estimate = estimate_model_memory(entry, context_length=4096, concurrency=1)
    if backend_profile == "llamacpp-cpu":
        placement = _place_cpu(estimate, _gpu_count(profile))
        layers = 0
    else:
        placement = _place_llamacpp_gpu(
            estimate,
            profile,
            num_layers=entry.model_metadata.num_hidden_layers,
            kv_cache_placement="vram",
        )
        layers = int(placement.gpu_layers or 0)
    memory_status, memory_reason = _pool_fit(
        placement,
        profile,
        use_planning_capacity=True,
    )
    disk_status, disk_reason, _ = _disk_fit(entry, profile)
    status = _combine_status(memory_status, disk_status)
    reason = f"{memory_reason} {disk_reason}".strip()
    return ServingPlan(
        artifact_id=entry.id,
        runtime="llama.cpp",
        backend_profile=backend_profile,
        base_url="http://rag-runtime:8080",
        device="CPU" if backend_profile == "llamacpp-cpu" else "GPU",
        candidate_type="Unavailable",
        offload="none",
        fit_status=status,
        reason=reason,
        priority_preset=preset,
        context_length=4096,
        ctx_size_per_slot=4096,
        server_ctx_size=4096,
        configured_context_cap=int(
            getattr(profile, "configured_context_cap", 16384) or 16384
        ),
        concurrency=1,
        parallel=1,
        max_num_seqs=1,
        max_num_batched_tokens=4096,
        cache_type_k="f16",
        cache_type_v="f16",
        kv_cache_placement="ram",
        gpu_layers=layers,
        n_gpu_layers=layers,
        threads=_threads(profile, preset),
        batch_size=batch_size,
        ubatch_size=ubatch_size,
        tensor_parallel_size=1,
        gpu_memory_utilization=0.9,
        weight_memory_mb=estimate.weight_memory_mb,
        kv_cache_memory_mb=estimate.kv_cache_memory_mb,
        runtime_overhead_mb=estimate.runtime_overhead_mb,
        logical_total_memory_mb=(
            placement.required_ram_mb + sum(placement.required_vram_per_gpu_mb)
        ),
        required_ram_mb=placement.required_ram_mb,
        required_vram_per_gpu_mb=placement.required_vram_per_gpu_mb,
        planning_ram_mb=_planning_ram(profile),
        memory_placement=placement.as_dict(),
    )


def _plan_vllm(
    entry: Any,
    profile: Any,
    preset: str,
    preserved_fit_status: str | None,
) -> ServingPlan:
    context = min(
        convert_policy(preset).target_context,
        int(entry.model_metadata.max_context_length),
        int(getattr(profile, "configured_context_cap", 16384) or 16384),
    )
    parallel = convert_policy(preset).target_concurrency
    estimate = estimate_model_memory(
        entry,
        context_length=context,
        concurrency=parallel,
        use_cuda=True,
    )
    placement = _place_vllm(estimate, _gpu_count(profile))
    status, reason = _pool_fit(
        placement, profile, use_planning_capacity=True
    )
    disk_status, disk_reason, _ = _disk_fit(entry, profile)
    status = _combine_status(status, disk_status)
    reason = f"{reason} {disk_reason}".strip()
    if status == "NOFIT":
        reason += " RAM spill is not supported by vLLM."
        if preserved_fit_status is not None:
            raise RuntimeConfigUnresolvable("RUNTIME_CONFIG_UNRESOLVABLE")
    if preserved_fit_status == "FIT" and status != "FIT":
        raise RuntimeConfigUnresolvable("RUNTIME_CONFIG_UNRESOLVABLE")
    return ServingPlan(
        artifact_id=entry.id,
        runtime="vllm",
        backend_profile="vllm-cuda",
        base_url="http://rag-runtime:8080",
        device="GPU",
        candidate_type="GPU vLLM",
        offload="full",
        fit_status=preserved_fit_status or status,
        reason=reason,
        priority_preset=preset,
        context_length=context,
        ctx_size_per_slot=context,
        server_ctx_size=context * parallel,
        configured_context_cap=int(
            getattr(profile, "configured_context_cap", 16384) or 16384
        ),
        concurrency=parallel,
        parallel=parallel,
        max_num_seqs=parallel,
        max_num_batched_tokens=context * parallel,
        cache_type_k="f16",
        cache_type_v="f16",
        kv_cache_placement="vram",
        gpu_layers=entry.model_metadata.num_hidden_layers,
        n_gpu_layers=entry.model_metadata.num_hidden_layers,
        threads=_threads(profile, preset),
        batch_size=_resolve_llamacpp_batch_pair(preset)[0],
        ubatch_size=_resolve_llamacpp_batch_pair(preset)[1],
        tensor_parallel_size=max(1, _gpu_count(profile)),
        gpu_memory_utilization=0.9,
        weight_memory_mb=estimate.weight_memory_mb,
        kv_cache_memory_mb=estimate.kv_cache_memory_mb,
        runtime_overhead_mb=estimate.runtime_overhead_mb,
        logical_total_memory_mb=(
            placement.required_ram_mb + sum(placement.required_vram_per_gpu_mb)
        ),
        required_ram_mb=placement.required_ram_mb,
        required_vram_per_gpu_mb=placement.required_vram_per_gpu_mb,
        planning_ram_mb=_planning_ram(profile),
        memory_placement=placement.as_dict(),
    )


def _entry_from_handoff(handoff: RuntimePolicyInput) -> Any:
    entry_data = handoff.catalog_assessment.get("catalog_entry")
    if not isinstance(entry_data, dict):
        raise RuntimeConfigUnresolvable(
            "RuntimePolicyInput has no catalog entry snapshot."
        )
    return _attr(entry_data)


def build_serving_plan(
    runtime_policy_input_or_entry: Any,
    profile: Any | None = None,
    priority_preset: str = "balanced",
) -> ServingPlan:
    handoff = (
        runtime_policy_input_or_entry
        if isinstance(runtime_policy_input_or_entry, RuntimePolicyInput)
        else None
    )
    if handoff is not None:
        if not handoff.verify_integrity():
            raise RuntimeConfigUnresolvable(
                "RUNTIME_HANDOFF_INTEGRITY_ERROR"
            )
        entry = _entry_from_handoff(handoff)
        profile = _attr(handoff.hardware_profile)
        priority_preset = handoff.priority_preset
        backend_profile = handoff.backend_profile
        preserved_fit_status = handoff.fit_status
    else:
        entry = runtime_policy_input_or_entry
        if profile is None:
            raise TypeError("Legacy planner call requires a hardware profile.")
        choice = select_runtime(entry, profile)
        if choice.backend_status == "UNAVAILABLE":
            assessment = assess_catalog_entry(entry, profile)
            raise RuntimeConfigUnresolvable(assessment.fit_reason)
        backend_profile = choice.backend_profile
        preserved_fit_status = None
    if priority_preset not in PRIORITY_PRESETS:
        raise ValueError(f"Unknown priority preset: {priority_preset}")
    if backend_profile == "vllm-cuda":
        plan = _plan_vllm(
            entry, profile, priority_preset, preserved_fit_status
        )
    else:
        plan = _plan_llamacpp(
            entry,
            profile,
            priority_preset,
            backend_profile,
            preserved_fit_status,
        )
    if handoff is not None and plan.fit_status == "NOFIT":
        raise RuntimeConfigUnresolvable(
            "RUNTIME_CONFIG_UNRESOLVABLE"
        )
    return plan
