from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


CALIBRATION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CalibrationPolicy:
    max_ttft_p95_ratio: float
    max_total_p95_ratio: float
    min_marginal_throughput_gain: float

    def as_dict(self) -> dict[str, float]:
        return {
            "max_ttft_p95_ratio": self.max_ttft_p95_ratio,
            "max_total_p95_ratio": self.max_total_p95_ratio,
            "min_marginal_throughput_gain": self.min_marginal_throughput_gain,
        }


CALIBRATION_POLICIES = {
    # speed protects single-request responsiveness most aggressively.
    "speed": CalibrationPolicy(1.15, 1.20, 0.10),
    # balanced accepts some latency growth when aggregate throughput improves.
    "balanced": CalibrationPolicy(1.35, 1.40, 0.08),
    # quality normally resolves to a ceiling of one, but remains conservative
    # if a future catalog entry permits more than one sequence.
    "quality": CalibrationPolicy(1.25, 1.30, 0.08),
}


@dataclass(frozen=True)
class WorkloadItem:
    item_id: str
    phase: str
    question: str
    node_ids: tuple[str, ...]
    top_k: int = 3
    language: str = "ko"

    def request_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "question": self.question,
            "top_k": self.top_k,
            "language": self.language,
        }
        if self.node_ids:
            payload["node_ids"] = list(self.node_ids)
        return payload


def _require_non_empty_string(value: Any, *, field: str, line_number: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"workload line {line_number}: {field} must be a non-empty string")
    return value.strip()


