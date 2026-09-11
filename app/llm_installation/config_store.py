from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from document_ai.services.rag_runtime_config import (
    get_llm_runtime_config_path,
    normalize_serving_profile,
)
from llm_installation.runtime_probe import ServerRuntimeProfile


CONFIG_VERSION = 8
RUNTIME_BASE_URL = "http://rag-runtime:8080"
LEGACY_ARGS_FILE = {
    "llama.cpp": "llama_rag.args",
    "vllm": "vllm_rag.args",
}

CALIBRATION_STATUSES = {"running", "calibrated"}


def _write_runtime_args(path: Path, args_text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as args_file:
        args_file.write(args_text)


def _serialize_catalog_entry(entry) -> dict[str, Any]:
    if hasattr(entry, "model_dump"):
        return entry.model_dump(mode="json")
    return asdict(entry)


def _selected_entry_snapshot(target) -> dict[str, Any] | None:
    handoff = target.runtime_policy_input
    if not isinstance(handoff, dict):
        return None
    assessment = handoff.get("catalog_assessment")
    if not isinstance(assessment, dict):
        return None
    entry = assessment.get("catalog_entry")
    return entry if isinstance(entry, dict) else None


def _build_llama_runtime_args(*, target, profile) -> list[str] | None:
    if target.runtime != "llama.cpp" or not target.serving_profile:
        return None
    entry = _selected_entry_snapshot(target)
    if entry is None or entry.get("id") != target.model:
        return None

    plan = normalize_serving_profile(target.serving_profile)
    hf = entry.get("hf") or {}
    artifact = entry.get("artifact") or {}
    hf_repo = str(hf.get("repo_id") or "")
    filename = artifact.get("filename")
    if filename:
        args = ["--hf-repo", hf_repo, "--hf-file", str(filename)]
    else:
        if artifact.get("quant"):
            hf_repo = f"{hf_repo}:{artifact['quant']}"
        args = ["--hf-repo", hf_repo]
    args.extend(
        [
            "--alias",
            entry["id"],
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "--threads",
            str(plan.get("threads", max(1, int(profile.cpu_count or 1) - 1))),
            "--ctx-size",
            str(plan.get("server_ctx_size", plan["context_length"])),
            "--parallel",
            str(plan.get("parallel", plan["concurrency"])),
            "--batch-size",
            str(plan["batch_size"]),
            "--ubatch-size",
            str(plan["ubatch_size"]),
            "--metrics",
        ]
    )
    args.extend(
        [
            "--cache-type-k",
            str(plan.get("cache_type_k", "f16")),
            "--cache-type-v",
            str(plan.get("cache_type_v", "f16")),
            "--n-gpu-layers",
            str(plan.get("gpu_layers", plan.get("n_gpu_layers", 0))),
        ]
    )
    if plan.get("kv_cache_placement") == "ram" and plan.get("gpu_layers", 0):
        args.append("--no-kv-offload")
    return args


def _build_vllm_runtime_args(*, target) -> list[str] | None:
    if target.runtime != "vllm" or not target.serving_profile:
        return None
    entry = _selected_entry_snapshot(target)
    if entry is None or entry.get("id") != target.model:
        return None

    plan = normalize_serving_profile(target.serving_profile)
    hf = entry.get("hf") or {}
    artifact = entry.get("artifact") or {}
    args = [
        "--model", str(hf.get("repo_id") or ""),
        "--served-model-name", entry["id"],
        "--host", "0.0.0.0",
        "--port", "8080",
        "--max-model-len", str(plan["context_length"]),
        "--max-num-seqs", str(plan["max_num_seqs"]),
        "--max-num-batched-tokens", str(plan["max_num_batched_tokens"]),
        "--tensor-parallel-size", str(plan.get("tensor_parallel_size", 1)),
        "--gpu-memory-utilization", str(plan.get("gpu_memory_utilization", 0.9)),
    ]
    if artifact.get("format") in {"awq", "gptq"}:
        args.extend(["--quantization", artifact["format"]])
    return args


def _build_runtime_args(*, target, profile: ServerRuntimeProfile) -> list[str] | None:
    if target.runtime == "llama.cpp":
        return _build_llama_runtime_args(target=target, profile=profile)
    if target.runtime == "vllm":
        return _build_vllm_runtime_args(target=target)
    return None


def _build_runtime_payload(
    *, target, profile: ServerRuntimeProfile, catalog: list, generation_id: str = ""
) -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": {
            "endpoint_name": target.endpoint_name,
            "base_url": target.base_url,
            "model": target.model,
            "runtime": target.runtime,
            "generation_id": generation_id,
            "reason": target.reason,
            "fallback_used": target.fallback_used,
            "priority_preset": target.priority_preset,
            "selection_mode": target.selection_mode,
            "selection_reason_code": target.selection_reason_code,
            "runtime_policy_input": target.runtime_policy_input,
            "serving_profile": normalize_serving_profile(
                target.serving_profile
            ),
            "endpoint_status": asdict(target.endpoint_status)
            if target.endpoint_status
            else None,
            "health_status": asdict(target.health_status)
            if getattr(target, "health_status", None)
            else None,
            "smoke_status": asdict(target.smoke_status)
            if getattr(target, "smoke_status", None)
            else None,
        },
        "profile": asdict(profile),
        "catalog": [_serialize_catalog_entry(entry) for entry in catalog],
        "diagnostics": target.diagnostics or {},
    }


