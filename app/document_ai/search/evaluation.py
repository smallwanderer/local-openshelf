from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from django.db import transaction
from django.utils import timezone

from config.enums import AIStatus
from document_ai.embedding.embeding_models import EmbeddingResult
from document_ai.search.profiles import (
    RetrievalProfileError,
    _active_for_workspace,
    get_effective_retrieval_config,
    profile_threshold_to_retriever,
    retrieval_tuning_params,
)


def dense_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def sparse_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items())


def rank_documents(
    query_embedding: EmbeddingResult,
    chunk_records: list[dict],
    chunk_embeddings: list[EmbeddingResult],
    top_k: int,
    dense_weight: float,
    sparse_weight: float,
) -> list[dict]:
    chunk_hits = []
    for record, embedding in zip(chunk_records, chunk_embeddings):
        dense_score = dense_similarity(query_embedding.dense_vector, embedding.dense_vector)
        sparse_score = sparse_similarity(query_embedding.sparse_vector, embedding.sparse_vector)
        hybrid_score = (dense_weight * dense_score) + (sparse_weight * sparse_score)
        chunk_hits.append(
            {
                "node_id": record["node_id"],
                "node_name": record["node_name"],
                "hybrid_score": hybrid_score,
            }
        )

    chunk_hits.sort(key=lambda item: item["hybrid_score"], reverse=True)
    chunk_hits = chunk_hits[:top_k]

    grouped_hits = defaultdict(list)
    for hit in chunk_hits:
        grouped_hits[hit["node_id"]].append(hit)

    documents = []
    for node_id, hits in grouped_hits.items():
        hits.sort(key=lambda item: item["hybrid_score"], reverse=True)
        top_hits = hits[:3]
        combined_score = sum(max(item["hybrid_score"], 0.0) for item in top_hits)
        doc_score = math.log10(1.0 + combined_score)
        documents.append(
            {
                "node_id": node_id,
                "node_name": top_hits[0]["node_name"],
                "doc_score": doc_score,
            }
        )

    documents.sort(key=lambda item: item["doc_score"], reverse=True)
    return documents


def validate_dataset_items(axis: str, items: Any) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise RetrievalProfileError(
            "DATASET_VALIDATION_FAILED", "Dataset must be a non-empty array.", 400,
        )
    if axis != "retrieval":
        raise RetrievalProfileError(
            "DATASET_VALIDATION_FAILED", f"No dataset validator for axis {axis}.", 400,
        )

    errors: dict[str, list[str]] = {}
    normalized = []
    for index, item in enumerate(items):
        key = f"item[{index}]"
        if not isinstance(item, dict):
            errors.setdefault(key, []).append("Must be an object.")
            continue
        query = item.get("query")
        expected_ids = item.get("expected_node_ids")
        if not isinstance(query, str) or not query.strip():
            errors.setdefault(key, []).append("A non-empty 'query' string is required.")
        if not isinstance(expected_ids, list) or not expected_ids or not all(
            isinstance(node_id, str) and node_id.strip() for node_id in expected_ids
        ):
            errors.setdefault(key, []).append("A non-empty 'expected_node_ids' string array is required.")
        if key not in errors:
            normalized.append({"query": query.strip(), "expected_node_ids": [str(n) for n in expected_ids]})
    if errors:
        raise RetrievalProfileError("DATASET_VALIDATION_FAILED", "Dataset validation failed.", 400, errors)
    return normalized


