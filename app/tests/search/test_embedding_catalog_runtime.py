import json
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from document_ai.services.embedding_runtime_config import (
    EmbeddingRuntimeConfigError,
    load_embedding_runtime,
)
from llm_installation.embedding_catalog import (
    get_embedding_catalog_entry_for_preset,
    get_supported_embedding_catalog,
)
from llm_installation.embedding_config_store import (
    commit_active_embedding_runtime,
    write_embedding_runtime_generation,
)
from document_ai.models import EmbeddingGeneration
from document_ai.management.commands import rollback_embedding_runtime


pytestmark = pytest.mark.unit


def test_supported_profiles_include_verified_and_expanded_models():
    supported = get_supported_embedding_catalog()
    supported_ids = [entry.id for entry in supported]

    assert "bge-m3-hybrid" in supported_ids
    assert "harrier-270m" in supported_ids
    assert "granite-278m" in supported_ids
    assert "gte-qwen2-1.5b" in supported_ids
    assert "openai-text-embedding-3-small" in supported_ids

    bge_entry = next(entry for entry in supported if entry.id == "bge-m3-hybrid")
    assert bge_entry.repo_id == "BAAI/bge-m3"
    assert bge_entry.revision == "5617a9f"
    assert bge_entry.provider == "bgem3_hybrid"
    assert bge_entry.store == "pgvector_chunk_1024"
    assert bge_entry.dimension == 1024
    assert bge_entry.supports_sparse is True

    harrier_entry = next(entry for entry in supported if entry.id == "harrier-270m")
    assert harrier_entry.dimension == 640
    assert harrier_entry.store == "pgvector_chunk_640"
    assert harrier_entry.provider == "sentence_transformers"
    assert "ko" in harrier_entry.languages


@pytest.mark.parametrize("preset", ["speed", "balanced", "quality"])
def test_catalog_resolves_server_preset(preset):
    entry = get_embedding_catalog_entry_for_preset(preset)

    assert entry.id == "bge-m3-hybrid"


def test_embedding_runtime_generation_is_atomic_and_readable(tmp_path):
    entry = get_embedding_catalog_entry_for_preset("balanced")
    generation_dir = write_embedding_runtime_generation(
        scope="production",
        generation_id="embedding-test",
        entry=entry,
        repo_root=tmp_path,
    )

    active_path = (
        tmp_path
        / "data"
        / "config"
        / "runtime_scopes"
        / "production"
        / "embedding_runtime.json"
    )
    assert (generation_dir / "runtime.json").exists()
    assert not active_path.exists()

    commit_active_embedding_runtime(
        "production",
        "embedding-test",
        repo_root=tmp_path,
    )
    runtime = load_embedding_runtime(
        path=active_path,
        scope="production",
        repo_root=tmp_path,
    )

    assert runtime.generation_id == "embedding-test"
    assert runtime.model_id == "BAAI/bge-m3"
    assert runtime.runtime_fingerprint
    assert not active_path.with_suffix(".json.tmp").exists()


def test_existing_invalid_runtime_fails_closed(tmp_path):
    path = tmp_path / "embedding_runtime.json"
    path.write_text(json.dumps({"model_id": "broken"}), encoding="utf-8")

    with pytest.raises(EmbeddingRuntimeConfigError):
        load_embedding_runtime(path=path, scope="production")


def test_missing_runtime_ignores_legacy_embedding_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "operator/model")
    monkeypatch.setenv("EMBEDDING_BACKEND", "operator-backend")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "384")

    runtime = load_embedding_runtime(
        path=tmp_path / "missing.json",
        scope="production",
    )

    assert runtime.model_id == "BAAI/bge-m3"
    assert runtime.provider == "bgem3_hybrid"
    assert runtime.dimension == 1024


@pytest.mark.django_db
def test_migration_seeds_legacy_embedding_generation():
    generation = EmbeddingGeneration.objects.get(
        generation_id="legacy-bge-m3"
    )

    assert generation.model_id == "BAAI/bge-m3"
    assert generation.provider == "bgem3_hybrid"
    assert generation.dimension == 1024


