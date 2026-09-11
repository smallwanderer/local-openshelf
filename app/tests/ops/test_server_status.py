import io
import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from document_ai.management.commands.server_status import (
    HEALTHCHECK_FILE_PREFIX,
    HEALTHCHECK_OWNER_EMAIL,
    _embedding_status,
    _rag_status,
    check_file_io_pipeline,
)
from llm_installation.catalog import get_catalog_entry
from llm_installation.config_store import write_llm_runtime_config
from llm_installation.runtime_probe import EndpointStatus, ServerRuntimeProfile
from llm_installation.router import resolve_server_rag_target
from files.models import Node
from files.services import storage as storage_service

pytestmark = pytest.mark.unit

User = get_user_model()

GGUF_MODEL_ID = "qwen2.5-7b-instruct-q4_k_m"


class FileIOPipelineCheckTests(TestCase):
    def test_happy_path_cleans_up_after_itself(self):
        result = check_file_io_pipeline()

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "ok")
        self.assertFalse(
            Node.objects.filter(name__startswith=HEALTHCHECK_FILE_PREFIX).exists()
        )
        owner = User.objects.get(email=HEALTHCHECK_OWNER_EMAIL)
        self.assertFalse(owner.is_active)
        self.assertFalse(owner.has_usable_password())

    def test_idempotent_across_repeated_runs(self):
        check_file_io_pipeline()
        check_file_io_pipeline()

        self.assertEqual(
            User.objects.filter(email=HEALTHCHECK_OWNER_EMAIL).count(), 1
        )
        self.assertFalse(
            Node.objects.filter(name__startswith=HEALTHCHECK_FILE_PREFIX).exists()
        )

    def test_sweeps_orphaned_file_from_previous_crashed_run(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        owner = User.objects.create(email=HEALTHCHECK_OWNER_EMAIL, is_active=False)
        workspace = owner.workspace_memberships.get(workspace__kind="personal").workspace
        stale_upload = SimpleUploadedFile(
            f"{HEALTHCHECK_FILE_PREFIX}stale.txt", b"leftover", content_type="text/plain"
        )
        storage_service.save_file(
            workspace=workspace, owner=owner, file=stale_upload, description="", ai_processing_enabled=False
        )
        self.assertTrue(
            Node.objects.filter(name__startswith=HEALTHCHECK_FILE_PREFIX).exists()
        )

        check_file_io_pipeline()

        self.assertFalse(
            Node.objects.filter(name__startswith=HEALTHCHECK_FILE_PREFIX).exists()
        )

    def test_failure_path_still_cleans_up_node(self):
        original_open_file = storage_service.open_file

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated read failure")

        storage_service.open_file = _boom
        try:
            result = check_file_io_pipeline()
        finally:
            storage_service.open_file = original_open_file

        self.assertFalse(result["ok"])
        self.assertIn("simulated read failure", result["message"])
        self.assertFalse(
            Node.objects.filter(name__startswith=HEALTHCHECK_FILE_PREFIX).exists()
        )


@pytest.mark.parametrize(
    "operation_mode,expected_enabled",
    [
        ("rag", True),
        ("search", True),
        ("basic", False),
    ],
)
def test_embedding_status_enabled_matches_operation_mode(
    monkeypatch, operation_mode, expected_enabled
):
    monkeypatch.setenv("DOTORI_OPERATION_MODE", operation_mode)

    status = _embedding_status()

    assert status["enabled"] is expected_enabled
    assert status["model"]


@pytest.mark.parametrize("query_llm_enabled_env", ["1", None, "0", "false"])
def test_rag_status_enabled_ignores_query_llm_enabled_env(monkeypatch, tmp_path, query_llm_enabled_env):
    # QUERY_UNDERSTANDING_LLM_ENABLED gates the unrelated (abandoned) query-understanding
    # LLM step, not RAG generation — it must have no effect on rag_status.
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_PATH", str(tmp_path / "missing.json"))
    if query_llm_enabled_env is None:
        monkeypatch.delenv("QUERY_UNDERSTANDING_LLM_ENABLED", raising=False)
    else:
        monkeypatch.setenv("QUERY_UNDERSTANDING_LLM_ENABLED", query_llm_enabled_env)

    status = _rag_status(timeout=1)

    assert status["enabled"] is False
    assert status["configured"] is False


def test_rag_status_probes_healthy_persisted_config(monkeypatch, tmp_path):
    profile = ServerRuntimeProfile(
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
    catalog = [get_catalog_entry(GGUF_MODEL_ID)]
    target = resolve_server_rag_target(
        profile=profile,
        catalog=catalog,
        check_endpoint=False,
        priority_preset="balanced",
    )
    config_path = tmp_path / "llm_runtime.json"
    write_llm_runtime_config(target=target, profile=profile, catalog=catalog, path=config_path)
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_PATH", str(config_path))
    # Set even though it should no longer matter, to guard against the old bug
    # where this unrelated flag suppressed the health probe.
    monkeypatch.setenv("QUERY_UNDERSTANDING_LLM_ENABLED", "0")
    monkeypatch.setattr(
        "document_ai.management.commands.server_status.check_endpoint_health",
        lambda base_url, timeout: EndpointStatus(base_url=base_url, ok=True, check_name="health"),
    )
    monkeypatch.setattr(
        "document_ai.management.commands.server_status.check_openai_compatible_endpoint",
        lambda base_url, expected_model, timeout: EndpointStatus(base_url=base_url, ok=True, check_name="models"),
    )

    status = _rag_status(timeout=1)

    assert status["enabled"] is True
    assert status["configured"] is True
    assert status["runtime"] == target.runtime
    assert status["health_status"]["ok"] is True


def test_rag_status_reports_persisted_oom_without_live_probe(monkeypatch, tmp_path):
    config_path = tmp_path / "llm_runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "target": {
                    "endpoint_name": "Local runtime",
                    "base_url": "http://rag-runtime:8080",
                    "model": "test-model",
                    "runtime": "llama.cpp",
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "runtime_status.json").write_text(
        json.dumps(
            {
                "status": "unavailable",
                "reason_code": "LLM_UNAVAILABLE_OOM",
                "retryable": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(
        "document_ai.management.commands.server_status.check_endpoint_health",
        lambda *a, **k: pytest.fail("must not probe an unavailable runtime"),
    )

    status = _rag_status(timeout=1)

    assert status["configured"] is True
    assert status["available"] is False
    assert status["runtime_status"]["reason_code"] == "LLM_UNAVAILABLE_OOM"


class ServerStatusCommandTests(TestCase):
    def test_json_output_contains_expected_top_level_keys(self):
        out = io.StringIO()
        call_command("server_status", "--json-output", "--skip-file-io", stdout=out)
        payload = json.loads(out.getvalue())

        assert set(payload.keys()) == {"connection", "features", "memory_reservations"}
        assert set(payload["features"].keys()) == {"file_io", "embedding", "rag"}
        assert payload["features"]["file_io"]["pipeline_check"] is None

    def test_json_output_reports_memory_reservations(self):
        out = io.StringIO()
        call_command("server_status", "--json-output", "--skip-file-io", stdout=out)
        reservations = json.loads(out.getvalue())["memory_reservations"]

        assert set(reservations.keys()) == {"scope", "claims", "total"}
        # The total is derived from the claims on every read, so it must agree
        # with them even when nothing has been claimed yet.
        assert reservations["total"]["ram_mb"] == sum(
            claim["ram_mb"] for claim in reservations["claims"].values()
        )
        assert set(reservations["total"]["workloads"]) == set(reservations["claims"])