def run_retrieval_evaluation(workspace, *, config: dict[str, Any], items: list[dict]) -> dict[str, Any]:
    """Score a dataset against the production retrieval path.

    This calls search_documents_sync rather than ranking chunks directly: the
    candidate funnel (candidate_multiplier, per_node_candidate_cap, threshold)
    and the document pooling are exactly what a profile change alters, so
    scoring chunks standalone would report metrics for an algorithm the
    product never runs.
    """
    from document_ai.models import DocumentChunk
    from document_ai.search.execution import search_documents_sync

    top_k = int(config.get("search_top_k", 5))
    dense_weight = float(config["dense_weight"])
    sparse_weight = float(config["sparse_weight"])

    chunk_count = DocumentChunk.objects.filter(
        parse_result__status=AIStatus.COMPLETED,
        parse_result__node__workspace=workspace,
    ).count()
    if not chunk_count:
        raise RetrievalProfileError(
            "EVALUATION_CORPUS_EMPTY",
            "No completed document chunks were found for this workspace.",
            400,
        )

    threshold = profile_threshold_to_retriever(config.get("retrieval_threshold"))
    tuning_params = retrieval_tuning_params(config)

    hits_at_1 = 0
    hits_at_k = 0
    reciprocal_rank_sum = 0.0
    search_ms_total = 0.0
    per_query = []

    for item in items:
        results, metrics = search_documents_sync(
            owner=None,
            workspace=workspace,
            query=item["query"],
            top_k=top_k,
            threshold=threshold,
            tuning_params=tuning_params,
        )
        expected_ids = set(item["expected_node_ids"])
        ranked_node_ids = [result["node_id"] for result in results]
        hit_at_1 = bool(ranked_node_ids) and ranked_node_ids[0] in expected_ids
        matched_rank = next(
            (rank for rank, node_id in enumerate(ranked_node_ids, start=1) if node_id in expected_ids),
            None,
        )
        if hit_at_1:
            hits_at_1 += 1
        if matched_rank is not None:
            hits_at_k += 1
            reciprocal_rank_sum += 1.0 / matched_rank
        search_ms = float(metrics.get("request_search_ms") or 0.0)
        search_ms_total += search_ms
        per_query.append(
            {
                "query": item["query"],
                "hit_at_1": hit_at_1,
                "matched_rank": matched_rank,
                "search_ms": round(search_ms, 2),
            }
        )

    total_queries = len(items)
    return {
        "queries": total_queries,
        "chunk_count": chunk_count,
        "top_k": top_k,
        "dense_weight": dense_weight,
        "sparse_weight": sparse_weight,
        "candidate_multiplier": int(config.get("candidate_multiplier", 0)),
        "per_node_candidate_cap": int(config.get("per_node_candidate_cap", 0)),
        "hit_rate_at_1": round(hits_at_1 / total_queries, 4),
        "hit_rate_at_k": round(hits_at_k / total_queries, 4),
        "mrr_at_k": round(reciprocal_rank_sum / total_queries, 4),
        "avg_search_ms": round(search_ms_total / total_queries, 2),
        "per_query": per_query,
    }


def _serialize_dataset(dataset) -> dict[str, Any]:
    return {
        "uid": str(dataset.uid),
        "axis": dataset.axis,
        "name": dataset.name,
        "item_count": dataset.item_count,
        "created_at": dataset.created_at.isoformat(),
    }


