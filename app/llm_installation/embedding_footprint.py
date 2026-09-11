"""Declared memory footprint of an embedding runtime.

The LLM side gets its numbers from the planner, which already sizes a model
before starting it. The embedding side has no planner: a catalog entry is
picked by preset and activated directly. This module fills that gap so both
workloads can record a claim of the same shape.

What is declared here is a floor, not a measurement. Resident weights are
derived from the parameter count the way the planner derives them from
``parameter_count_b``; the activation peak stays 0 until a real run reports
one. A claim therefore says "at least this much", and the ``source`` field on
the claim says whether anything has been observed yet.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from llm_installation.memory_reservations import (
    CLAIM_EMBEDDING,
    DEVICE_CPU,
    DEVICE_CUDA,
    DEVICE_REMOTE,
    SOURCE_DECLARED,
    MemoryClaim,
    write_claim,
)
from llm_installation.planner import (
    CPU_RUNTIME_OVERHEAD_MB,
    CUDA_CONTEXT_OVERHEAD_MB,
)


# BGE-M3 runs fp16 on CUDA and fp32 on CPU (see BGEM3HybridProvider's
# use_fp16), and sentence_transformers defaults to fp32. Deriving weights from
# the device instead of storing one number per model keeps the catalog honest
# about costing twice as much on CPU.
BYTES_PER_PARAM = {
    DEVICE_CUDA: 2.0,
    DEVICE_CPU: 4.0,
}

# Providers that reach someone else's hardware hold nothing locally.
REMOTE_PROVIDERS = frozenset({"openai_compatible"})


def resolve_embedding_device(provider: str, *, has_gpu: bool) -> str:
    """Decide which pool an embedding runtime will land in.

    Mirrors the provider's own choice: the local providers select CUDA when
    torch reports a device and fall back to CPU otherwise.
    """
    if provider in REMOTE_PROVIDERS:
        return DEVICE_REMOTE
    return DEVICE_CUDA if has_gpu else DEVICE_CPU


def estimate_embedding_components(entry: Any, *, device: str) -> dict[str, int]:
    """Return the per-component memory cost of holding this entry resident."""
    if device == DEVICE_REMOTE:
        return {}

    footprint = getattr(entry, "footprint", None)
    if footprint is None:
        # An entry with no declared footprint contributes only the process
        # overhead. Claiming zero would be worse: it would read as "measured
        # and free" rather than "not declared yet".
        return {"runtime_overhead": CPU_RUNTIME_OVERHEAD_MB}

    bytes_per_param = BYTES_PER_PARAM.get(device, BYTES_PER_PARAM[DEVICE_CPU])
    weights_mb = round(float(footprint.parameter_count_m) * bytes_per_param)

    components = {
        "model_weights": weights_mb,
        "peak_activation": int(footprint.peak_activation_mb),
        "runtime_overhead": CPU_RUNTIME_OVERHEAD_MB,
    }
    if device == DEVICE_CUDA:
        components["cuda_context"] = CUDA_CONTEXT_OVERHEAD_MB
    return components


def build_embedding_claim(
    entry: Any,
    *,
    generation_id: str,
    device: str,
    gpu_index: int = 0,
    gpu_count: int = 1,
    bounds: dict[str, int] | None = None,
) -> MemoryClaim:
    """Build the embedding claim for a catalog entry on a resolved device."""
    components = estimate_embedding_components(entry, device=device)
    total_mb = sum(components.values())

    if device == DEVICE_CUDA:
        # The executor loads one model on one device; the other GPUs stay free.
        vram = [0] * max(gpu_count, gpu_index + 1)
        vram[gpu_index] = total_mb
        ram_mb = 0
        vram_per_gpu = tuple(vram)
    else:
        ram_mb = total_mb
        vram_per_gpu = ()

    return MemoryClaim(
        workload=CLAIM_EMBEDDING,
        generation_id=generation_id,
        device=device,
        ram_mb=ram_mb,
        vram_per_gpu_mb=vram_per_gpu,
        components=components,
        bounds=dict(bounds or {}),
        source=SOURCE_DECLARED,
    )


def current_embedding_bounds() -> dict[str, int]:
    """Capture the request limits the declared peak is only valid under.

    A footprint measured at 16 x 1280 tokens says nothing about a server
    configured for more, so the limits travel with the claim and a later check
    can tell whether they still hold.
    """
    bounds: dict[str, int] = {}
    try:
        from django.conf import settings

        from document_ai.embedding.internal_views import _MAX_BATCH_SIZE

        bounds["max_batch"] = int(_MAX_BATCH_SIZE)
        bounds["max_tokens"] = int(getattr(settings, "EMBEDDING_MAX_TOKENS", 0))
    except Exception:
        # The installer runs outside Django too; bounds are diagnostic, so a
        # missing settings module must not block recording the claim.
        return {key: value for key, value in bounds.items() if value}
    return {key: value for key, value in bounds.items() if value}


def detect_local_gpu() -> tuple[bool, int]:
    """Return (has_gpu, gpu_count) for placing an embedding claim.

    Activation is a rare maintenance operation, so paying for one GPU probe
    here is cheaper than storing a placement that never gets corrected.
    """
    try:
        from llm_installation.runtime_probe import _probe_gpu_info

        result = _probe_gpu_info()
    except Exception:
        return False, 0
    count = int(getattr(result, "gpu_count", 0) or 0)
    return count > 0, count


def claim_embedding_from_generation(
    scope: str,
    generation_id: str,
    *,
    repo_root: Any = None,
) -> MemoryClaim | None:
    """Record the embedding claim for an activated generation.

    Reads the generation's own runtime.json rather than taking an entry
    argument, so every activation path -- initial detection, model change,
    external endpoint staging -- funnels through one place.
    """
    import json
    from pathlib import Path

    from llm_installation.embedding_config_store import (
        get_embedding_runtime_config_path,
    )

    active_path = get_embedding_runtime_config_path(
        scope, repo_root=Path(repo_root) if repo_root else None
    )
    runtime_json = (
        active_path.parent
        / "embedding_generations"
        / generation_id
        / "runtime.json"
    )
    try:
        payload = json.loads(runtime_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    entry = None
    catalog_id = str(payload.get("catalog_id") or "")
    if catalog_id:
        try:
            from llm_installation.embedding_catalog import (
                get_embedding_catalog_entry,
            )

            entry = get_embedding_catalog_entry(catalog_id)
        except Exception:
            entry = None

    provider = str(payload.get("provider") or "")
    if entry is None:
        # An externally staged endpoint has no catalog entry; the provider
        # alone still decides whether anything is held locally.
        entry = SimpleNamespace(provider=provider, footprint=None)

    has_gpu, gpu_count = (
        detect_local_gpu() if provider not in REMOTE_PROVIDERS else (False, 0)
    )
    return claim_embedding_runtime(
        entry,
        scope=scope,
        generation_id=generation_id,
        has_gpu=has_gpu,
        gpu_count=max(gpu_count, 1),
        repo_root=repo_root,
    )


def claim_embedding_runtime(
    entry: Any,
    *,
    scope: str,
    generation_id: str,
    has_gpu: bool = False,
    gpu_index: int = 0,
    gpu_count: int = 1,
    repo_root: Any = None,
) -> MemoryClaim | None:
    """Record the embedding claim for a newly activated generation.

    Best effort, like the LLM side: an activated embedding runtime must not be
    reported as failed because its claim could not be written.
    """
    provider = str(getattr(entry, "provider", "") or "")
    device = resolve_embedding_device(provider, has_gpu=has_gpu)
    claim = build_embedding_claim(
        entry,
        generation_id=generation_id,
        device=device,
        gpu_index=gpu_index,
        gpu_count=gpu_count,
        bounds=current_embedding_bounds(),
    )
    try:
        write_claim(claim, scope=scope, repo_root=repo_root)
    except OSError:
        return None
    return claim