def load_workload(path: Path, *, allow_unscoped: bool = False) -> list[WorkloadItem]:
    items: list[WorkloadItem] = []
    seen_ids: set[str] = set()
    warmup_questions: set[str] = set()
    measurement_questions: set[str] = set()

    with path.open(encoding="utf-8") as workload_file:
        for line_number, raw_line in enumerate(workload_file, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"workload line {line_number}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(payload, dict):
                raise ValueError(f"workload line {line_number}: expected a JSON object")

            item_id = _require_non_empty_string(
                payload.get("id"), field="id", line_number=line_number
            )
            if item_id in seen_ids:
                raise ValueError(f"workload line {line_number}: duplicate id {item_id!r}")
            seen_ids.add(item_id)

            phase = str(payload.get("phase") or "measure").strip().lower()
            if phase not in {"warmup", "measure"}:
                raise ValueError(
                    f"workload line {line_number}: phase must be 'warmup' or 'measure'"
                )
            question = _require_non_empty_string(
                payload.get("question"), field="question", line_number=line_number
            )
            node_ids_value = payload.get("node_ids") or []
            if not isinstance(node_ids_value, list) or not all(
                isinstance(node_id, str) and node_id.strip()
                for node_id in node_ids_value
            ):
                raise ValueError(
                    f"workload line {line_number}: node_ids must be a list of strings"
                )
            node_ids = tuple(node_id.strip() for node_id in node_ids_value)
            if not node_ids and not allow_unscoped:
                raise ValueError(
                    f"workload line {line_number}: node_ids is required; "
                    "use --allow-unscoped only when measuring the whole workspace"
                )

            top_k = payload.get("top_k", 3)
            if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 10:
                raise ValueError(f"workload line {line_number}: top_k must be between 1 and 10")
            language = str(payload.get("language") or "ko").strip().lower()
            if language not in {"ko", "en"}:
                raise ValueError(f"workload line {line_number}: language must be 'ko' or 'en'")

            item = WorkloadItem(
                item_id=item_id,
                phase=phase,
                question=question,
                node_ids=node_ids,
                top_k=top_k,
                language=language,
            )
            items.append(item)
            if phase == "warmup":
                warmup_questions.add(question)
            else:
                measurement_questions.add(question)

    if not items:
        raise ValueError("workload is empty")
    if not measurement_questions:
        raise ValueError("workload must contain at least one phase='measure' item")
    overlap = warmup_questions & measurement_questions
    if overlap:
        raise ValueError("warmup and measurement questions must be different")
    return items


def workload_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as workload_file:
        for chunk in iter(lambda: workload_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 3)


def _metric_values(records: list[dict[str, Any]], key: str) -> list[float]:
    values = []
    for record in records:
        metrics = record.get("performance_metrics")
        if not isinstance(metrics, dict):
            continue
        value = metrics.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return values


def summarize_step(
    *,
    concurrency: int,
    records: list[dict[str, Any]],
    resource_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    measured = [record for record in records if record.get("phase") == "measure"]
    successful = [record for record in measured if record.get("ok") is True]
    failed = len(measured) - len(successful)

    batch_windows_seconds = 0.0
    output_tokens = 0
    output_token_records = 0
    for round_id in sorted({record.get("round") for record in successful}):
        batch = [record for record in successful if record.get("round") == round_id]
        if not batch:
            continue
        started = min(float(record["started_offset_ms"]) for record in batch)
        completed = max(float(record["completed_offset_ms"]) for record in batch)
        batch_windows_seconds += max(0.0, completed - started) / 1000.0
        for record in batch:
            metrics = record.get("performance_metrics")
            value = metrics.get("output_token_count") if isinstance(metrics, dict) else None
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                output_tokens += value
                output_token_records += 1

    metrics_complete = bool(successful) and output_token_records == len(successful)
    aggregate_tps = (
        round(output_tokens / batch_windows_seconds, 3)
        if metrics_complete and batch_windows_seconds > 0
        else None
    )
    client_ttft = [
        float(record["ttft_ms"])
        for record in successful
        if isinstance(record.get("ttft_ms"), (int, float))
    ]
    client_total = [float(record["total_ms"]) for record in successful]
    tpot_ms = []
    for record in successful:
        metrics = record.get("performance_metrics")
        if not isinstance(metrics, dict):
            continue
        count = metrics.get("output_token_count")
        duration = metrics.get("llm_generation_after_first_token_ms")
        if (
            isinstance(count, (int, float))
            and not isinstance(count, bool)
            and count > 1
            and isinstance(duration, (int, float))
            and not isinstance(duration, bool)
        ):
            tpot_ms.append(float(duration) / (float(count) - 1))

    summary = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "concurrency": concurrency,
        "requests": len(measured),
        "successful": len(successful),
        "failed": failed,
        "metrics_complete": metrics_complete,
        "output_tokens": output_tokens,
        "measured_batch_window_seconds": round(batch_windows_seconds, 3),
        "aggregate_output_tokens_per_sec": aggregate_tps,
        "client_ttft_ms": {
            "p50": percentile(client_ttft, 0.50),
            "p95": percentile(client_ttft, 0.95),
            "p99": percentile(client_ttft, 0.99),
        },
        "client_total_ms": {
            "p50": percentile(client_total, 0.50),
            "p95": percentile(client_total, 0.95),
            "p99": percentile(client_total, 0.99),
        },
        "llm_ttft_ms": {
            "p50": percentile(_metric_values(successful, "llm_ttft_ms"), 0.50),
            "p95": percentile(_metric_values(successful, "llm_ttft_ms"), 0.95),
            "p99": percentile(_metric_values(successful, "llm_ttft_ms"), 0.99),
        },
        "tpot_ms": {
            "p50": percentile(tpot_ms, 0.50),
            "p95": percentile(tpot_ms, 0.95),
            "p99": percentile(tpot_ms, 0.99),
        },
        "resources": resource_summary or {},
    }
    return summary


def _positive_ratio(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    return float(value)


def select_serving_concurrency(
    step_summaries: list[dict[str, Any]], *, priority_preset: str
) -> dict[str, Any]:
    if priority_preset not in CALIBRATION_POLICIES:
        raise ValueError(f"unknown priority preset: {priority_preset}")
    if not step_summaries:
        raise ValueError("at least one calibration step is required")

    steps = sorted(step_summaries, key=lambda item: int(item["concurrency"]))
    baseline = steps[0]
    if int(baseline["concurrency"]) != 1:
        raise ValueError("calibration must include concurrency=1 as the baseline")
    policy = CALIBRATION_POLICIES[priority_preset]
    baseline_ttft = _positive_ratio((baseline.get("client_ttft_ms") or {}).get("p95"))
    baseline_total = _positive_ratio((baseline.get("client_total_ms") or {}).get("p95"))

    decisions: list[dict[str, Any]] = []
    selected = 1
    previous_tps: float | None = None
    for step in steps:
        concurrency = int(step["concurrency"])
        reasons: list[str] = []
        throughput = _positive_ratio(step.get("aggregate_output_tokens_per_sec"))
        if step.get("failed"):
            reasons.append("request_failures")
        if not step.get("metrics_complete") or throughput is None:
            reasons.append("incomplete_token_metrics")

        ttft = _positive_ratio((step.get("client_ttft_ms") or {}).get("p95"))
        total = _positive_ratio((step.get("client_total_ms") or {}).get("p95"))
        tpot = _positive_ratio((step.get("tpot_ms") or {}).get("p95"))
        if ttft is None or total is None:
            reasons.append("incomplete_latency_metrics")
        if tpot is None:
            reasons.append("incomplete_tpot_metrics")
        if concurrency > 1 and baseline_ttft and ttft:
            if ttft / baseline_ttft > policy.max_ttft_p95_ratio:
                reasons.append("ttft_p95_budget_exceeded")
        if concurrency > 1 and baseline_total and total:
            if total / baseline_total > policy.max_total_p95_ratio:
                reasons.append("total_p95_budget_exceeded")

        marginal_gain = None
        if concurrency > 1 and previous_tps and throughput:
            marginal_gain = throughput / previous_tps - 1.0
            if marginal_gain < policy.min_marginal_throughput_gain:
                reasons.append("marginal_throughput_gain_too_small")

        accepted = not reasons
        decisions.append(
            {
                "concurrency": concurrency,
                "accepted": accepted,
                "reasons": reasons,
                "marginal_throughput_gain": (
                    round(marginal_gain, 4) if marginal_gain is not None else None
                ),
            }
        )
        if not accepted:
            break
        selected = concurrency
        previous_tps = throughput

    return {
        "priority_preset": priority_preset,
        "policy": policy.as_dict(),
        "selected_serving_concurrency": selected,
        "decisions": decisions,
    }
