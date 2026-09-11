from __future__ import annotations

import os
import time
import uuid

from dataclasses import asdict

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand
from django.db import connection

from document_ai.services.rag_runtime_config import (
    load_llm_runtime_status,
    target_from_persisted_config,
)
from document_ai.services.embedding_runtime_config import (
    EmbeddingRuntimeConfigError,
    load_embedding_runtime,
)
from llm_installation.cli import json_output
from llm_installation.runtime_probe import (
    check_endpoint_health,
    check_openai_compatible_endpoint,
)
from files.models import Node
from files.services import storage as storage_service

HEALTHCHECK_OWNER_EMAIL = "server-status-healthcheck@dotori.local"
HEALTHCHECK_FILE_PREFIX = "__server_status_check__"
HEALTHCHECK_PAYLOAD = b"dotori-server-status-check"


def _get_or_create_healthcheck_owner():
    User = get_user_model()
    user, created = User.objects.get_or_create(
        email=HEALTHCHECK_OWNER_EMAIL,
        defaults={"is_active": False, "email_verified": True},
    )
    if created or user.has_usable_password():
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user


def check_file_io_pipeline() -> dict:
    """Exercise the real save_file -> open_file -> delete_file pipeline and clean up after itself."""
    started = time.monotonic()
    owner = _get_or_create_healthcheck_owner()
    workspace = owner.workspace_memberships.get(workspace__kind="personal").workspace

    for stale in Node.objects.filter(owner=owner, name__startswith=HEALTHCHECK_FILE_PREFIX):
        try:
            storage_service.delete_file(stale)
        except Exception:
            pass

    name = f"{HEALTHCHECK_FILE_PREFIX}{uuid.uuid4().hex}.txt"
    upload = SimpleUploadedFile(name, HEALTHCHECK_PAYLOAD, content_type="text/plain")
    node = None
    try:
        node = storage_service.save_file(
            workspace=workspace,
            owner=owner,
            file=upload,
            description="server_status healthcheck",
            ai_processing_enabled=False,
        )
        with storage_service.open_file(node) as handle:
            read_back = handle.read()
        ok = read_back == HEALTHCHECK_PAYLOAD
        message = "ok" if ok else "Content mismatch on read-back."
    except Exception as exc:
        ok = False
        message = str(exc)[:240]
    finally:
        if node is not None:
            try:
                storage_service.delete_file(node)
            except Exception:
                pass

    return {"ok": ok, "message": message, "elapsed_ms": int((time.monotonic() - started) * 1000)}


def _connection_status() -> dict:
    started = time.monotonic()
    try:
        connection.ensure_connection()
        db_ok, message = True, "ok"
    except Exception as exc:
        db_ok, message = False, str(exc)[:240]
    return {
        "app_reachable": True,
        "db_ok": db_ok,
        "message": message,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "allowed_hosts": list(settings.ALLOWED_HOSTS),
        "debug": settings.DEBUG,
    }


def _file_io_status(*, skip: bool) -> dict:
    return {
        "enabled": True,
        "pipeline_check": None if skip else check_file_io_pipeline(),
    }


def _embedding_status() -> dict:
    operation_mode = os.environ.get("DOTORI_OPERATION_MODE", "search").strip().lower()
    enabled = operation_mode in {"rag", "search"}
    try:
        runtime = load_embedding_runtime()
    except EmbeddingRuntimeConfigError as exc:
        return {
            "enabled": enabled,
            "configured": False,
            "available": False,
            "message": str(exc),
        }
    return {
        "enabled": enabled,
        "configured": True,
        "available": enabled,
        "catalog_id": runtime.catalog_id,
        "generation_id": runtime.generation_id,
        "runtime_fingerprint": runtime.runtime_fingerprint,
        "model": runtime.model_id,
        "model_revision": runtime.model_revision,
        "backend": runtime.provider,
        "dimension": runtime.dimension,
        "sparse_enabled": runtime.supports_sparse,
        "store": runtime.store,
        "distance_strategy": runtime.distance_strategy,
    }


def _memory_reservations_status() -> dict:
    """Report what each workload currently claims, plus the sum.

    The total is computed here rather than read from disk: it must never be
    able to disagree with the claims it came from.
    """
    from llm_installation.memory_reservations import (
        get_total_reservation,
        read_reservations,
    )

    scope = os.getenv("RUNTIME_SCOPE", "production")
    claims = read_reservations(scope)
    total = get_total_reservation(scope)
    return {
        "scope": scope,
        "claims": {
            workload: claim.as_dict() for workload, claim in sorted(claims.items())
        },
        "total": total.as_dict(),
    }


