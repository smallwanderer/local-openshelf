import json

import pytest

from document_ai.services.rag_runtime_config import (
    get_llm_runtime_config_path,
)
from llm_installation.catalog import get_catalog_entry
from llm_installation.config_store import (
    commit_active_runtime_config,
    stage_legacy_runtime_generation,
    write_runtime_generation,
)
from llm_installation.router import resolve_server_rag_target
from llm_installation.runtime_probe import ServerRuntimeProfile

pytestmark = pytest.mark.unit

GGUF_MODEL_ID = "qwen2.5-7b-instruct-q4_k_m"


def _profile():
    return ServerRuntimeProfile(
        cpu_count=8,
        ram_mb=32768,
        has_gpu=False,
        gpu_name="",
        gpu_vram_mb=0,
        gpu_vram_free_mb=0,
        gpu_count=0,
        gpu_vram_list=[],
        gpu_vram_free_list=[],
        cuda_available=False,
        platform="test",
        cluster_mode=False,
        disk_free_mb=102400,
    )


def _target(catalog):
    return resolve_server_rag_target(
        profile=_profile(), catalog=catalog, check_endpoint=False, priority_preset="balanced"
    )


def test_write_runtime_generation_does_not_touch_active_pointer(tmp_path):
    catalog = [get_catalog_entry(GGUF_MODEL_ID)]
    target = _target(catalog)
    active_path = get_llm_runtime_config_path("production", repo_root=tmp_path)

    generation_dir = write_runtime_generation(
        scope="production",
        target=target,
        profile=_profile(),
        catalog=catalog,
        generation_id="20240101-abc",
        repo_root=tmp_path,
    )

    assert (generation_dir / "runtime.json").exists()
    assert (generation_dir / "runtime.args").exists()
    assert not active_path.exists()


def test_commit_active_runtime_config_atomic_replace(tmp_path):
    catalog = [get_catalog_entry(GGUF_MODEL_ID)]
    target = _target(catalog)
    write_runtime_generation(
        scope="production",
        target=target,
        profile=_profile(),
        catalog=catalog,
        generation_id="20240101-abc",
        repo_root=tmp_path,
    )

    active_path = commit_active_runtime_config("production", "20240101-abc", repo_root=tmp_path)

    assert active_path.exists()
    payload = json.loads(active_path.read_text(encoding="utf-8"))
    assert payload["target"]["generation_id"] == "20240101-abc"
    assert payload["target"]["model"] == GGUF_MODEL_ID
    assert not active_path.with_suffix(".json.tmp").exists()


def test_commit_overwrites_previous_active_config(tmp_path):
    catalog = [get_catalog_entry(GGUF_MODEL_ID)]
    target = _target(catalog)
    for generation_id in ("gen-a", "gen-b"):
        write_runtime_generation(
            scope="production",
            target=target,
            profile=_profile(),
            catalog=catalog,
            generation_id=generation_id,
            repo_root=tmp_path,
        )

    commit_active_runtime_config("production", "gen-a", repo_root=tmp_path)
    active_path = commit_active_runtime_config("production", "gen-b", repo_root=tmp_path)

    payload = json.loads(active_path.read_text(encoding="utf-8"))
    assert payload["target"]["generation_id"] == "gen-b"


def test_stage_legacy_runtime_generation_for_managed_start(tmp_path):
    legacy_dir = tmp_path / "data" / "config"
    legacy_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "llm_runtime.json"
    legacy_path.write_text(
        json.dumps(
            {
                "version": 7,
                "target": {
                    "runtime": "llama.cpp",
                    "model": GGUF_MODEL_ID,
                    "base_url": "http://llama-rag:8080",
                },
            }
        ),
        encoding="utf-8",
    )
    (legacy_dir / "llama_rag.args").write_text(
        "--hf-repo test/model\n", encoding="utf-8"
    )

    migrated = stage_legacy_runtime_generation("production", repo_root=tmp_path)

    assert migrated is not None
    runtime, model_id, generation_id = migrated
    assert (runtime, model_id) == ("llama.cpp", GGUF_MODEL_ID)
    generation_dir = (
        tmp_path
        / "data"
        / "config"
        / "runtime_scopes"
        / "production"
        / "generations"
        / generation_id
    )
    payload = json.loads(
        (generation_dir / "runtime.json").read_text(encoding="utf-8")
    )
    assert payload["target"]["generation_id"] == generation_id
    assert payload["target"]["base_url"] == "http://rag-runtime:8080"
    assert (generation_dir / "runtime.args").read_text(encoding="utf-8") == (
        "--hf-repo test/model\n"
    )
    assert b"\r\n" not in (generation_dir / "runtime.args").read_bytes()
    assert not (
        tmp_path
        / "data"
        / "config"
        / "runtime_scopes"
        / "production"
        / "llm_runtime.json"
    ).exists()


def test_stage_legacy_repairs_scoped_pointer_with_missing_generation(tmp_path):
    legacy_dir = tmp_path / "data" / "config"
    scoped_dir = legacy_dir / "runtime_scopes" / "production"
    scoped_dir.mkdir(parents=True)
    legacy_payload = {
        "version": 7,
        "target": {
            "runtime": "llama.cpp",
            "model": GGUF_MODEL_ID,
            "base_url": "http://llama-rag:8080",
        },
    }
    (legacy_dir / "llm_runtime.json").write_text(
        json.dumps(legacy_payload), encoding="utf-8"
    )
    (legacy_dir / "llama_rag.args").write_text(
        "--hf-repo test/model\n", encoding="utf-8"
    )
    (scoped_dir / "llm_runtime.json").write_text(
        json.dumps(
            {
                **legacy_payload,
                "target": {
                    **legacy_payload["target"],
                    "generation_id": "missing-generation",
                    "base_url": "http://rag-runtime:8080",
                },
            }
        ),
        encoding="utf-8",
    )

    migrated = stage_legacy_runtime_generation("production", repo_root=tmp_path)

    assert migrated is not None
    runtime, model_id, generation_id = migrated
    assert (runtime, model_id) == ("llama.cpp", GGUF_MODEL_ID)
    generation_dir = scoped_dir / "generations" / generation_id
    assert (generation_dir / "runtime.json").is_file()
    assert (generation_dir / "runtime.args").is_file()
    assert json.loads((scoped_dir / "llm_runtime.json").read_text())["target"][
        "generation_id"
    ] == "missing-generation"


def test_get_llm_runtime_config_path_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_RUNTIME_CONFIG_PATH", raising=False)
    monkeypatch.delenv("RUNTIME_SCOPE", raising=False)

    default_path = get_llm_runtime_config_path(repo_root=tmp_path)
    assert "production" in str(default_path)

    monkeypatch.setenv("RUNTIME_SCOPE", "isolated-test")
    assert "production" in str(get_llm_runtime_config_path("production", repo_root=tmp_path))
    assert "isolated-test" in str(get_llm_runtime_config_path(repo_root=tmp_path))

    override = tmp_path / "custom.json"
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_PATH", str(override))
    assert get_llm_runtime_config_path("production", repo_root=tmp_path) == override
