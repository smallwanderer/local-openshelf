import json
from dataclasses import replace

import pytest

from installation.deployment import (
    ALL_WORKER_SERVICES,
    CORE_SERVICES,
    DeploymentPlan,
    DOCUMENT_QUEUE_SERVICES,
    DOCUMENT_RUNTIME_RESTART_SERVICES,
    WorkerSpec,
    build_deployment_plan,
    compose_up_command,
    embedding_runtime_change_command,
    read_deployment_plan,
    worker_services_for_lifecycle,
    write_deployment_plan,
)
from llm_installation.runtime_lifecycle import build_runtime_spec


pytestmark = pytest.mark.unit


def test_fast_start_command_explicitly_disables_builds():
    command = compose_up_command(
        "docker compose -f docker-compose.yml",
        ["db", "redis", "app"],
        build_images=False,
    )

    assert command == (
        "docker compose -f docker-compose.yml up --no-build --remove-orphans -d db redis app"
    )


def test_maintenance_rebuild_command_builds_and_recreates():
    command = compose_up_command(
        "docker compose -f docker-compose.yml",
        ["db", "redis", "app"],
        build_images=True,
        force_recreate=True,
    )

    assert command == (
        "docker compose -f docker-compose.yml up --build --remove-orphans --force-recreate "
        "-d db redis app"
    )


def test_embedding_runtime_change_uses_one_off_model_owner_with_writable_config():
    command = embedding_runtime_change_command(
        "docker compose -f docker-compose.yml",
        scope="production",
        priority_preset="quality",
    )

    assert command == (
        "docker compose -f docker-compose.yml run --rm "
        "-e DOTORI_EMBEDDING_MODEL_PROCESS=1 "
        "-v ./data/config:/data/config "
        "-v ./app:/app "
        "embedding-executor python manage.py change_embedding_runtime "
        "--scope production --preset quality --activate"
    )

def _runtime(tmp_path, scope="production"):
    return build_runtime_spec(
        scope,
        "llama.cpp",
        "test-model",
        "runtime-gen-1",
        repo_root=tmp_path,
    )


@pytest.mark.parametrize(
    "mode,enabled,disabled",
    [
        (
            "basic",
            (),
            ALL_WORKER_SERVICES,
        ),
        (
            "search",
            ALL_WORKER_SERVICES,
            (),
        ),
    ],
)
def test_plan_selects_workers_for_non_rag_modes(mode, enabled, disabled):
    plan = build_deployment_plan(mode)

    assert plan.core_services == CORE_SERVICES
    assert tuple(worker.compose_service for worker in plan.enabled_workers) == enabled
    assert plan.disabled_worker_services == disabled
    assert plan.runtime is None


def test_rag_plan_keeps_the_complete_document_processing_topology(tmp_path):
    pending = build_deployment_plan("1")
    active = build_deployment_plan("rag", runtime=_runtime(tmp_path))

    assert pending.enabled_services == (*CORE_SERVICES, *ALL_WORKER_SERVICES)
    assert active.enabled_services == (*CORE_SERVICES, *ALL_WORKER_SERVICES)
    assert active.runtime is not None
    assert active.runtime.scope == active.scope


def test_document_processing_specs_match_compose_roles():
    plan = build_deployment_plan("search")
    specs = {worker.compose_service: worker for worker in plan.workers}

    assert tuple(specs) == ALL_WORKER_SERVICES
    assert specs["embedding-executor"].queues == ()
    assert specs["embedding-executor"].health_strategy == "http-readyz"
    assert specs["parser-executor"].queues == ()
    assert specs["parser-executor"].health_strategy == "http-readyz"
    assert specs["dotori-orchestrator"].queues == ("parse", "embed")
    assert specs["dotori-orchestrator"].health_strategy == "celery-ping"


def test_document_runtime_lifecycle_constants_cover_all_services():
    assert DOCUMENT_QUEUE_SERVICES == ("dotori-orchestrator",)
    assert DOCUMENT_RUNTIME_RESTART_SERVICES == ("app", *ALL_WORKER_SERVICES)


def test_old_saved_plan_cannot_omit_current_workers_from_lifecycle():
    old_plan = {
        "workers": [
            {
                "compose_service": "dotori-document",
                "enabled": True,
            }
        ]
    }

    assert worker_services_for_lifecycle(old_plan) == ALL_WORKER_SERVICES


def test_lifecycle_ignores_removed_legacy_service_names():
    saved_plan = {
        "workers": [
            {"compose_service": "legacy-document-worker", "enabled": True},
            {"compose_service": "disabled-worker", "enabled": False},
        ]
    }

    assert worker_services_for_lifecycle(saved_plan) == ALL_WORKER_SERVICES


def test_plan_rejects_runtime_from_another_scope(tmp_path):
    runtime = replace(_runtime(tmp_path), scope="other")

    with pytest.raises(ValueError, match="scope"):
        build_deployment_plan("rag", scope="production", runtime=runtime)


def test_plan_rejects_runtime_in_search_mode(tmp_path):
    with pytest.raises(ValueError, match="Only RAG mode"):
        build_deployment_plan("search", runtime=_runtime(tmp_path))


def test_runtime_dependent_worker_cannot_be_enabled_without_runtime():
    rag_worker = WorkerSpec(
        name="rag",
        compose_service="runtime-worker",
        queues=("rag",),
        concurrency=1,
        prefetch_multiplier=1,
        enabled=True,
        requires_runtime=True,
        dependencies=("rag-runtime",),
        health_strategy="celery-ping",
    )

    with pytest.raises(ValueError, match="requires a RuntimeSpec"):
        DeploymentPlan(
            scope="production",
            mode="rag",
            core_services=CORE_SERVICES,
            workers=(rag_worker,),
            runtime=None,
            network_access="local",
            generation_id="plan-test",
        )


def test_plan_write_is_readable_and_stable(tmp_path):
    plan = build_deployment_plan("rag", runtime=_runtime(tmp_path))

    path = write_deployment_plan(plan, repo_root=tmp_path)
    first_payload = json.loads(path.read_text(encoding="utf-8"))
    write_deployment_plan(plan, repo_root=tmp_path)

    assert read_deployment_plan("production", repo_root=tmp_path) == first_payload
    assert first_payload["generation_id"] == plan.generation_id
    assert first_payload["runtime"]["args_file"].endswith("runtime.args")
