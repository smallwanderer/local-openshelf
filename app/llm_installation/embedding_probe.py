from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from llm_installation.embedding_config_store import write_embedding_runtime_generation

SUPPORTED_DIMENSIONS: dict[int, str] = {
    384: "pgvector_chunk_384",
    640: "pgvector_chunk_640",
    768: "pgvector_chunk_768",
    1024: "pgvector_chunk_1024",
    1536: "pgvector_chunk_1536",
}


@dataclass(frozen=True)
class EmbeddingProbeResult:
    ok: bool
    endpoint_url: str
    model_name: str
    dimension: int = 0
    store: str = ""
    store_supported: bool = False
    status_code: int | None = None
    elapsed_ms: int = 0
    error_message: str = ""


def normalize_openai_embeddings_url(base_url: str) -> str:
    cleaned = base_url.strip().rstrip("/")
    if cleaned.endswith("/embeddings"):
        return cleaned
    if cleaned.endswith("/v1"):
        return f"{cleaned}/embeddings"
    return f"{cleaned}/v1/embeddings"


def sanitize_model_slug(model_name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", model_name.strip())
    slug = re.sub(r"-+", "-", slug).strip("-").lower()
    return slug or "model"


def probe_openai_embedding_endpoint(
    base_url: str,
    model_name: str,
    api_key: str | None = None,
    timeout_seconds: float = 10.0,
    test_text: str = "dotori embedding probe",
) -> EmbeddingProbeResult:
    endpoint_url = normalize_openai_embeddings_url(base_url)
    started = time.monotonic()

    payload = {
        "model": model_name,
        "input": test_text,
    }
    encoded = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Dotori-Embedding-Probe/1.0",
    }
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    req = urllib.request.Request(
        endpoint_url,
        data=encoded,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            status_code = resp.status
            body_bytes = resp.read()
            data = json.loads(body_bytes.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        err_msg = f"HTTP {exc.code}: {exc.reason}"
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
            err_json = json.loads(err_body)
            if "error" in err_json:
                detail = err_json["error"]
                if isinstance(detail, dict) and "message" in detail:
                    err_msg += f" - {detail['message']}"
                else:
                    err_msg += f" - {detail}"
        except Exception:
            pass
        return EmbeddingProbeResult(
            ok=False,
            endpoint_url=endpoint_url,
            model_name=model_name,
            status_code=exc.code,
            elapsed_ms=elapsed_ms,
            error_message=err_msg,
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return EmbeddingProbeResult(
            ok=False,
            endpoint_url=endpoint_url,
            model_name=model_name,
            elapsed_ms=elapsed_ms,
            error_message=str(exc),
        )

    try:
        data_items = data.get("data", [])
        if not data_items or not isinstance(data_items, list):
            return EmbeddingProbeResult(
                ok=False,
                endpoint_url=endpoint_url,
                model_name=model_name,
                status_code=status_code,
                elapsed_ms=elapsed_ms,
                error_message="Endpoint returned 200 OK but 'data' array is empty or missing.",
            )
        embedding = data_items[0].get("embedding")
        if not embedding or not isinstance(embedding, list):
            return EmbeddingProbeResult(
                ok=False,
                endpoint_url=endpoint_url,
                model_name=model_name,
                status_code=status_code,
                elapsed_ms=elapsed_ms,
                error_message="Response missing 'embedding' vector array in data[0].",
            )
        dim = len(embedding)
    except Exception as exc:
        return EmbeddingProbeResult(
            ok=False,
            endpoint_url=endpoint_url,
            model_name=model_name,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            error_message=f"Failed to parse embedding vector: {exc}",
        )

    store = SUPPORTED_DIMENSIONS.get(dim, "")
    store_supported = bool(store)

    return EmbeddingProbeResult(
        ok=True,
        endpoint_url=endpoint_url,
        model_name=model_name,
        dimension=dim,
        store=store,
        store_supported=store_supported,
        status_code=status_code,
        elapsed_ms=elapsed_ms,
        error_message=(
            ""
            if store_supported
            else f"Dimension {dim} is not supported by Dotori stores (supported: {list(SUPPORTED_DIMENSIONS.keys())})."
        ),
    )


def build_external_embedding_entry(
    model_name: str,
    dimension: int,
    store: str,
    *,
    display_name: str | None = None,
    languages: list[str] | None = None,
    model_input_max_tokens: int = 8192,
) -> SimpleNamespace:
    slug = sanitize_model_slug(model_name)
    return SimpleNamespace(
        id=f"openai-{slug}",
        display_name=display_name or f"OpenAI-compatible: {model_name}",
        description=f"External OpenAI-compatible embedding model ({model_name})",
        license="external",
        model_id=f"openai-{slug}",
        repo_id=model_name,
        revision="external",
        tokenizer_id=model_name,
        tokenizer_revision="external",
        provider="openai_compatible",
        store=store,
        dimension=dimension,
        model_input_max_tokens=model_input_max_tokens,
        supports_sparse=False,
        normalize_embeddings=True,
        distance_strategy="inner_product",
        query_prefix="",
        document_prefix="",
        availability="supported",
        priority=60,
        presets=[],
        languages=languages or ["multilingual"],
    )


def stage_external_embedding_generation(
    scope: str,
    entry: Any,
    *,
    repo_root: Path | None = None,
) -> tuple[str, Path]:
    slug = sanitize_model_slug(getattr(entry, "repo_id", getattr(entry, "id", "external")))
    generation_id = f"{scope}-embedding-external-{slug}-{int(time.time())}"
    generation_dir = write_embedding_runtime_generation(
        scope=scope,
        generation_id=generation_id,
        entry=entry,
        repo_root=repo_root,
    )
    return generation_id, generation_dir


def load_active_embedding_runtime_summary(
    scope: str = "production",
    *,
    repo_root: Path | None = None,
) -> SimpleNamespace | None:
    """Read the currently active embedding runtime from disk with Python standard library only."""
    from llm_installation.embedding_config_store import get_embedding_runtime_config_path
    active_path = get_embedding_runtime_config_path(scope, repo_root=repo_root)
    if not active_path.exists():
        return None
    try:
        data = json.loads(active_path.read_text(encoding="utf-8"))
        return SimpleNamespace(**data)
    except Exception:
        return None


def load_host_embedding_catalog(
    repo_root: Path | None = None,
) -> list[SimpleNamespace]:
    """Load supported embedding catalog entries with Python standard library only."""
    root = repo_root or Path(__file__).resolve().parents[2]
    catalog_dir = root / "app" / "llm_installation" / "embedding_catalog"
    models_dir = catalog_dir / "models"
    profiles_dir = catalog_dir / "profiles"

    models_by_id: dict[str, dict] = {}
    for p in models_dir.rglob("*.json"):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            models_by_id[m["id"]] = m
        except Exception:
            pass

    resolved = []
    for p in profiles_dir.rglob("*.json"):
        try:
            prof = json.loads(p.read_text(encoding="utf-8"))
            if prof.get("availability") != "supported":
                continue
            model = models_by_id.get(prof.get("model_id"))
            if not model:
                continue
            combined = dict(model)
            combined.update({k: v for k, v in prof.items() if k != "model_id"})
            resolved.append(SimpleNamespace(**combined))
        except Exception:
            pass

    return sorted(resolved, key=lambda x: getattr(x, "priority", 0), reverse=True)

