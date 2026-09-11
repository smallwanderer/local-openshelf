#!/usr/bin/env python3
"""Calibrate the active local LLM concurrency with a real Dotori RAG workload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPO_ROOT / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from document_ai.services.rag_runtime_config import (  # noqa: E402
    load_llm_runtime_config,
    normalize_serving_profile,
)
from llm_installation.calibration import (  # noqa: E402
    CALIBRATION_SCHEMA_VERSION,
    WorkloadItem,
    load_workload,
    select_serving_concurrency,
    summarize_step,
    workload_sha256,
)
from llm_installation.config_store import (  # noqa: E402
    stage_calibration_runtime_generation,
)
from llm_installation.embedding_config_store import (  # noqa: E402
    get_embedding_runtime_config_path,
)
from llm_installation.runtime_lifecycle import (  # noqa: E402
    LLM_UNAVAILABLE_OOM,
    SCOPE_CONFIG,
    RuntimeLifecycleManager,
    build_runtime_spec,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = f"{hostname}:{parts.port}" if parts.port else hostname
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def _load_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _payload_sha256(payload: dict) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _read_secret(value: str | None, file_path: str | None, env_name: str) -> str:
    if value:
        return value.strip()
    if file_path:
        return Path(file_path).read_text(encoding="utf-8").strip()
    return os.getenv(env_name, "").strip()


def _validate_run_id(value: str) -> str:
    if not value or len(value) > 64 or any(
        not (character.isalnum() or character in "-_") for character in value
    ):
        raise SystemExit("run ID may contain only letters, numbers, '-' and '_'")
    return value


class NvidiaSampler:
    def __init__(self, *, concurrency: int, interval_seconds: float):
        self.concurrency = concurrency
        self.interval_seconds = interval_seconds
        self.samples: list[dict] = []
        self.error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._binary = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")

    def start(self) -> None:
        self._started = time.perf_counter()
        if not self._binary:
            self.error = "nvidia-smi not found"
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"dotori-calibration-gpu-c{self.concurrency}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self.interval_seconds * 2))

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            self._stop.wait(self.interval_seconds)

    def _sample_once(self) -> None:
        command = [
            str(self._binary),
            "--query-gpu=index,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw",
            "--format=csv,noheader,nounits",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.error = str(exc)
            return
        if result.returncode != 0:
            self.error = (result.stderr or result.stdout).strip()[:500]
            return
        for line in result.stdout.splitlines():
            values = [part.strip() for part in line.split(",")]
            if len(values) != 6:
                continue
            try:
                sample = {
                    "schema_version": CALIBRATION_SCHEMA_VERSION,
                    "concurrency": self.concurrency,
                    "offset_ms": round((time.perf_counter() - self._started) * 1000, 3),
                    "gpu_index": int(values[0]),
                    "gpu_utilization_percent": float(values[1]),
                    "memory_utilization_percent": float(values[2]),
                    "memory_used_mb": float(values[3]),
                    "memory_total_mb": float(values[4]),
                    "power_watts": float(values[5]),
                }
            except ValueError:
                continue
            self.samples.append(sample)

    def summary(self) -> dict:
        if not self.samples:
            return {
                "available": False,
                "sample_count": 0,
                "error": self.error or "no GPU samples collected",
            }
        return {
            "available": True,
            "sample_count": len(self.samples),
            "peak_gpu_utilization_percent": max(
                sample["gpu_utilization_percent"] for sample in self.samples
            ),
            "peak_memory_used_mb": max(sample["memory_used_mb"] for sample in self.samples),
            "peak_memory_utilization_percent": max(
                sample["memory_utilization_percent"] for sample in self.samples
            ),
            "peak_power_watts": max(sample["power_watts"] for sample in self.samples),
        }


def _run_request(
    *,
    url: str,
    cookie: str,
    csrf_token: str,
    timeout: int,
    expected_model: str,
    item: WorkloadItem,
    concurrency: int,
    phase: str,
    round_number: int,
    request_number: int,
    barrier: threading.Barrier,
    run_started: float,
) -> dict:
    headers = {
        "Accept": "application/x-ndjson",
        "Content-Type": "application/json",
        "Cookie": cookie,
        "X-CSRFToken": csrf_token,
    }
    try:
        barrier.wait(timeout=15)
    except threading.BrokenBarrierError as exc:
        return {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "concurrency": concurrency,
            "phase": phase,
            "round": round_number,
            "request": request_number,
            "workload_id": item.item_id,
            "ok": False,
            "error": f"request start barrier failed: {exc}",
        }

    started_at = _utc_now()
    started = time.perf_counter()
    first_token_at = None
    event_types: list[str] = []
    job_id = None
    performance_metrics: dict = {}
    terminal_type = ""
    llm_target = ""
    llm_model = ""
    error = ""
    try:
        with requests.post(
            url,
            headers=headers,
            json=item.request_payload(),
            stream=True,
            timeout=(5, timeout),
        ) as response:
            response.raise_for_status()
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                event = json.loads(raw_line)
                event_type = str(event.get("type") or "")
                event_types.append(event_type)
                if event_type == "started":
                    job_id = event.get("job_id")
                    llm_target = str(event.get("llm_target") or "")
                    llm_model = str(event.get("llm_model") or "")
                elif event_type == "token" and first_token_at is None:
                    first_token_at = time.perf_counter()
                elif event_type in {"completed", "error", "canceled"}:
                    terminal_type = event_type
                    if isinstance(event.get("performance_metrics"), dict):
                        performance_metrics = event["performance_metrics"]
                    if event_type == "error":
                        error = str(event.get("message") or event.get("code") or "RAG error")
    except Exception as exc:
        error = str(exc)

    completed = time.perf_counter()
    if llm_target and llm_target != "server":
        error = "RAG request used an external LLM instead of the server runtime"
    elif llm_model != expected_model:
        error = (
            f"RAG request used model {llm_model!r}, expected active model "
            f"{expected_model!r}"
        )
    ok = (
        terminal_type == "completed"
        and llm_target == "server"
        and llm_model == expected_model
    )
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "concurrency": concurrency,
        "phase": phase,
        "round": round_number,
        "request": request_number,
        "workload_id": item.item_id,
        "job_id": job_id,
        "llm_target": llm_target,
        "llm_model": llm_model,
        "ok": ok,
        "terminal_type": terminal_type,
        "error": error,
        "started_at": started_at,
        "started_offset_ms": round((started - run_started) * 1000, 3),
        "completed_offset_ms": round((completed - run_started) * 1000, 3),
        "ttft_ms": (
            round((first_token_at - started) * 1000, 3)
            if first_token_at is not None
            else None
        ),
        "total_ms": round((completed - started) * 1000, 3),
        "event_types": event_types,
        "performance_metrics": performance_metrics,
    }


def _run_batches(
    *,
    items: list[WorkloadItem],
    phase: str,
    rounds: int,
    concurrency: int,
    url: str,
    cookie: str,
    csrf_token: str,
    timeout: int,
    expected_model: str,
    run_started: float,
) -> list[dict]:
    phase_items = [item for item in items if item.phase == phase]
    if rounds == 0:
        return []
    if not phase_items:
        raise ValueError(f"workload has no phase={phase!r} items")

    records: list[dict] = []
    item_offset = 0
    for round_number in range(1, rounds + 1):
        selected = [
            phase_items[(item_offset + index) % len(phase_items)]
            for index in range(concurrency)
        ]
        item_offset += concurrency
        barrier = threading.Barrier(concurrency)
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(
                    _run_request,
                    url=url,
                    cookie=cookie,
                    csrf_token=csrf_token,
                    timeout=timeout,
                    expected_model=expected_model,
                    item=item,
                    concurrency=concurrency,
                    phase=phase,
                    round_number=round_number,
                    request_number=index + 1,
                    barrier=barrier,
                    run_started=run_started,
                )
                for index, item in enumerate(selected)
            ]
            for future in as_completed(futures):
                records.append(future.result())
    return records


def _active_runtime_identity(payload: dict) -> tuple[str, str, str, str, dict]:
    target = payload.get("target") if isinstance(payload, dict) else None
    if not isinstance(target, dict):
        raise ValueError("No active LLM runtime target is configured")
    runtime = str(target.get("runtime") or "")
    model = str(target.get("model") or "")
    generation_id = str(target.get("generation_id") or "")
    preset = str(target.get("priority_preset") or "balanced")
    profile = normalize_serving_profile(target.get("serving_profile"))
    if runtime not in {"llama.cpp", "vllm"} or not model or not generation_id:
        raise ValueError("Calibration requires an active managed llama.cpp or vLLM runtime")
    return runtime, model, generation_id, preset, profile


def _restore_interrupted_calibration(
    *,
    scope: str,
    payload: dict,
    manager: RuntimeLifecycleManager,
) -> dict:
    """Restore the pre-calibration generation left by a terminated runner."""
    for _ in range(16):
        runtime, model, generation_id, _preset, profile = _active_runtime_identity(payload)
        if profile.get("calibration_status") != "running":
            return payload
        restore_generation_id = str(
            profile.get("calibration_original_generation_id")
            or profile.get("calibration_source_generation_id")
            or ""
        )
        if not restore_generation_id or restore_generation_id == generation_id:
            raise RuntimeError(
                "Interrupted calibration has no recoverable source generation"
            )
        restore_spec = build_runtime_spec(
            scope,
            runtime,
            model,
            restore_generation_id,
            repo_root=REPO_ROOT,
        )
        result = manager.apply(restore_spec, rebuild_image=False)
        if not result.ok:
            raise RuntimeError(
                "Failed to restore interrupted calibration: "
                + "; ".join(result.messages)
            )
        payload = load_llm_runtime_config(scope=scope, repo_root=REPO_ROOT)
    raise RuntimeError("Interrupted calibration generation chain is too deep")


def _restart_rag_worker_if_present(scope: str) -> dict:
    scope_config = SCOPE_CONFIG[scope]
    services = subprocess.run(
        ["docker", "compose", "-f", scope_config.compose_file, "config", "--services"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if services.returncode != 0:
        return {"restarted": False, "present": None, "error": services.stderr.strip()[:500]}
    if "rag-worker" not in set(services.stdout.splitlines()):
        return {"restarted": False, "present": False}
    restart = subprocess.run(
        ["docker", "compose", "-f", scope_config.compose_file, "restart", "rag-worker"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "restarted": restart.returncode == 0,
        "present": True,
        "error": restart.stderr.strip()[:500] if restart.returncode else "",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", required=True, type=Path, help="JSONL workload file")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/api/document-ai/v1/rag/stream/",
    )
    parser.add_argument("--scope", choices=sorted(SCOPE_CONFIG), default="production")
    parser.add_argument("--warmup-rounds", type=int, default=1)
    parser.add_argument("--measurement-rounds", type=int, default=3)
    parser.add_argument("--max-concurrency", type=int)
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--gpu-sample-interval", type=float, default=0.5)
    parser.add_argument("--output-root", type=Path, default=REPO_ROOT / "data" / "evaluation" / "runs")
    parser.add_argument("--run-id")
    parser.add_argument("--allow-unscoped", action="store_true")
    cookie = parser.add_mutually_exclusive_group()
    cookie.add_argument("--cookie")
    cookie.add_argument("--cookie-file")
    csrf = parser.add_mutually_exclusive_group()
    csrf.add_argument("--csrf-token")
    csrf.add_argument("--csrf-token-file")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.warmup_rounds < 0 or args.measurement_rounds < 1:
        raise SystemExit("warmup rounds must be >= 0 and measurement rounds must be >= 1")
    if args.timeout < 1 or args.gpu_sample_interval <= 0:
        raise SystemExit("timeout and gpu sample interval must be positive")

    cookie = _read_secret(args.cookie, args.cookie_file, "DOTORI_CALIBRATION_COOKIE")
    csrf_token = _read_secret(
        args.csrf_token, args.csrf_token_file, "DOTORI_CALIBRATION_CSRF_TOKEN"
    )
    if not cookie or not csrf_token:
        raise SystemExit(
            "Authentication is required. Pass cookie/CSRF files or set "
            "DOTORI_CALIBRATION_COOKIE and DOTORI_CALIBRATION_CSRF_TOKEN."
        )

    workload_path = args.workload.resolve()
    workload = load_workload(workload_path, allow_unscoped=args.allow_unscoped)
    manager = RuntimeLifecycleManager(REPO_ROOT)
    active_payload = load_llm_runtime_config(scope=args.scope, repo_root=REPO_ROOT)
    active_payload = _restore_interrupted_calibration(
        scope=args.scope,
        payload=active_payload,
        manager=manager,
    )
    runtime, model, original_generation_id, preset, serving_profile = (
        _active_runtime_identity(active_payload)
    )
    safe_ceiling = int(serving_profile.get("safe_concurrency_ceiling") or 1)
    max_concurrency = (
        safe_ceiling if args.max_concurrency is None else args.max_concurrency
    )
    if max_concurrency < 1 or max_concurrency > safe_ceiling:
        raise SystemExit(
            f"max concurrency must be between 1 and safe ceiling {safe_ceiling}"
        )

    run_id = _validate_run_id(
        args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    run_directory = args.output_root / run_id
    try:
        run_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise SystemExit(f"calibration run already exists: {run_directory}") from exc

    embedding_payload = _load_json(
        get_embedding_runtime_config_path(args.scope, repo_root=REPO_ROOT)
    )
    manifest = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "run_id": run_id,
        "status": "running",
        "started_at": _utc_now(),
        "scope": args.scope,
        "git_commit": _git_commit(),
        "endpoint": _safe_url(args.url),
        "workload": {
            "filename": workload_path.name,
            "sha256": workload_sha256(workload_path),
            "items": len(workload),
            "warmup_items": sum(item.phase == "warmup" for item in workload),
            "measurement_items": sum(item.phase == "measure" for item in workload),
            "questions_persisted": False,
        },
        "llm_runtime": {
            "runtime": runtime,
            "model": model,
            "source_generation_id": original_generation_id,
            "priority_preset": preset,
            "safe_concurrency_ceiling": safe_ceiling,
            "context_length": serving_profile.get("context_length"),
            "source_config_sha256": _payload_sha256(active_payload),
        },
        "hardware_profile": active_payload.get("profile") or {},
        "embedding_runtime": {
            key: embedding_payload.get(key)
            for key in (
                "catalog_id",
                "generation_id",
                "model_id",
                "model_revision",
                "runtime_fingerprint",
            )
            if embedding_payload.get(key) is not None
        },
        "parameters": {
            "warmup_rounds": args.warmup_rounds,
            "measurement_rounds": args.measurement_rounds,
            "max_concurrency": max_concurrency,
            "timeout_seconds": args.timeout,
            "gpu_sample_interval_seconds": args.gpu_sample_interval,
        },
    }
    _write_json(run_directory / "manifest.json", manifest)

    run_started = time.perf_counter()
    records: list[dict] = []
    resource_samples: list[dict] = []
    failures: list[dict] = []
    steps: list[dict] = []
    current_generation_id = original_generation_id
    final_generation_id = ""
    calibrated = False

    try:
        for concurrency in range(1, max_concurrency + 1):
            generation_id, _ = stage_calibration_runtime_generation(
                scope=args.scope,
                serving_concurrency=concurrency,
                calibration_run_id=run_id,
                calibration_status="running",
                repo_root=REPO_ROOT,
            )
            spec = build_runtime_spec(
                args.scope, runtime, model, generation_id, repo_root=REPO_ROOT
            )
            apply_result = manager.apply(spec, rebuild_image=False)
            if not apply_result.ok:
                failures.append(
                    {
                        "at": _utc_now(),
                        "phase": "runtime_candidate",
                        "concurrency": concurrency,
                        "failure_code": apply_result.failure_code,
                        "oom": apply_result.failure_code == LLM_UNAVAILABLE_OOM,
                        "rolled_back": apply_result.rolled_back,
                        "messages": apply_result.messages,
                    }
                )
                break
            current_generation_id = generation_id

            sampler = NvidiaSampler(
                concurrency=concurrency,
                interval_seconds=args.gpu_sample_interval,
            )
            sampler.start()
            try:
                step_records = _run_batches(
                    items=workload,
                    phase="warmup",
                    rounds=args.warmup_rounds,
                    concurrency=concurrency,
                    url=args.url,
                    cookie=cookie,
                    csrf_token=csrf_token,
                    timeout=args.timeout,
                    expected_model=model,
                    run_started=run_started,
                )
                step_records.extend(
                    _run_batches(
                        items=workload,
                        phase="measure",
                        rounds=args.measurement_rounds,
                        concurrency=concurrency,
                        url=args.url,
                        cookie=cookie,
                        csrf_token=csrf_token,
                        timeout=args.timeout,
                        expected_model=model,
                        run_started=run_started,
                    )
                )
            finally:
                sampler.stop()

            records.extend(step_records)
            resource_samples.extend(sampler.samples)
            step_summary = summarize_step(
                concurrency=concurrency,
                records=step_records,
                resource_summary=sampler.summary(),
            )
            step_summary["runtime_generation_id"] = generation_id
            steps.append(step_summary)
            _write_jsonl(run_directory / "requests.jsonl", records)
            _write_jsonl(run_directory / "resource_samples.jsonl", resource_samples)
            _write_json(run_directory / "steps.json", steps)

            print(
                json.dumps(
                    {
                        "concurrency": concurrency,
                        "aggregate_output_tokens_per_sec": step_summary[
                            "aggregate_output_tokens_per_sec"
                        ],
                        "ttft_p95_ms": step_summary["client_ttft_ms"]["p95"],
                        "total_p95_ms": step_summary["client_total_ms"]["p95"],
                        "failed": step_summary["failed"],
                    },
                    ensure_ascii=False,
                )
            )
            if step_summary["failed"] or not step_summary["metrics_complete"]:
                break
            partial_selection = select_serving_concurrency(
                steps, priority_preset=preset
            )
            if not partial_selection["decisions"][-1]["accepted"]:
                break

        if not steps:
            raise RuntimeError("No concurrency candidate completed measurement")
        selection = select_serving_concurrency(steps, priority_preset=preset)
        first_decision = selection["decisions"][0]
        if not first_decision["accepted"]:
            raise RuntimeError(
                "The concurrency=1 baseline is invalid: " + ", ".join(first_decision["reasons"])
            )
        selected = int(selection["selected_serving_concurrency"])
        final_generation_id, _ = stage_calibration_runtime_generation(
            scope=args.scope,
            serving_concurrency=selected,
            calibration_run_id=run_id,
            calibration_status="calibrated",
            repo_root=REPO_ROOT,
        )
        final_spec = build_runtime_spec(
            args.scope, runtime, model, final_generation_id, repo_root=REPO_ROOT
        )
        final_result = manager.apply(final_spec, rebuild_image=False)
        if not final_result.ok:
            raise RuntimeError("Failed to activate selected concurrency: " + "; ".join(final_result.messages))
        current_generation_id = final_generation_id
        worker_restart = _restart_rag_worker_if_present(args.scope)
        if worker_restart.get("present") and not worker_restart.get("restarted"):
            raise RuntimeError("Selected runtime is active, but rag-worker restart failed")
        calibrated = True

        summary = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "run_id": run_id,
            "status": "calibrated",
            "completed_at": _utc_now(),
            "selection": selection,
            "steps": steps,
            "failures": failures,
            "active_generation_id": final_generation_id,
            "rag_worker": worker_restart,
            "durable_queue": "deferred",
        }
        _write_json(run_directory / "summary.json", summary)
        _write_jsonl(run_directory / "failures.jsonl", failures)
        manifest["status"] = "calibrated"
        manifest["completed_at"] = summary["completed_at"]
        manifest["selected_serving_concurrency"] = selected
        manifest["active_generation_id"] = final_generation_id
        _write_json(run_directory / "manifest.json", manifest)
        print(json.dumps({"summary": summary}, ensure_ascii=False))
        print(f"Calibration artifacts: {run_directory}")
        return 0
    except Exception as exc:
        failures.append({"at": _utc_now(), "phase": "calibration", "error": str(exc)})
        rollback = None
        if not calibrated and current_generation_id != original_generation_id:
            original_spec = build_runtime_spec(
                args.scope,
                runtime,
                model,
                original_generation_id,
                repo_root=REPO_ROOT,
            )
            rollback = manager.apply(original_spec, rebuild_image=False)
        failure_summary = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "run_id": run_id,
            "status": "failed",
            "completed_at": _utc_now(),
            "error": str(exc),
            "steps": steps,
            "failures": failures,
            "rollback": (
                {
                    "ok": rollback.ok,
                    "rolled_back": rollback.rolled_back,
                    "messages": rollback.messages,
                }
                if rollback is not None
                else None
            ),
        }
        _write_json(run_directory / "summary.json", failure_summary)
        _write_jsonl(run_directory / "failures.jsonl", failures)
        _write_jsonl(run_directory / "requests.jsonl", records)
        _write_jsonl(run_directory / "resource_samples.jsonl", resource_samples)
        manifest["status"] = "failed"
        manifest["completed_at"] = failure_summary["completed_at"]
        _write_json(run_directory / "manifest.json", manifest)
        print(f"Calibration failed: {exc}", file=sys.stderr)
        print(f"Calibration artifacts: {run_directory}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