def _serialize_run(run) -> dict[str, Any]:
    return {
        "uid": str(run.uid),
        "axis": run.axis,
        "dataset_uid": str(run.dataset.uid),
        "status": run.status,
        "metrics": run.metrics,
        "error_message": run.error_message,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def list_evaluation_datasets(workspace, *, axis: str) -> list[dict[str, Any]]:
    from workspaces.models import WorkspaceEvaluationDataset

    datasets = WorkspaceEvaluationDataset.objects.filter(workspace=workspace, axis=axis)
    return [_serialize_dataset(dataset) for dataset in datasets]


def create_evaluation_dataset(workspace, *, actor, axis: str, name: str, items: Any) -> dict[str, Any]:
    from workspaces.models import WorkspaceEvaluationDataset, WorkspaceQualityProfileRevision

    if axis not in dict(WorkspaceQualityProfileRevision.AXIS_CHOICES):
        raise RetrievalProfileError("INVALID_REQUEST", "Unknown axis.", 400)
    name = str(name or "").strip()
    if not name or len(name) > 200:
        raise RetrievalProfileError("INVALID_REQUEST", "A dataset name (max 200 chars) is required.", 400)

    normalized_items = validate_dataset_items(axis, items)
    dataset = WorkspaceEvaluationDataset.objects.create(
        workspace=workspace,
        axis=axis,
        name=name,
        items=normalized_items,
        item_count=len(normalized_items),
        created_by=actor,
    )
    return {"ok": True, "dataset": _serialize_dataset(dataset)}


def _start_evaluation(workspace, *, actor, axis: str, expected_revision: int, dataset_uid: str) -> dict[str, Any]:
    from workspaces.models import WorkspaceEvaluationDataset, WorkspaceQualityEvaluationRun, WorkspaceQualityProfileRevision

    with transaction.atomic():
        from workspaces.models import Workspace

        locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
        _active_for_workspace(locked_workspace, actor=actor, lock=True)  # ensures + locks the active row against a concurrent apply
        draft = WorkspaceQualityProfileRevision.objects.select_for_update().filter(
            workspace=locked_workspace,
            status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
        ).first()
        current_revision = draft.revision if draft else 0
        if draft is None or expected_revision != current_revision:
            raise RetrievalProfileError(
                "PROFILE_REVISION_CONFLICT",
                "The draft was changed by another request.",
                409,
                {"expected_revision": expected_revision, "current_revision": current_revision},
            )
        dataset = WorkspaceEvaluationDataset.objects.filter(
            workspace=locked_workspace, uid=dataset_uid, axis=axis,
        ).first()
        if dataset is None:
            raise RetrievalProfileError("NOT_FOUND", "No such evaluation dataset.", 404)

        if axis == WorkspaceQualityProfileRevision.AXIS_RETRIEVAL:
            config_snapshot = get_effective_retrieval_config(locked_workspace)
            config_snapshot.update(draft.retrieval_config or {})
        else:
            raise RetrievalProfileError(
                "EVALUATION_UNSUPPORTED_AXIS", f"No evaluator is available yet for axis {axis}.", 409,
            )

        run = WorkspaceQualityEvaluationRun.objects.create(
            workspace=locked_workspace,
            axis=axis,
            dataset=dataset,
            profile_revision=draft,
            tested_revision_number=draft.revision,
            config_snapshot=config_snapshot,
            status=WorkspaceQualityEvaluationRun.STATUS_PENDING,
            created_by=actor,
        )
    from document_ai.orchestration import enqueue_retrieval_evaluation

    enqueue_retrieval_evaluation(str(run.uid))
    return {"ok": True, "run": _serialize_run(run)}


def start_retrieval_evaluation(workspace, *, actor, expected_revision, dataset_uid) -> dict[str, Any]:
    from workspaces.models import WorkspaceQualityProfileRevision

    return _start_evaluation(
        workspace,
        actor=actor,
        axis=WorkspaceQualityProfileRevision.AXIS_RETRIEVAL,
        expected_revision=expected_revision,
        dataset_uid=dataset_uid,
    )


def get_evaluation_run(workspace, *, run_uid) -> dict[str, Any]:
    from workspaces.models import WorkspaceQualityEvaluationRun

    run = WorkspaceQualityEvaluationRun.objects.select_related("dataset").filter(
        workspace=workspace, uid=run_uid,
    ).first()
    if run is None:
        raise RetrievalProfileError("NOT_FOUND", "No such evaluation run.", 404)
    return {"ok": True, "run": _serialize_run(run)}


def resolve_verified_evaluation_run(workspace, *, axis: str, draft, evaluation_run_uid):
    """Look up a succeeded evaluation run and confirm it still matches the draft being applied."""
    from workspaces.models import WorkspaceQualityEvaluationRun

    run = WorkspaceQualityEvaluationRun.objects.filter(
        workspace=workspace, uid=evaluation_run_uid, axis=axis,
    ).first()
    if run is None:
        raise RetrievalProfileError("NOT_FOUND", "No such evaluation run.", 404)
    if run.status != WorkspaceQualityEvaluationRun.STATUS_SUCCEEDED:
        raise RetrievalProfileError(
            "EVALUATION_REQUIRED", "The evaluation run has not succeeded.", 409, {"status": run.status},
        )
    if run.profile_revision_id != draft.id or run.tested_revision_number != draft.revision:
        raise RetrievalProfileError(
            "EVALUATION_STALE",
            "The draft changed after this evaluation run. Run a new evaluation before applying.",
            409,
        )
    return run


def execute_quality_evaluation_run(run_uid: str) -> None:
    """Runs the evaluation and persists its result. Invoked from the Celery task
    in document_ai.tasks (Celery's autodiscover_tasks() only imports each app's
    tasks.py, so the @shared_task wrapper has to live there, not here)."""
    from workspaces.models import WorkspaceQualityEvaluationRun

    run = WorkspaceQualityEvaluationRun.objects.select_related("workspace", "dataset").get(uid=run_uid)
    if run.status == WorkspaceQualityEvaluationRun.STATUS_SUCCEEDED:
        return
    run.status = WorkspaceQualityEvaluationRun.STATUS_RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])

    try:
        if run.axis == "retrieval":
            metrics = run_retrieval_evaluation(
                run.workspace, config=run.config_snapshot, items=run.dataset.items,
            )
        else:
            raise RetrievalProfileError(
                "EVALUATION_UNSUPPORTED_AXIS", f"No evaluator is available for axis {run.axis}.", 409,
            )
    except Exception as exc:  # noqa: BLE001 - persist any failure onto the run row
        run.status = WorkspaceQualityEvaluationRun.STATUS_FAILED
        run.error_message = str(exc)[:500]
    else:
        run.status = WorkspaceQualityEvaluationRun.STATUS_SUCCEEDED
        run.metrics = metrics
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "metrics", "error_message", "finished_at"])

    if run.status == WorkspaceQualityEvaluationRun.STATUS_SUCCEEDED and run.profile_revision_id:
        from workspaces.models import WorkspaceQualityProfileRevision

        with transaction.atomic():
            draft = WorkspaceQualityProfileRevision.objects.select_for_update().filter(
                pk=run.profile_revision_id,
                status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
                revision=run.tested_revision_number,
            ).first()
            if draft is not None:
                draft.validation_state = "verified"
                draft.applied_evaluation_run_uid = run.uid
                draft.save(update_fields=["validation_state", "applied_evaluation_run_uid", "updated_at"])