def write_runtime_generation(
    *,
    scope: str,
    target,
    profile: ServerRuntimeProfile,
    catalog: list,
    generation_id: str,
    repo_root: Path | None = None,
) -> Path:
    """Stage a candidate runtime config + args file under
    data/config/runtime_scopes/<scope>/generations/<generation_id>/ without
    touching the active llm_runtime.json pointer. The active pointer is only
    updated by commit_active_runtime_config, after health validation."""
    active_path = get_llm_runtime_config_path(scope, repo_root=repo_root)
    generation_dir = active_path.parent / "generations" / generation_id
    generation_dir.mkdir(parents=True, exist_ok=True)

    payload = _build_runtime_payload(
        target=target, profile=profile, catalog=catalog, generation_id=generation_id
    )
    (generation_dir / "runtime.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    args = _build_runtime_args(target=target, profile=profile)
    if args is not None:
        _write_runtime_args(generation_dir / "runtime.args", "\n".join(args) + "\n")

    return generation_dir


def commit_active_runtime_config(
    scope: str, generation_id: str, repo_root: Path | None = None
) -> Path:
    """Atomically point <scope>/llm_runtime.json at an already-validated
    generation's runtime.json (temp-file write + os.replace)."""
    active_path = get_llm_runtime_config_path(scope, repo_root=repo_root)
    generation_dir = active_path.parent / "generations" / generation_id
    payload_text = (generation_dir / "runtime.json").read_text(encoding="utf-8")

    active_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = active_path.with_suffix(".json.tmp")
    tmp_path.write_text(payload_text, encoding="utf-8")
    tmp_path.replace(active_path)
    return active_path


def _replace_runtime_arg(args: list[str], flag: str, value: int) -> None:
    try:
        flag_index = args.index(flag)
    except ValueError as exc:
        raise ValueError(f"Active runtime args do not contain {flag}") from exc
    value_index = flag_index + 1
    if value_index >= len(args):
        raise ValueError(f"Active runtime arg {flag} has no value")
    args[value_index] = str(value)


def stage_calibration_runtime_generation(
    *,
    scope: str,
    serving_concurrency: int,
    calibration_run_id: str,
    calibration_status: str,
    repo_root: Path | None = None,
) -> tuple[str, Path]:
    """Clone the active local runtime into a concurrency-specific generation.

    Calibration changes only the runtime scheduler/admission capacity. Model,
    quantization, context per request, and every other installation decision
    remain identical to the active generation.
    """
    if calibration_status not in CALIBRATION_STATUSES:
        raise ValueError(f"Unsupported calibration status: {calibration_status}")
    if isinstance(serving_concurrency, bool) or serving_concurrency < 1:
        raise ValueError("serving_concurrency must be a positive integer")

    active_path = get_llm_runtime_config_path(scope, repo_root=repo_root)
    try:
        payload = json.loads(active_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Active runtime config does not exist: {active_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Active runtime config is invalid JSON: {active_path}") from exc

    target = payload.get("target") if isinstance(payload, dict) else None
    if not isinstance(target, dict):
        raise ValueError("Active runtime config has no target")
    runtime = str(target.get("runtime") or "")
    if runtime not in LEGACY_ARGS_FILE:
        raise ValueError("Concurrency calibration requires a managed local runtime")
    source_generation_id = str(target.get("generation_id") or "")
    if not source_generation_id:
        raise ValueError("Active runtime is not a managed generation")

    serving_profile = normalize_serving_profile(target.get("serving_profile"))
    safe_ceiling = int(serving_profile.get("safe_concurrency_ceiling") or 1)
    if serving_concurrency > safe_ceiling:
        raise ValueError(
            f"serving_concurrency={serving_concurrency} exceeds "
            f"safe_concurrency_ceiling={safe_ceiling}"
        )
    context_length = int(serving_profile.get("context_length") or 0)
    if context_length < 1:
        raise ValueError("Active serving profile has no valid context_length")

    source_args_path = (
        active_path.parent
        / "generations"
        / source_generation_id
        / "runtime.args"
    )
    try:
        args = source_args_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"Active runtime args do not exist: {source_args_path}") from exc
    if not args:
        raise ValueError(f"Active runtime args are empty: {source_args_path}")

    if runtime == "llama.cpp":
        _replace_runtime_arg(args, "--parallel", serving_concurrency)
        _replace_runtime_arg(args, "--ctx-size", context_length * serving_concurrency)
    else:
        _replace_runtime_arg(args, "--max-num-seqs", serving_concurrency)
        _replace_runtime_arg(
            args,
            "--max-num-batched-tokens",
            context_length * serving_concurrency,
        )

    now = datetime.now(timezone.utc)
    safe_run_id = "".join(
        character for character in calibration_run_id if character.isalnum() or character in "-_"
    )[:48]
    if not safe_run_id:
        raise ValueError("calibration_run_id must contain a letter or number")
    status_suffix = "selected" if calibration_status == "calibrated" else "candidate"
    generation_id = (
        f"cal-{now.strftime('%Y%m%d%H%M%S%f')}-{safe_run_id}-{status_suffix}"
        f"-c{serving_concurrency}"
    )

    calibrated_profile = dict(serving_profile)
    original_generation_id = str(
        serving_profile.get("calibration_original_generation_id")
        or source_generation_id
    )
    calibrated_profile.update(
        {
            "serving_concurrency": serving_concurrency,
            "concurrency": serving_concurrency,
            "parallel": serving_concurrency,
            "max_num_seqs": serving_concurrency,
            "server_ctx_size": context_length * serving_concurrency,
            "max_num_batched_tokens": context_length * serving_concurrency,
            "calibration_status": calibration_status,
            "calibration_run_id": calibration_run_id,
            "calibration_source_generation_id": source_generation_id,
            "calibration_original_generation_id": original_generation_id,
            "calibration_updated_at": now.isoformat(),
        }
    )

    staged_payload = json.loads(json.dumps(payload))
    staged_payload["version"] = CONFIG_VERSION
    staged_payload["generated_at"] = now.isoformat()
    staged_target = staged_payload["target"]
    staged_target["generation_id"] = generation_id
    staged_target["serving_profile"] = calibrated_profile

    generation_dir = active_path.parent / "generations" / generation_id
    generation_dir.mkdir(parents=True, exist_ok=False)
    (generation_dir / "runtime.json").write_text(
        json.dumps(staged_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_runtime_args(generation_dir / "runtime.args", "\n".join(args) + "\n")
    return generation_id, generation_dir


def stage_legacy_runtime_generation(
    scope: str, *, repo_root: Path | None = None
) -> tuple[str, str, str] | None:
    """Convert a flat pre-generation config into a staged managed runtime.

    The active scoped pointer is left untouched until RuntimeLifecycleManager
    validates the container and commits this generation.
    """
    active_path = get_llm_runtime_config_path(scope, repo_root=repo_root)
    root = repo_root or Path(__file__).resolve().parents[2]
    legacy_path = root / "data" / "config" / "llm_runtime.json"

    source_path = active_path if active_path.is_file() else legacy_path
    if not source_path.is_file():
        return None

    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    target = payload.get("target") if isinstance(payload, dict) else None
    if not isinstance(target, dict):
        return None

    generation_id = str(target.get("generation_id") or "")
    if generation_id:
        generation_dir = active_path.parent / "generations" / generation_id
        if (generation_dir / "runtime.json").is_file() and (
            generation_dir / "runtime.args"
        ).is_file():
            return None

        # A scoped pointer without its generation artifacts cannot be
        # resumed. Fall back to the pre-generation files when they still
        # exist so an interrupted migration can be staged and validated
        # again instead of leaving RAG permanently disabled.
        if source_path == legacy_path or not legacy_path.is_file():
            return None
        source_path = legacy_path
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        target = payload.get("target") if isinstance(payload, dict) else None
        if not isinstance(target, dict) or target.get("generation_id"):
            return None

    runtime = str(target.get("runtime") or "")
    model_id = str(target.get("model") or "")
    args_filename = LEGACY_ARGS_FILE.get(runtime)
    if not runtime or not model_id or not args_filename:
        return None

    args_path = source_path.with_name(args_filename)
    try:
        args_text = args_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not args_text.strip():
        return None

    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\0"
        + args_text.encode("utf-8")
    ).hexdigest()
    generation_id = f"legacy-{fingerprint[:12]}"
    generation_dir = active_path.parent / "generations" / generation_id
    generation_dir.mkdir(parents=True, exist_ok=True)

    migrated_payload = json.loads(json.dumps(payload))
    migrated_target = migrated_payload["target"]
    migrated_target["generation_id"] = generation_id
    migrated_target["base_url"] = RUNTIME_BASE_URL
    (generation_dir / "runtime.json").write_text(
        json.dumps(migrated_payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    _write_runtime_args(generation_dir / "runtime.args", args_text)
    return runtime, model_id, generation_id


def write_llm_runtime_config(
    *,
    target,
    profile: ServerRuntimeProfile,
    catalog: list,
    path: Path | None = None,
) -> Path:
    """Write and immediately activate a runtime config at an explicit flat
    path, with no generation staging or health-gated commit. This is the
    direct-write path used by `detect_llm_runtime --write` (headless,
    operator-managed containers) and by tests that need a fixture at a known
    path; the interactive installation flow uses write_runtime_generation +
    commit_active_runtime_config instead, so a failed candidate never
    clobbers the active config."""
    config_path = path or get_llm_runtime_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    payload = _build_runtime_payload(target=target, profile=profile, catalog=catalog)
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    args = _build_runtime_args(target=target, profile=profile)
    if args is not None:
        args_filename = "llama_rag.args" if target.runtime == "llama.cpp" else "vllm_rag.args"
        _write_runtime_args(
            config_path.with_name(args_filename), "\n".join(args) + "\n"
        )
    return config_path