def _rag_status(*, timeout: int) -> dict:
    # There's no reliable "operator wants RAG on" signal to read (the RAG
    # operation mode isn't recorded in an env var), so "enabled" mirrors
    # "configured": a runtime has been persisted via detect_llm_runtime.
    # This used to read QUERY_UNDERSTANDING_LLM_ENABLED, an unrelated query-understanding
    # toggle, which made a healthy RAG runtime silently report as disabled
    # (and skipped its health check) whenever query-understanding was off.
    target = target_from_persisted_config()
    runtime_status = load_llm_runtime_status()
    enabled = target is not None

    if target is None:
        return {
            "enabled": enabled,
            "configured": False,
            "available": False,
            "runtime_status": runtime_status,
            "message": "No LLM runtime persisted; run detect_llm_runtime --write.",
        }

    detail = {
        "enabled": enabled,
        "configured": True,
        "available": not runtime_status or runtime_status.get("status") == "healthy",
        "runtime_status": runtime_status,
        "endpoint_name": target.endpoint_name,
        "base_url": target.base_url,
        "model": target.model,
        "runtime": target.runtime,
        "priority_preset": target.priority_preset,
        "selection_mode": target.selection_mode,
        "fallback_used": target.fallback_used,
        "search_top_k": settings.RAG_SEARCH_TOP_K,
        "retrieval_threshold": settings.RAG_RETRIEVAL_THRESHOLD,
    }

    if detail["available"]:
        health = check_endpoint_health(target.base_url, timeout=timeout)
        detail["health_status"] = asdict(health)
        if health.ok:
            models = check_openai_compatible_endpoint(target.base_url, expected_model=target.model, timeout=timeout)
            detail["models_status"] = asdict(models)

    return detail


class Command(BaseCommand):
    help = "Report Dotori server connection, feature on/off, and per-feature configuration status."

    def add_arguments(self, parser):
        parser.add_argument("--json-output", action="store_true", help="Print machine-readable JSON.")
        parser.add_argument(
            "--skip-file-io",
            action="store_true",
            help="Skip the write/read/delete storage pipeline check.",
        )
        parser.add_argument(
            "--endpoint-timeout",
            type=int,
            default=2,
            help="Timeout (seconds) for the RAG endpoint liveness probe.",
        )

    def handle(self, *args, **options):
        payload = {
            "connection": _connection_status(),
            "features": {
                "file_io": _file_io_status(skip=options["skip_file_io"]),
                "embedding": _embedding_status(),
                "rag": _rag_status(timeout=options["endpoint_timeout"]),
            },
            "memory_reservations": _memory_reservations_status(),
        }

        if options["json_output"]:
            self.stdout.write(json_output(payload))
            return

        self._print_human(payload)

    def _print_human(self, payload: dict) -> None:
        connection_info = payload["connection"]
        self.stdout.write("Connection")
        self.stdout.write(f"db_ok: {connection_info['db_ok']} ({connection_info['message']})")
        self.stdout.write(f"allowed_hosts: {', '.join(connection_info['allowed_hosts']) or '-'}")
        self.stdout.write(f"debug: {connection_info['debug']}")
        self.stdout.write("")

        file_io = payload["features"]["file_io"]
        self.stdout.write("Feature status")
        self.stdout.write(f"file_io: enabled={file_io['enabled']}")
        if file_io["pipeline_check"] is not None:
            check = file_io["pipeline_check"]
            self.stdout.write(f"  pipeline_check: ok={check['ok']} ({check['message']})")

        embedding = payload["features"]["embedding"]
        self.stdout.write(f"embedding: enabled={embedding['enabled']}")

        rag = payload["features"]["rag"]
        self.stdout.write(f"rag: enabled={rag['enabled']} configured={rag.get('configured')}")
        self.stdout.write("")

        self.stdout.write("Feature detail")
        if embedding.get("configured"):
            self.stdout.write(
                f"embedding: model={embedding['model']} backend={embedding['backend']} "
                f"dimension={embedding['dimension']} sparse_enabled={embedding['sparse_enabled']} "
                f"generation={embedding['generation_id']}"
            )
        else:
            self.stdout.write(f"embedding: {embedding.get('message')}")
        if rag.get("configured"):
            self.stdout.write(
                f"rag: model={rag['model']} runtime={rag['runtime']} base_url={rag['base_url']} "
                f"priority_preset={rag['priority_preset']}"
            )
            health = rag.get("health_status")
            if health:
                self.stdout.write(f"  health_status: ok={health['ok']} ({health['message']})")
        else:
            self.stdout.write(f"rag: {rag.get('message')}")

        reservations = payload["memory_reservations"]
        self.stdout.write("")
        self.stdout.write(f"Memory reservations (scope: {reservations['scope']})")
        if not reservations["claims"]:
            self.stdout.write("no workload has claimed memory yet")
        else:
            for workload, claim in reservations["claims"].items():
                vram = claim["vram_per_gpu_mb"]
                vram_text = (
                    ", ".join(f"gpu{index}={value} MB" for index, value in enumerate(vram))
                    if vram
                    else "-"
                )
                self.stdout.write(
                    f"{workload}: device={claim['device']} ram={claim['ram_mb']} MB "
                    f"vram={vram_text} source={claim['source']}"
                )
                components = claim.get("components") or {}
                if components:
                    detail = " ".join(
                        f"{key}={value}" for key, value in sorted(components.items())
                    )
                    self.stdout.write(f"  components: {detail}")
                bounds = claim.get("bounds") or {}
                if bounds:
                    detail = " ".join(
                        f"{key}={value}" for key, value in sorted(bounds.items())
                    )
                    self.stdout.write(f"  bounds: {detail}")
            total = reservations["total"]
            total_vram = total["vram_per_gpu_mb"]
            total_vram_text = (
                ", ".join(f"gpu{index}={value} MB" for index, value in enumerate(total_vram))
                if total_vram
                else "-"
            )
            self.stdout.write(
                f"total: ram={total['ram_mb']} MB vram={total_vram_text}"
            )
