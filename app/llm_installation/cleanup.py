from __future__ import annotations

from pathlib import Path
from typing import Any

from document_ai.services.rag_runtime_config import load_llm_runtime_config
from llm_installation.memory_reservations import CLAIM_LLM, clear_claim
from llm_installation.runtime_lifecycle import RuntimeLifecycleManager, get_repo_root

RUNTIME_CACHE_SUBPATH = {
    "llama.cpp": ("data", "cache", "huggingface", "hub"),
    "vllm": ("data", "cache", "huggingface", "hub"),
}


def get_runtime_cache_dir(runtime: str, repo_root: Path | None = None) -> Path | None:
    parts = RUNTIME_CACHE_SUBPATH.get(runtime)
    if parts is None:
        return None
    return (repo_root or get_repo_root()).joinpath(*parts)


def extract_runtime_and_repo(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Pull (runtime, hf_repo_id) out of a loaded llm_runtime.json payload."""
    target = payload.get("target") if isinstance(payload, dict) else None
    if not isinstance(target, dict):
        return None
    runtime = target.get("runtime")
    handoff = target.get("runtime_policy_input")
    if not isinstance(handoff, dict):
        return None
    assessment = handoff.get("catalog_assessment")
    if not isinstance(assessment, dict):
        return None
    entry = assessment.get("catalog_entry")
    if not isinstance(entry, dict):
        return None
    repo_id = (entry.get("hf") or {}).get("repo_id")
    if not runtime or not repo_id:
        return None
    return str(runtime), str(repo_id)


def hf_cache_dirname(repo_id: str) -> str:
    return "models--" + repo_id.replace("/", "--")


def remove_model_weights(
    runtime: str, repo_id: str, repo_root: Path | None = None
) -> tuple[bool, str]:
    cache_dir = get_runtime_cache_dir(runtime, repo_root)
    if cache_dir is None:
        return False, f"Unknown runtime '{runtime}'; skipped weight cleanup."
    model_dir = cache_dir / hf_cache_dirname(repo_id)
    if not model_dir.exists():
        return False, f"No cached weights found for {repo_id} at {model_dir}."
    try:
        import shutil

        shutil.rmtree(model_dir)
    except OSError as exc:
        return False, (
            f"Could not remove cached weights for {repo_id} at {model_dir}: {exc}. "
            "The runtime container may still be holding a lock on these files; "
            "stop it and try again, or delete the folder manually."
        )
    return True, f"Removed cached weights for {repo_id}: {model_dir}"


def remove_current_llm_runtime(
    *,
    scope: str = "production",
    remove_weights: bool = True,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Stop+remove the scope's managed runtime container, delete its cached
    weights, and clear the scope's runtime config tree (active pointer + all
    generations)."""
    root = repo_root or get_repo_root()
    payload = load_llm_runtime_config(scope=scope, repo_root=root)
    messages: list[str] = []
    info = extract_runtime_and_repo(payload) if payload else None

    manager = RuntimeLifecycleManager(root)
    removed = manager.remove(scope)
    messages.append(
        "Removed managed runtime container and config."
        if removed
        else "Could not remove managed runtime container (already stopped, not found, or not owned by Dotori)."
    )

    if info and remove_weights:
        runtime, repo_id = info
        _, message = remove_model_weights(runtime, repo_id, root)
        messages.append(message)
    elif not info:
        messages.append("No resolvable runtime/model in the current config; skipped weight cleanup.")

    # Removal is the only event that releases the memory claim -- a stopped or
    # unhealthy runtime keeps its hold, because it is expected to start again.
    if clear_claim(CLAIM_LLM, scope=scope, repo_root=root) is not None:
        messages.append("Released the LLM memory reservation.")

    return {
        "had_config": bool(payload),
        "messages": messages,
    }


def cleanup_stale_runtime(
    previous_payload: dict[str, Any],
    new_runtime: str,
    new_repo_id: str,
    *,
    scope: str = "production",
    remove_weights: bool = True,
    repo_root: Path | None = None,
) -> list[str]:
    """After switching to a new runtime/model, remove leftover cached weights
    from the previous selection. Container teardown itself is handled by
    RuntimeLifecycleManager.apply() as part of activating the new runtime."""
    messages: list[str] = []
    info = extract_runtime_and_repo(previous_payload) if previous_payload else None
    if info is None:
        return messages

    prev_runtime, prev_repo_id = info
    if prev_runtime == new_runtime and prev_repo_id == new_repo_id:
        return messages

    if remove_weights and prev_repo_id != new_repo_id:
        _, message = remove_model_weights(prev_runtime, prev_repo_id, repo_root)
        messages.append(message)

    return messages
