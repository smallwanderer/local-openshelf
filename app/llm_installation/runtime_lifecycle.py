from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from llm_installation.memory_reservations import claim_llm_runtime

RUNTIME_IMAGE = {
    "llama.cpp": "dotori/llama-rag",
    "vllm": "dotori/vllm-rag",
}

RUNTIME_BUILD_CONTEXT = {
    "llama.cpp": "llm-runtime",
    "vllm": "llm-runtime",
}

RUNTIME_DOCKERFILE = {
    "llama.cpp": "llama.Dockerfile",
    "vllm": "vllm.Dockerfile",
}

RUNTIME_ARGS_ENV = "RAG_RUNTIME_ARGS_FILE"
HUGGINGFACE_CACHE_VOLUME = "dotori-huggingface-cache"

NETWORK_ALIAS = "rag-runtime"

LABEL_MANAGED = "com.dotori.managed"
LABEL_COMPONENT = "com.dotori.component"
LABEL_SCOPE = "com.dotori.scope"
LABEL_RUNTIME = "com.dotori.runtime"
LABEL_GENERATION = "com.dotori.generation"
LABEL_IMAGE_REVISION = "com.dotori.image-revision"
COMPONENT_VALUE = "rag-runtime"

HEALTH_POLL_INTERVAL_S = 2
HEALTH_TIMEOUT_S = {"llama.cpp": 900, "vllm": 1200}
RUNTIME_STATUS_FILENAME = "runtime_status.json"

LLM_UNAVAILABLE_OOM = "LLM_UNAVAILABLE_OOM"
LLM_UNAVAILABLE_TIMEOUT = "LLM_UNAVAILABLE_TIMEOUT"
LLM_UNAVAILABLE_START_FAILED = "LLM_UNAVAILABLE_START_FAILED"


@dataclass(frozen=True)
class ScopeConfig:
    compose_file: str
    env_file: str
    network_name: str
    container_name: str


SCOPE_CONFIG = {
    "production": ScopeConfig(
        compose_file="docker-compose.yml",
        env_file=".env",
        network_name="dotori-runtime",
        container_name="dotori-llm",
    ),
}


@dataclass(frozen=True)
class RuntimeSpec:
    scope: str
    runtime: str
    model_id: str
    image: str
    container_name: str
    network_name: str
    network_alias: str
    generation_id: str
    args_file: Path
    health_url: str
    model_probe_url: str | None


@dataclass
class ApplyResult:
    ok: bool
    rolled_back: bool = False
    failure_code: str | None = None
    messages: list[str] = field(default_factory=list)


@dataclass
class ContainerState:
    exists: bool
    owned: bool = False
    running: bool = False
    health: str | None = None
    oom_killed: bool = False
    exit_code: int | None = None
    restart_count: int = 0
    labels: dict[str, str] = field(default_factory=dict)


def get_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def make_generation_id(integrity_sha256: str) -> str:
    return f"{int(time.time())}-{integrity_sha256[:12]}"


def runtime_status_path(scope: str, *, repo_root: Path | None = None) -> Path:
    if scope not in SCOPE_CONFIG:
        raise ValueError(f"Unknown scope: {scope}")
    root = repo_root or get_repo_root()
    return root / "data" / "config" / "runtime_scopes" / scope / RUNTIME_STATUS_FILENAME