@pytest.mark.django_db
def test_rollback_requires_reembedding_or_database_restore(
    tmp_path,
    monkeypatch,
):
    entry = get_embedding_catalog_entry_for_preset("balanced")
    for generation_id in ("gen-old", "gen-new"):
        write_embedding_runtime_generation(
            scope="production",
            generation_id=generation_id,
            entry=entry,
            repo_root=tmp_path,
        )
    active_path = commit_active_embedding_runtime(
        "production",
        "gen-new",
        repo_root=tmp_path,
    )
    old = EmbeddingGeneration.objects.create(
        generation_id="gen-old",
        scope="production",
        model_id=entry.repo_id,
        model_revision=entry.revision,
        provider=entry.provider,
        store=entry.store,
        dimension=entry.dimension,
        supports_sparse=entry.supports_sparse,
        status="RETIRED",
    )
    current = EmbeddingGeneration.objects.create(
        generation_id="gen-new",
        scope="production",
        model_id=entry.repo_id,
        model_revision=entry.revision,
        provider=entry.provider,
        store=entry.store,
        dimension=entry.dimension,
        supports_sparse=entry.supports_sparse,
        status="ACTIVE",
    )
    with pytest.raises(CommandError, match="cannot be activated"):
        call_command(
            "rollback_embedding_runtime",
            scope="production",
            stdout=StringIO(),
        )

    old.refresh_from_db()
    current.refresh_from_db()
    assert old.status == "RETIRED"
    assert current.status == "ACTIVE"
    assert load_embedding_runtime(
        path=active_path,
        scope="production",
    ).generation_id == "gen-new"


def test_embedding_runtime_generation_includes_max_tokens(tmp_path):
    entry = get_embedding_catalog_entry_for_preset("balanced")
    generation_dir = write_embedding_runtime_generation(
        scope="production",
        generation_id="embedding-test-tokens",
        entry=entry,
        repo_root=tmp_path,
    )
    payload = json.loads((generation_dir / "runtime.json").read_text(encoding="utf-8"))
    assert "max_tokens" in payload
    assert payload["max_tokens"] == entry.model_input_max_tokens


def test_legacy_fingerprint_compatibility(tmp_path):
    entry = get_embedding_catalog_entry_for_preset("balanced")
    from llm_installation.embedding_config_store import build_embedding_runtime_payload
    import hashlib

    payload = build_embedding_runtime_payload(
        scope="production",
        generation_id="legacy-v1",
        entry=entry,
    )
    # Strip max_tokens and sign as v1 legacy
    payload.pop("max_tokens", None)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["runtime_fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    path = tmp_path / "legacy_embedding_runtime.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    runtime = load_embedding_runtime(path=path, scope="production")
    assert runtime.max_tokens == 8192
    assert runtime.generation_id == "legacy-v1"


def test_dynamic_parser_config_resolution(monkeypatch):
    from document_ai.parsers.config import (
        get_embedding_max_tokens,
        get_chunk_max_tokens,
        get_parser_tokenizer_id,
        get_parser_tokenizer_revision,
        get_embedding_token_headroom,
        clear_parser_tokenizer_cache,
    )
    from document_ai.services.embedding_runtime_config import EmbeddingRuntimeSnapshot

    fake_runtime = EmbeddingRuntimeSnapshot(
        scope="production",
        catalog_id="test-cat",
        catalog_revision="v1",
        generation_id="test-gen",
        model_id="custom/embed-model",
        model_revision="main",
        tokenizer_id="custom/tokenizer-id",
        tokenizer_revision="main",
        provider="custom_provider",
        store="pgvector_chunk_512",
        dimension=512,
        max_tokens=512,
        supports_sparse=False,
        distance_strategy="cosine",
    )

    import document_ai.parsers.config as p_config
    monkeypatch.setattr(
        "document_ai.services.embedding_runtime_config.get_active_embedding_runtime",
        lambda *args, **kwargs: fake_runtime,
    )
    clear_parser_tokenizer_cache()

    assert get_parser_tokenizer_id() == "custom/tokenizer-id"
    assert get_parser_tokenizer_revision() is None
    assert get_embedding_max_tokens() == 512
    headroom = get_embedding_token_headroom()
    assert get_chunk_max_tokens() == 512 - headroom
