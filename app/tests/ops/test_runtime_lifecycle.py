import json
import subprocess

import pytest

from llm_installation.runtime_lifecycle import (
    HUGGINGFACE_CACHE_VOLUME,
    LLM_UNAVAILABLE_OOM,
    RuntimeLifecycleManager,
    build_runtime_spec,
    load_runtime_status,
)

pytestmark = pytest.mark.unit


def _spec(tmp_path, scope="production", runtime="llama.cpp", generation_id="gen1", model_id="test-model"):
    generation_dir = (
        tmp_path / "data" / "config" / "runtime_scopes" / scope / "generations" / generation_id
    )
    generation_dir.mkdir(parents=True)
    (generation_dir / "runtime.args").write_text("--hf-repo test/repo\n", encoding="utf-8")
    (generation_dir / "runtime.json").write_text(
        json.dumps(
            {"target": {"runtime": runtime, "model": model_id, "generation_id": generation_id}}
        ),
        encoding="utf-8",
    )
    return build_runtime_spec(scope, runtime, model_id, generation_id, repo_root=tmp_path)


class FakeDockerEnvironment:
    """Minimal in-memory Docker simulator injected as RuntimeLifecycleManager's
    runner, so apply()/stop()/remove()/status() can be exercised without a
    real Docker daemon."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.networks: set[str] = set()
        self.images: set[str] = set()
        self.containers: dict[str, dict] = {}
        self.health_after_start = "healthy"
        self.fail_candidate_oom = False

    def seed_container(
        self,
        name,
        *,
        labels,
        running=True,
        health="healthy",
        oom_killed=False,
        exit_code=0,
    ):
        self.containers[name] = {
            "labels": labels,
            "running": running,
            "health": health,
            "oom_killed": oom_killed,
            "exit_code": exit_code,
            "restart_count": 0,
            "restart_policy": "unless-stopped",
        }

    def __call__(self, args, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(args)
        assert args[0] == "docker"
        sub = args[1]

        if sub == "network":
            name = args[3]
            if args[2] == "inspect":
                return self._ok() if name in self.networks else self._fail()
            if args[2] == "create":
                self.networks.add(name)
                return self._ok()

        if sub == "build":
            # docker build -q -t <image> -f <dockerfile> <context>
            image = args[4]
            self.images.add(image)
            return self._ok(stdout="sha256:fakeimageid")

        if sub == "inspect":
            # docker inspect -f <fmt> <target>
            fmt, target = args[3], args[4]
            if target in self.images:
                return self._ok(stdout="sha256:fakeimageid")
            container = self.containers.get(target)
            if container is None:
                return self._fail()
            if fmt == "{{json .Config.Labels}}":
                return self._ok(stdout=json.dumps(container["labels"]))
            if fmt == "{{.State.Health.Status}}":
                return self._ok(stdout=container["health"] or "")
            running = "true" if container["running"] else "false"
            oom_killed = "true" if container.get("oom_killed") else "false"
            return self._ok(
                stdout=(
                    f"{running}|{container['health'] or ''}|{oom_killed}|"
                    f"{container.get('exit_code', 0)}|{container.get('restart_count', 0)}"
                )
            )

        if sub == "run":
            name = args[args.index("--name") + 1]
            labels = {}
            for i, a in enumerate(args):
                if a == "--label":
                    key, _, value = args[i + 1].partition("=")
                    labels[key] = value
            self.containers[name] = {
                "labels": labels,
                "running": not self.fail_candidate_oom,
                "health": None if self.fail_candidate_oom else self.health_after_start,
                "oom_killed": self.fail_candidate_oom,
                "exit_code": 137 if self.fail_candidate_oom else 0,
                "restart_count": 0,
                "restart_policy": args[args.index("--restart") + 1],
            }
            return self._ok()

        if sub == "update":
            container = self.containers.get(args[-1])
            if container is None:
                return self._fail()
            container["restart_policy"] = args[args.index("--restart") + 1]
            return self._ok()

        if sub == "rm":
            self.containers.pop(args[-1], None)
            return self._ok()

        if sub == "rename":
            old_name, new_name = args[2], args[3]
            if old_name not in self.containers:
                return self._fail()
            self.containers[new_name] = self.containers.pop(old_name)
            return self._ok()

        if sub == "stop":
            container = self.containers.get(args[-1])
            if container is not None:
                container["running"] = False
            return self._ok()

        if sub == "start":
            container = self.containers.get(args[-1])
            if container is None:
                return self._fail()
            container["running"] = True
            container["health"] = "healthy"
            container["oom_killed"] = False
            container["exit_code"] = 0
            return self._ok()

        if sub == "exec":
            return self._ok()

        if sub == "logs":
            return self._ok(stdout="runtime failed to initialize")

        if sub == "compose":
            return self._ok()

        raise AssertionError(f"Unhandled fake docker call: {args}")

    @staticmethod
    def _ok(stdout: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    @staticmethod
    def _fail(stderr: str = "not found") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def test_apply_fresh_install_starts_and_commits(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.apply(spec)

    assert result.ok is True
    assert env.containers[spec.container_name]["running"] is True
    run_call = next(c for c in env.calls if c[1] == "run")
    assert "--network-alias" in run_call
    assert "rag-runtime" in run_call
    assert f"com.dotori.scope={spec.scope}" in run_call
    assert "RAG_RUNTIME_ARGS_FILE=/runtime/runtime.args" in run_call
    assert f"{HUGGINGFACE_CACHE_VOLUME}:/root/.cache/huggingface" in run_call
    assert run_call[run_call.index("--restart") + 1] == "no"
    assert "900s" in run_call
    assert any(
        call[1:4] == ["update", "--restart", "unless-stopped"]
        for call in env.calls
    )
    build_call = next(c for c in env.calls if c[1] == "build")
    assert "llama.Dockerfile" in build_call[-2]
    assert build_call[-1].endswith("llm-runtime")

    active_path = tmp_path / "data" / "config" / "runtime_scopes" / "production" / "llm_runtime.json"
    assert active_path.exists()
    committed = json.loads(active_path.read_text(encoding="utf-8"))
    assert committed["target"]["generation_id"] == spec.generation_id


def test_apply_can_reuse_existing_image_for_calibration(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    env.images.add(spec.image)
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.apply(spec, rebuild_image=False)

    assert result.ok is True
    assert any(call[1] == "run" for call in env.calls)
    assert not any(call[1] == "build" for call in env.calls)


def test_apply_calibration_requires_existing_runtime_image(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.apply(spec, rebuild_image=False)

    assert result.ok is False
    assert "not available" in result.messages[0]
    assert not any(call[1] in {"build", "run"} for call in env.calls)


def test_apply_refuses_name_collision_with_foreign_container(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    env.seed_container(spec.container_name, labels={"some.other.tool": "true"}, running=True)
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.apply(spec)

    assert result.ok is False
    assert "label mismatch" in result.messages[0]
    assert spec.container_name in env.containers
    assert not any(c[1] == "run" for c in env.calls)


def test_apply_rolls_back_on_unhealthy_candidate(tmp_path):
    env = FakeDockerEnvironment()
    owned_labels = {
        "com.dotori.managed": "true",
        "com.dotori.component": "rag-runtime",
        "com.dotori.scope": "production",
        "com.dotori.runtime": "llama.cpp",
        "com.dotori.generation": "old-gen",
    }
    spec = _spec(tmp_path, generation_id="new-gen")
    env.seed_container(spec.container_name, labels=owned_labels, running=True, health="healthy")
    env.health_after_start = "unhealthy"
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.apply(spec)

    assert result.ok is False
    assert result.rolled_back is True
    assert env.containers[spec.container_name]["running"] is True
    active_path = tmp_path / "data" / "config" / "runtime_scopes" / "production" / "llm_runtime.json"
    assert not active_path.exists()


def test_apply_does_not_start_compose_services_when_first_runtime_fails(tmp_path):
    env = FakeDockerEnvironment()
    env.health_after_start = "unhealthy"
    spec = _spec(tmp_path)
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.apply(spec)

    assert result.ok is False
    assert result.rolled_back is False
    assert any("runtime failed to initialize" in message for message in result.messages)
    assert not any(call[1:5] == ["compose", "-f", "docker-compose.yml", "start"] for call in env.calls)


def test_apply_records_oom_and_leaves_runtime_stopped_without_restart_loop(tmp_path):
    env = FakeDockerEnvironment()
    env.fail_candidate_oom = True
    spec = _spec(tmp_path)
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.apply(spec)

    assert result.ok is False
    assert result.failure_code == LLM_UNAVAILABLE_OOM
    assert spec.container_name not in env.containers
    run_call = next(call for call in env.calls if call[1] == "run")
    assert run_call[run_call.index("--restart") + 1] == "no"
    assert not any(
        call[1:4] == ["update", "--restart", "unless-stopped"]
        for call in env.calls
    )
    persisted = load_runtime_status(spec.scope, repo_root=tmp_path)
    assert persisted["status"] == "unavailable"
    assert persisted["reason_code"] == LLM_UNAVAILABLE_OOM
    assert persisted["retryable"] is True


def test_resume_starts_matching_stopped_container_without_building(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    env.seed_container(
        spec.container_name,
        labels={
            "com.dotori.managed": "true",
            "com.dotori.component": "rag-runtime",
            "com.dotori.scope": spec.scope,
            "com.dotori.runtime": spec.runtime,
            "com.dotori.generation": spec.generation_id,
        },
        running=False,
        health=None,
    )
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.resume(spec)

    assert result.ok is True
    assert env.containers[spec.container_name]["running"] is True
    assert any(call[1] == "start" for call in env.calls)
    assert not any(call[1] == "build" for call in env.calls)
    assert not any(call[1] == "run" for call in env.calls)


def test_resume_recreates_missing_container_from_existing_image_without_building(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    env.images.add(spec.image)
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.resume(spec)

    assert result.ok is True
    assert env.containers[spec.container_name]["running"] is True
    assert any(call[1] == "run" for call in env.calls)
    assert not any(call[1] == "build" for call in env.calls)


def test_resume_requires_rebuild_when_runtime_image_is_missing(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.resume(spec)

    assert result.ok is False
    assert "Rebuild & Restart" in result.messages[0]
    assert not any(call[1] == "build" for call in env.calls)
    assert not any(call[1] == "run" for call in env.calls)


def test_resume_rejects_args_path_when_it_is_a_directory(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    spec.args_file.unlink()
    spec.args_file.mkdir()
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.resume(spec)

    assert result.ok is False
    assert "Missing args file" in result.messages[0]
    assert env.calls == []


def test_resume_refuses_container_from_another_generation(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    env.seed_container(
        spec.container_name,
        labels={
            "com.dotori.managed": "true",
            "com.dotori.component": "rag-runtime",
            "com.dotori.scope": spec.scope,
            "com.dotori.runtime": spec.runtime,
            "com.dotori.generation": "old-generation",
        },
        running=False,
        health=None,
    )
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.resume(spec)

    assert result.ok is False
    assert "active generation" in result.messages[0]
    assert not any(call[1] in {"start", "build", "run"} for call in env.calls)


def test_stop_refuses_when_container_not_owned(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    env.seed_container(spec.container_name, labels={}, running=True)
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    assert manager.stop("production") is False
    assert spec.container_name in env.containers


def test_stop_removes_owned_container(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    env.seed_container(
        spec.container_name,
        labels={
            "com.dotori.managed": "true",
            "com.dotori.component": "rag-runtime",
            "com.dotori.scope": "production",
        },
        running=True,
    )
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    assert manager.stop("production") is True
    assert spec.container_name not in env.containers


def test_status_reports_running_state(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    env.seed_container(
        spec.container_name,
        labels={
            "com.dotori.managed": "true",
            "com.dotori.component": "rag-runtime",
            "com.dotori.scope": "production",
            "com.dotori.runtime": "vllm",
            "com.dotori.generation": "gen42",
        },
        running=True,
        health="healthy",
    )
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    status = manager.status("production")

    assert status["running"] is True
    assert status["owned"] is True
    assert status["runtime"] == "vllm"
    assert status["generation"] == "gen42"