def load_runtime_status(scope: str, *, repo_root: Path | None = None) -> dict:
    path = runtime_status_path(scope, repo_root=repo_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def build_runtime_spec(
    scope: str,
    runtime: str,
    model_id: str,
    generation_id: str,
    *,
    repo_root: Path | None = None,
) -> RuntimeSpec:
    if scope not in SCOPE_CONFIG:
        raise ValueError(f"Unknown scope: {scope}")
    if runtime not in RUNTIME_IMAGE:
        raise ValueError(f"Unknown runtime: {runtime}")

    root = repo_root or get_repo_root()
    scope_cfg = SCOPE_CONFIG[scope]
    generation_dir = (
        root / "data" / "config" / "runtime_scopes" / scope / "generations" / generation_id
    )
    return RuntimeSpec(
        scope=scope,
        runtime=runtime,
        model_id=model_id,
        image=RUNTIME_IMAGE[runtime],
        container_name=scope_cfg.container_name,
        network_name=scope_cfg.network_name,
        network_alias=NETWORK_ALIAS,
        generation_id=generation_id,
        args_file=generation_dir / "runtime.args",
        health_url="http://localhost:8080/health",
        model_probe_url="http://localhost:8080/v1/models",
    )


def _default_runner(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **kwargs,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=str(exc))


class RuntimeLifecycleManager:
    """Owns the Docker lifecycle of the single, scope-scoped RAG runtime
    container. Never selects a model/backend/runtime itself -- it only
    applies an already-resolved RuntimeSpec. See
    dev-docs/agent/rag-runtime-container-lifecycle.md for the full contract.
    """

    def __init__(
        self,
        repo_root: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ):
        self.repo_root = repo_root or get_repo_root()
        self._runner = runner or _default_runner

    def _docker(self, *args: str) -> subprocess.CompletedProcess:
        return self._runner(["docker", *args], cwd=str(self.repo_root))

    def _compose(self, scope: str, *args: str) -> subprocess.CompletedProcess:
        compose_file = SCOPE_CONFIG[scope].compose_file
        return self._runner(
            ["docker", "compose", "-f", compose_file, *args], cwd=str(self.repo_root)
        )

    # -- inspection -----------------------------------------------------

    def ensure_network(self, scope: str) -> bool:
        network_name = SCOPE_CONFIG[scope].network_name
        result = self._docker("network", "inspect", network_name)
        if result.returncode == 0:
            return True
        result = self._docker("network", "create", network_name)
        return result.returncode == 0

    def inspect(self, scope: str) -> ContainerState:
        container_name = SCOPE_CONFIG[scope].container_name
        result = self._docker(
            "inspect",
            "-f",
            "{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|"
            "{{.State.OOMKilled}}|{{.State.ExitCode}}|{{.RestartCount}}",
            container_name,
        )
        if result.returncode != 0:
            return ContainerState(exists=False)

        labels = self._container_labels(container_name)
        owned = (
            labels.get(LABEL_MANAGED) == "true"
            and labels.get(LABEL_COMPONENT) == COMPONENT_VALUE
            and labels.get(LABEL_SCOPE) == scope
        )
        parts = result.stdout.strip().split("|")
        running = parts[0] == "true" if parts else False
        health = parts[1] if len(parts) > 1 and parts[1] else None
        oom_killed = len(parts) > 2 and parts[2] == "true"
        try:
            exit_code = int(parts[3]) if len(parts) > 3 and parts[3] else None
        except ValueError:
            exit_code = None
        try:
            restart_count = int(parts[4]) if len(parts) > 4 and parts[4] else 0
        except ValueError:
            restart_count = 0
        return ContainerState(
            exists=True,
            owned=owned,
            running=running,
            health=health,
            oom_killed=oom_killed,
            exit_code=exit_code,
            restart_count=restart_count,
            labels=labels,
        )

    def _container_labels(self, container_name: str) -> dict[str, str]:
        result = self._docker(
            "inspect", "-f", "{{json .Config.Labels}}", container_name
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        import json as _json

        try:
            return _json.loads(result.stdout.strip()) or {}
        except ValueError:
            return {}

    # -- build/run --------------------------------------------------------

    def _write_runtime_status(
        self,
        spec: RuntimeSpec,
        status: str,
        *,
        reason_code: str = "",
        message: str = "",
        retryable: bool = False,
    ) -> None:
        path = runtime_status_path(spec.scope, repo_root=self.repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "scope": spec.scope,
            "status": status,
            "reason_code": reason_code,
            "message": message,
            "retryable": retryable,
            "runtime": spec.runtime,
            "model": spec.model_id,
            "generation_id": spec.generation_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

        # Claim memory once the runtime is actually serving. Every status
        # transition funnels through here, so this covers both apply() and
        # resume(). A non-healthy status does not release the claim: the
        # container still exists and is expected to come back, and the
        # embedding side must not size itself into memory that is about to be
        # reoccupied. Only an explicit removal releases it.
        if status == "healthy":
            claim_llm_runtime(
                spec.scope, spec.generation_id, repo_root=self.repo_root
            )

    def _restore_runtime_status(self, scope: str, payload: dict) -> None:
        path = runtime_status_path(scope, repo_root=self.repo_root)
        if not payload:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @staticmethod
    def _failure_details(state: ContainerState, *, timed_out: bool) -> tuple[str, str]:
        if state.oom_killed or state.exit_code == 137:
            return (
                LLM_UNAVAILABLE_OOM,
                "The local LLM was disabled after Docker reported an out-of-memory exit.",
            )
        if timed_out:
            return (
                LLM_UNAVAILABLE_TIMEOUT,
                "The local LLM did not become healthy before the startup timeout.",
            )
        return (
            LLM_UNAVAILABLE_START_FAILED,
            "The local LLM exited or failed endpoint validation during startup.",
        )

    def _promote_restart_policy(self, container_name: str) -> bool:
        return self._docker(
            "update", "--restart", "unless-stopped", container_name
        ).returncode == 0

    def build(self, spec: RuntimeSpec) -> tuple[bool, str | None]:
        build_context = self.repo_root / RUNTIME_BUILD_CONTEXT[spec.runtime]
        dockerfile = build_context / RUNTIME_DOCKERFILE[spec.runtime]
        result = self._docker(
            "build",
            "-q",
            "-t",
            spec.image,
            "-f",
            str(dockerfile),
            str(build_context),
        )
        if result.returncode != 0:
            return False, None
        revision = result.stdout.strip() or None
        if not revision:
            id_result = self._docker("inspect", "-f", "{{.Id}}", spec.image)
            revision = id_result.stdout.strip() if id_result.returncode == 0 else None
        return True, revision

    def _image_revision(self, image: str) -> str | None:
        result = self._docker("inspect", "-f", "{{.Id}}", image)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _run_candidate(self, spec: RuntimeSpec, image_revision: str | None) -> bool:
        self._docker("rm", "-f", spec.container_name)

        run_args = [
            "run", "-d",
            "--name", spec.container_name,
            "--network", spec.network_name,
            "--network-alias", spec.network_alias,
            # A candidate gets one attempt. Automatic restart is enabled only
            # after health and endpoint validation succeed.
            "--restart", "no",
            "--env-file", str(self.repo_root / SCOPE_CONFIG[spec.scope].env_file),
            "-v", f"{HUGGINGFACE_CACHE_VOLUME}:/root/.cache/huggingface",
            "-v", f"{spec.args_file}:/runtime/runtime.args:ro",
            "-e", f"{RUNTIME_ARGS_ENV}=/runtime/runtime.args",
            "--label", f"{LABEL_MANAGED}=true",
            "--label", f"{LABEL_COMPONENT}={COMPONENT_VALUE}",
            "--label", f"{LABEL_SCOPE}={spec.scope}",
            "--label", f"{LABEL_RUNTIME}={spec.runtime}",
            "--label", f"{LABEL_GENERATION}={spec.generation_id}",
        ]
        if image_revision:
            run_args += ["--label", f"{LABEL_IMAGE_REVISION}={image_revision}"]

        if spec.runtime == "vllm":
            run_args += [
                "--gpus", "all",
                "--shm-size", "4g",
                "--health-cmd",
                "python3 -c \"import urllib.request; "
                "urllib.request.urlopen('http://localhost:8080/health', timeout=2)\"",
                "--health-interval", "10s",
                "--health-timeout", "5s",
                "--health-retries", "12",
                "--health-start-period", "1200s",
            ]
        else:
            run_args += [
                "--health-cmd", "curl -f http://localhost:8080/health",
                "--health-interval", "10s",
                "--health-timeout", "5s",
                "--health-retries", "12",
                "--health-start-period", "900s",
            ]
        run_args.append(spec.image)

        result = self._docker(*run_args)
        if result.returncode == 0:
            self._write_runtime_status(
                spec,
                "starting",
                message="The local LLM candidate is loading and being validated.",
            )
        return result.returncode == 0

    def _wait_healthy(self, spec: RuntimeSpec) -> bool:
        timeout = HEALTH_TIMEOUT_S.get(spec.runtime, 180)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.inspect(spec.scope)
            if not state.exists or not state.running:
                return False
            if state.health == "healthy":
                return True
            if state.health == "unhealthy":
                return False
            time.sleep(HEALTH_POLL_INTERVAL_S)
        return False

    def _runtime_logs(self, container_name: str) -> str:
        result = self._docker("logs", "--tail", "100", container_name)
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
        )
        return output[-4000:]

    def _verify_endpoints(self, spec: RuntimeSpec) -> tuple[bool, str]:
        health = self._docker(
            "exec", spec.container_name, "curl", "-sf", spec.health_url
        )
        if health.returncode != 0:
            return False, f"/health check failed: {health.stderr.strip()[:200]}"
        if spec.model_probe_url:
            probe = self._docker(
                "exec", spec.container_name, "curl", "-sf", spec.model_probe_url
            )
            if probe.returncode != 0:
                return True, "/v1/models probe failed (non-fatal)"
        return True, "ok"

    # -- lifecycle --------------------------------------------------------

    def resume(self, spec: RuntimeSpec) -> ApplyResult:
        """Start the active runtime without building or replacing its image.

        This is the ordinary stop/start path. It may restart an owned
        container for the same generation, or recreate that container from an
        already-built image after a full shutdown. Runtime changes remain the
        responsibility of apply().
        """
        if not spec.args_file.is_file():
            return ApplyResult(ok=False, messages=[f"Missing args file: {spec.args_file}"])
        if not self.ensure_network(spec.scope):
            return ApplyResult(ok=False, messages=["Failed to prepare runtime network."])

        current = self.inspect(spec.scope)
        if current.exists:
            if not current.owned:
                return ApplyResult(
                    ok=False,
                    messages=[
                        f"A container named '{spec.container_name}' exists but isn't "
                        "managed by Dotori (label mismatch); refusing to touch it."
                    ],
                )
            if (
                current.labels.get(LABEL_RUNTIME) != spec.runtime
                or current.labels.get(LABEL_GENERATION) != spec.generation_id
            ):
                return ApplyResult(
                    ok=False,
                    messages=[
                        "The existing runtime container does not match the active "
                        "generation; use Rebuild & Restart or Change LLM Model."
                    ],
                )
            if self._docker(
                "update", "--restart", "no", spec.container_name
            ).returncode != 0:
                message = "Failed to disable automatic restart for the LLM candidate."
                self._write_runtime_status(
                    spec,
                    "unavailable",
                    reason_code=LLM_UNAVAILABLE_START_FAILED,
                    message=message,
                    retryable=True,
                )
                return ApplyResult(
                    ok=False,
                    failure_code=LLM_UNAVAILABLE_START_FAILED,
                    messages=[message],
                )
            if not current.running:
                started = self._docker("start", spec.container_name).returncode == 0
                if not started:
                    message = "Failed to start the existing local LLM container."
                    self._write_runtime_status(
                        spec,
                        "unavailable",
                        reason_code=LLM_UNAVAILABLE_START_FAILED,
                        message=message,
                        retryable=True,
                    )
                    return ApplyResult(
                        ok=False,
                        failure_code=LLM_UNAVAILABLE_START_FAILED,
                        messages=[message],
                    )
        else:
            revision = self._image_revision(spec.image)
            if revision is None:
                message = (
                    f"Runtime image '{spec.image}' is not available; use "
                    "Rebuild & Restart or Change LLM Model."
                )
                self._write_runtime_status(
                    spec,
                    "unavailable",
                    reason_code=LLM_UNAVAILABLE_START_FAILED,
                    message=message,
                    retryable=True,
                )
                return ApplyResult(
                    ok=False,
                    failure_code=LLM_UNAVAILABLE_START_FAILED,
                    messages=[message],
                )
            if not self._run_candidate(spec, revision):
                message = "Failed to recreate the local LLM container from the existing image."
                self._write_runtime_status(
                    spec,
                    "unavailable",
                    reason_code=LLM_UNAVAILABLE_START_FAILED,
                    message=message,
                    retryable=True,
                )
                return ApplyResult(
                    ok=False,
                    failure_code=LLM_UNAVAILABLE_START_FAILED,
                    messages=[message],
                )

        healthy = self._wait_healthy(spec)
        if not healthy:
            failed_state = self.inspect(spec.scope)
            failure_code, failure_message = self._failure_details(
                failed_state,
                timed_out=failed_state.exists and failed_state.running,
            )
            runtime_logs = self._runtime_logs(spec.container_name)
            self._write_runtime_status(
                spec,
                "unavailable",
                reason_code=failure_code,
                message=failure_message,
                retryable=True,
            )
            if failed_state.exists and failed_state.owned:
                self._docker("rm", "-f", spec.container_name)
            messages = [failure_message]
            if runtime_logs:
                messages.append(f"Runtime logs:\n{runtime_logs}")
            return ApplyResult(
                ok=False,
                failure_code=failure_code,
                messages=messages,
            )
        verified, verify_message = self._verify_endpoints(spec)
        if not verified:
            failure_code = LLM_UNAVAILABLE_START_FAILED
            message = f"Runtime endpoint validation failed: {verify_message}"
            self._write_runtime_status(
                spec,
                "unavailable",
                reason_code=failure_code,
                message=message,
                retryable=True,
            )
            self._docker("rm", "-f", spec.container_name)
            return ApplyResult(
                ok=False,
                failure_code=failure_code,
                messages=[message],
            )
        if not self._promote_restart_policy(spec.container_name):
            failure_code = LLM_UNAVAILABLE_START_FAILED
            message = "The LLM validated, but its steady-state restart policy could not be enabled."
            self._write_runtime_status(
                spec,
                "unavailable",
                reason_code=failure_code,
                message=message,
                retryable=True,
            )
            self._docker("rm", "-f", spec.container_name)
            return ApplyResult(
                ok=False,
                failure_code=failure_code,
                messages=[message],
            )
        self._write_runtime_status(
            spec,
            "healthy",
            message="The local LLM passed health and endpoint validation.",
        )
        return ApplyResult(
            ok=True,
            messages=[
                f"Runtime '{spec.runtime}' resumed without rebuilding "
                f"(generation {spec.generation_id})."
            ],
        )

    def apply(
        self, spec: RuntimeSpec, *, rebuild_image: bool = True
    ) -> ApplyResult:
        from llm_installation.config_store import commit_active_runtime_config

        messages: list[str] = []
        if not spec.args_file.is_file():
            return ApplyResult(ok=False, messages=[f"Missing args file: {spec.args_file}"])

        self.ensure_network(spec.scope)

        if rebuild_image:
            built, revision = self.build(spec)
            if not built:
                return ApplyResult(ok=False, messages=["Failed to build runtime image."])
        else:
            revision = self._image_revision(spec.image)
            if revision is None:
                return ApplyResult(
                    ok=False,
                    messages=[
                        f"Runtime image '{spec.image}' is not available; calibration "
                        "cannot change concurrency without the active image."
                    ],
                )

        current = self.inspect(spec.scope)
        previous_runtime_status = load_runtime_status(
            spec.scope, repo_root=self.repo_root
        )
        if current.exists and not current.owned:
            return ApplyResult(
                ok=False,
                messages=[
                    f"A container named '{spec.container_name}' exists but isn't "
                    "managed by Dotori (label mismatch); refusing to touch it."
                ],
            )

        # Docker only allows one container per name, so the previous
        # container (if any) must be renamed out of the way -- not removed --
        # before the candidate can take the name. It's only actually deleted
        # once the candidate is confirmed healthy; on failure it's renamed
        # back and restarted.
        previous_name = f"{spec.container_name}-previous"
        has_previous = False
        if current.exists and current.owned:
            self._docker("stop", spec.container_name)
            has_previous = self._docker("rename", spec.container_name, previous_name).returncode == 0

        started = self._run_candidate(spec, revision)
        healthy = started and self._wait_healthy(spec)
        verified, verify_message = (False, "container failed to start") if not started else (
            self._verify_endpoints(spec) if healthy else (False, "health check timed out")
        )
        promoted = verified and self._promote_restart_policy(spec.container_name)
        if verified and not promoted:
            verified = False
            verify_message = "failed to enable the steady-state restart policy"

        if started and healthy and verified:
            commit_active_runtime_config(spec.scope, spec.generation_id, repo_root=self.repo_root)
            if has_previous:
                self._docker("rm", "-f", previous_name)
            self._write_runtime_status(
                spec,
                "healthy",
                message="The local LLM passed health and endpoint validation.",
            )
            messages.append(f"Runtime '{spec.runtime}' active (generation {spec.generation_id}).")
            return ApplyResult(ok=True, messages=messages)

        # Rollback.
        failed_state = self.inspect(spec.scope)
        if failed_state.exists and not failed_state.running:
            verify_message = "container exited before becoming healthy"
        failure_code, failure_message = self._failure_details(
            failed_state,
            timed_out=started and not healthy and failed_state.exists and failed_state.running,
        )
        messages.append(f"Candidate failed validation: {verify_message}")
        runtime_logs = self._runtime_logs(spec.container_name)
        if runtime_logs:
            messages.append(f"Runtime logs:\n{runtime_logs}")
        self._docker("rm", "-f", spec.container_name)
        rolled_back = False
        if has_previous:
            self._docker("rename", previous_name, spec.container_name)
            restart = self._docker("start", spec.container_name)
            rolled_back = restart.returncode == 0
            messages.append(
                "Restored previous runtime container."
                if rolled_back
                else "Could not restart previous runtime container; manual recovery needed."
            )
        if rolled_back:
            self._restore_runtime_status(spec.scope, previous_runtime_status)
        else:
            self._write_runtime_status(
                spec,
                "unavailable",
                reason_code=failure_code,
                message=failure_message,
                retryable=True,
            )
            messages.append(failure_message)
        messages.append(f"Generation retained at {spec.args_file.parent}")
        return ApplyResult(
            ok=False,
            rolled_back=rolled_back,
            failure_code=None if rolled_back else failure_code,
            messages=messages,
        )

    def stop(self, scope: str, remove_container: bool = True) -> bool:
        state = self.inspect(scope)
        if not state.exists:
            return True
        if not state.owned:
            return False
        container_name = SCOPE_CONFIG[scope].container_name
        self._docker("stop", container_name)
        if remove_container:
            self._docker("rm", "-f", container_name)
        return True

    def remove(self, scope: str) -> bool:
        """Stop+remove the owned container and delete the scope's whole
        config tree (active pointer + all generations). Model weight cache
        cleanup is a separate, repo-id-specific concern owned by
        llm_installation.cleanup.remove_model_weights."""
        stopped = self.stop(scope, remove_container=True)
        scope_dir = self.repo_root / "data" / "config" / "runtime_scopes" / scope
        if scope_dir.exists():
            import shutil

            shutil.rmtree(scope_dir, ignore_errors=True)
        return stopped

    def status(self, scope: str) -> dict:
        state = self.inspect(scope)
        return {
            "scope": scope,
            "container_name": SCOPE_CONFIG[scope].container_name,
            "network_name": SCOPE_CONFIG[scope].network_name,
            "exists": state.exists,
            "owned": state.owned,
            "running": state.running,
            "health": state.health,
            "oom_killed": state.oom_killed,
            "exit_code": state.exit_code,
            "restart_count": state.restart_count,
            "runtime": state.labels.get(LABEL_RUNTIME),
            "generation": state.labels.get(LABEL_GENERATION),
            "runtime_status": load_runtime_status(scope, repo_root=self.repo_root),
        }
