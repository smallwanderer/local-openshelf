import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from document_ai.models import DocumentChunk, DocumentParseResult, EmbeddingGeneration
from document_ai.services.embedding_runtime_config import load_embedding_runtime
from llm_installation.embedding_catalog import get_embedding_catalog_entry_for_preset
from llm_installation.embedding_config_store import (
    commit_active_embedding_runtime,
    write_embedding_runtime_generation,
)
from llm_installation.embedding_probe import (
    build_external_embedding_entry,
    stage_external_embedding_generation,
)

pytestmark = pytest.mark.django_db


def test_change_embedding_runtime_check_chunks_empty():
    out = StringIO()
    call_command("change_embedding_runtime", "--check-chunks", stdout=out)
    data = json.loads(out.getvalue().strip())
    assert "documents" in data
    assert "chunks" in data
    assert isinstance(data["documents"], int)
    assert isinstance(data["chunks"], int)


def test_change_embedding_runtime_with_candidate_generation_and_skip_reembed(
    tmp_path, monkeypatch
):
    entry = get_embedding_catalog_entry_for_preset("balanced")
    write_embedding_runtime_generation(
        scope="production",
        generation_id="initial-gen",
        entry=entry,
        repo_root=tmp_path,
    )
    commit_active_embedding_runtime("production", "initial-gen", repo_root=tmp_path)

    config_target = (
        tmp_path
        / "data"
        / "config"
        / "runtime_scopes"
        / "production"
        / "embedding_runtime.json"
    )

    from llm_installation import embedding_config_store

    monkeypatch.setattr(
        embedding_config_store,
        "get_embedding_runtime_config_path",
        lambda scope, repo_root=None: config_target,
    )
    from document_ai.services import embedding_runtime_config

    monkeypatch.setattr(
        embedding_runtime_config,
        "get_embedding_runtime_config_path",
        lambda scope=None, repo_root=None: config_target,
    )
    from document_ai.management.commands import change_embedding_runtime

    monkeypatch.setattr(
        change_embedding_runtime,
        "get_embedding_runtime_config_path",
        lambda scope, repo_root=None: config_target,
    )

    ext_entry = build_external_embedding_entry(
        model_name="text-embedding-3-small",
        dimension=1536,
        store="pgvector_chunk_1536",
    )
    candidate_id, _ = stage_external_embedding_generation(
        "production",
        ext_entry,
        repo_root=tmp_path,
    )

    mock_provider = MagicMock()
    mock_provider.embed_query.return_value = MagicMock()
    mock_store = MagicMock()

    with patch(
        "document_ai.management.commands.change_embedding_runtime.get_embedding_provider",
        return_value=mock_provider,
    ), patch(
        "document_ai.management.commands.change_embedding_runtime.get_embedding_store_instance",
        return_value=mock_store,
    ), patch(
        "document_ai.management.commands.change_embedding_runtime.replace_active_hnsw_index"
    ) as mock_hnsw:
        out = StringIO()
        call_command(
            "change_embedding_runtime",
            "--candidate-generation-id",
            candidate_id,
            "--skip-reembed",
            stdout=out,
        )

    output = out.getvalue()
    assert "Activated embedding generation without re-embedding" in output
    active = load_embedding_runtime(scope="production", repo_root=tmp_path)
    assert active.generation_id == candidate_id
    assert active.dimension == 1536
    assert active.provider == "openai_compatible"
    mock_hnsw.assert_called_once_with(dimension=1536)
