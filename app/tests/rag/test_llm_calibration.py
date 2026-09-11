import json
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from config.enums import AIStatus, RAGStage
from document_ai.models import RAGJob, SearchJob
from document_ai.rag.streaming import create_rag_streaming_response
from llm_installation.calibration import (
    load_workload,
    select_serving_concurrency,
    summarize_step,
)
from llm_installation.config_store import stage_calibration_runtime_generation


pytestmark = pytest.mark.unit

User = get_user_model()


def test_load_workload_separates_warmup_and_measurement(tmp_path):
    workload_path = tmp_path / "workload.jsonl"
    workload_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "warmup-1",
                        "phase": "warmup",
                        "question": "도토리는 어떤 서비스인가?",
                        "node_ids": ["11111111-1111-1111-1111-111111111111"],
                    }
                ),
                json.dumps(
                    {
                        "id": "measure-1",
                        "phase": "measure",
                        "question": "런타임 선택 절차를 요약해줘",
                        "node_ids": ["11111111-1111-1111-1111-111111111111"],
                        "top_k": 5,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    items = load_workload(workload_path)

    assert [item.phase for item in items] == ["warmup", "measure"]
    assert items[1].top_k == 5
    assert items[1].request_payload()["node_ids"] == [
        "11111111-1111-1111-1111-111111111111"
    ]


def test_load_workload_rejects_prompt_reuse_between_warmup_and_measurement(tmp_path):
    workload_path = tmp_path / "workload.jsonl"
    rows = [
        {
            "id": "warmup-1",
            "phase": "warmup",
            "question": "같은 질문",
            "node_ids": ["node-1"],
        },
        {
            "id": "measure-1",
            "phase": "measure",
            "question": "같은 질문",
            "node_ids": ["node-1"],
        },
    ]
    workload_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be different"):
        load_workload(workload_path)


def _record(
    *,
    round_number,
    started,
    completed,
    tokens,
    ttft,
    total,
    generation_ms=900,
):
    return {
        "phase": "measure",
        "round": round_number,
        "ok": True,
        "started_offset_ms": started,
        "completed_offset_ms": completed,
        "ttft_ms": ttft,
        "total_ms": total,
        "performance_metrics": {
            "output_token_count": tokens,
            "llm_ttft_ms": ttft - 5,
            "llm_generation_after_first_token_ms": generation_ms,
        },
    }


def test_summarize_step_uses_shared_batch_window_for_aggregate_tps():
    records = [
        _record(
            round_number=1,
            started=0,
            completed=1000,
            tokens=50,
            ttft=100,
            total=1000,
        ),
        _record(
            round_number=1,
            started=20,
            completed=1100,
            tokens=50,
            ttft=120,
            total=1080,
        ),
        _record(
            round_number=2,
            started=2000,
            completed=3000,
            tokens=60,
            ttft=110,
            total=1000,
        ),
        _record(
            round_number=2,
            started=2020,
            completed=3100,
            tokens=40,
            ttft=130,
            total=1080,
        ),
    ]

    summary = summarize_step(concurrency=2, records=records)

    assert summary["output_tokens"] == 200
    assert summary["measured_batch_window_seconds"] == 2.2
    assert summary["aggregate_output_tokens_per_sec"] == pytest.approx(90.909)
    assert summary["metrics_complete"] is True
    assert summary["tpot_ms"]["p50"] is not None
    assert "semaphore_wait_ms" not in summary


def _step(concurrency, throughput, ttft=100, total=1000):
    return {
        "concurrency": concurrency,
        "failed": 0,
        "metrics_complete": True,
        "aggregate_output_tokens_per_sec": throughput,
        "client_ttft_ms": {"p95": ttft},
        "client_total_ms": {"p95": total},
        "tpot_ms": {"p95": 20},
    }


def test_selection_stops_when_marginal_throughput_gain_is_too_small():
    result = select_serving_concurrency(
        [_step(1, 40), _step(2, 70, ttft=120, total=1200), _step(3, 73)],
        priority_preset="balanced",
    )

    assert result["selected_serving_concurrency"] == 2
    assert result["decisions"][-1]["reasons"] == [
        "marginal_throughput_gain_too_small"
    ]


def _write_active_runtime(tmp_path, *, runtime="llama.cpp"):
    scope_dir = tmp_path / "data" / "config" / "runtime_scopes" / "production"
    generation_dir = scope_dir / "generations" / "source-gen"
    generation_dir.mkdir(parents=True)
    payload = {
        "version": 8,
        "target": {
            "runtime": runtime,
            "model": "dotori-test-model",
            "generation_id": "source-gen",
            "serving_profile": {
                "context_length": 4096,
                "safe_concurrency_ceiling": 4,
                "serving_concurrency": 1,
            },
        },
    }
    (scope_dir / "llm_runtime.json").write_text(json.dumps(payload), encoding="utf-8")
    if runtime == "llama.cpp":
        args = ["--model", "test.gguf", "--ctx-size", "4096", "--parallel", "1"]
    else:
        args = [
            "--model",
            "test/model",
            "--max-model-len",
            "4096",
            "--max-num-seqs",
            "1",
            "--max-num-batched-tokens",
            "4096",
        ]
    (generation_dir / "runtime.args").write_text("\n".join(args) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        ("llama.cpp", {"--parallel": "3", "--ctx-size": "12288"}),
        (
            "vllm",
            {"--max-num-seqs": "3", "--max-num-batched-tokens": "12288"},
        ),
    ],
)
def test_stage_calibration_generation_updates_runtime_capacity(tmp_path, runtime, expected):
    _write_active_runtime(tmp_path, runtime=runtime)

    generation_id, generation_dir = stage_calibration_runtime_generation(
        scope="production",
        serving_concurrency=3,
        calibration_run_id="run-123",
        calibration_status="running",
        repo_root=tmp_path,
    )

    payload = json.loads((generation_dir / "runtime.json").read_text(encoding="utf-8"))
    profile = payload["target"]["serving_profile"]
    assert payload["target"]["generation_id"] == generation_id
    assert profile["safe_concurrency_ceiling"] == 4
    assert profile["serving_concurrency"] == 3
    assert profile["calibration_status"] == "running"
    assert profile["calibration_original_generation_id"] == "source-gen"
    args = (generation_dir / "runtime.args").read_text(encoding="utf-8").splitlines()
    for flag, value in expected.items():
        assert args[args.index(flag) + 1] == value


def test_stage_calibration_generation_rejects_value_above_safe_ceiling(tmp_path):
    _write_active_runtime(tmp_path)

    with pytest.raises(ValueError, match="safe_concurrency_ceiling=4"):
        stage_calibration_runtime_generation(
            scope="production",
            serving_concurrency=5,
            calibration_run_id="run-123",
            calibration_status="running",
            repo_root=tmp_path,
        )


@pytest.mark.django_db(transaction=True)
def test_rag_stream_terminal_event_exposes_persisted_performance_metrics():
    user = User.objects.create_user(
        email="calibration-stream@example.com",
        password="password",
        is_active=True,
        email_verified=True,
    )

    def complete_search(job_id, max_retries=0):
        SearchJob.objects.filter(pk=job_id).update(
            status=AIStatus.COMPLETED,
            results=[],
            completed_at=timezone.now(),
        )
        return {"status": "success", "job_id": job_id}

    def complete_generation(job_id, on_token=None):
        if on_token:
            on_token("측정 답변")
        RAGJob.objects.filter(pk=job_id).update(
            answer="측정 답변",
            citations=[],
            status=AIStatus.COMPLETED,
            stage=RAGStage.COMPLETED,
            completed_at=timezone.now(),
            performance_metrics={
                "output_token_count": 12,
                "llm_ttft_ms": 100,
            },
        )
        return {"status": "success", "job_id": job_id}

    with patch(
        "document_ai.search.execution.perform_vector_search_sync",
        side_effect=complete_search,
    ), patch(
        "document_ai.rag.generation._build_rag_context",
        return_value=("", []),
    ), patch(
        "document_ai.rag.generation.generate_rag_response_sync",
        side_effect=complete_generation,
    ):
        response = create_rag_streaming_response(
            owner=user,
            question="Dotori 구조를 요약해줘",
            retrieval_query="Dotori 구조",
            top_k=3,
            threshold=None,
            language="ko",
            requested_node_ids=[],
            scoped_node_ids=[],
            llm_snapshot={
                "llm_endpoint_name": "Test runtime",
                "llm_base_url": "http://rag-runtime:8080",
                "llm_model": "test-model",
            },
        )
        events = [
            json.loads(chunk.decode("utf-8"))
            for chunk in response.streaming_content
        ]

    completed = next(event for event in events if event["type"] == "completed")
    started = next(event for event in events if event["type"] == "started")
    assert started["llm_target"] == "server"
    assert started["llm_model"] == "test-model"
    assert completed["performance_metrics"]["output_token_count"] == 12
    assert completed["performance_metrics"]["llm_ttft_ms"] == 100
