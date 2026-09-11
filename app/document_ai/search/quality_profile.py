from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from document_ai.rag.prompt_profiles import (
    FIXED_CONTRACT_DESCRIPTION,
    PROMPT_CONTRACT_VERSION,
    PROMPT_MAX_CHARS,
    PROMPT_ROUTES,
    _effective_prompt_policy,
    _serialize_route,
    prompt_defaults,
    validate_prompt_policy,
)
from document_ai.search.profiles import (
    GENERATION_SCHEMA,
    RETRIEVAL_SCHEMA,
    SCHEMA_VERSION,
    RetrievalProfileError,
    _active_for_workspace,
    _effective,
    _effective_generation,
    _sparse_overrides,
    generation_defaults,
    retrieval_defaults,
    validate_generation_config,
    validate_retrieval_config,
)
from workspaces.models import WorkspaceQualityProfileRevision

AXIS_RETRIEVAL = WorkspaceQualityProfileRevision.AXIS_RETRIEVAL
AXIS_GENERATION = WorkspaceQualityProfileRevision.AXIS_GENERATION
AXIS_PROMPT_POLICY = WorkspaceQualityProfileRevision.AXIS_PROMPT_POLICY


def _axis_payload(config: dict[str, Any] | None, *, defaults_fn, schema, active_config=None) -> dict[str, Any]:
    effective = defaults_fn()
    effective.update(config or {})
    if active_config is not None:
        active_effective = defaults_fn()
        active_effective.update(active_config or {})
        changed_fields = [key for key in schema if effective[key] != active_effective[key]]
    else:
        changed_fields = list(config or {})
    return {"overrides": config or {}, "effective": effective, "changed_fields": changed_fields}


def _prompt_axis_payload(policy: dict[str, Any] | None, *, active_policy=None) -> dict[str, Any]:
    effective = _effective_prompt_policy(policy)
    if active_policy is not None:
        active_effective = _effective_prompt_policy(active_policy)
        changed_fields = [route for route in PROMPT_ROUTES if effective[route] != active_effective[route]]
    else:
        changed_fields = list(policy or {})
    return {
        "overrides": {route: _serialize_route(value) for route, value in (policy or {}).items()},
        "effective": {route: _serialize_route(value) for route, value in effective.items()},
        "changed_fields": changed_fields,
    }


def _serialize_row(revision, *, active=None) -> dict[str, Any]:
    is_draft = revision.status == WorkspaceQualityProfileRevision.STATUS_DRAFT and active is not None
    creator = revision.created_by
    return {
        "uid": str(revision.uid),
        "version": revision.version,
        "revision": revision.revision,
        "status": revision.status,
        "changed_axes": list(revision.changed_axes or []),
        "based_on_uid": str(revision.based_on.uid) if revision.based_on_id else None,
        "retrieval": _axis_payload(
            revision.retrieval_config, defaults_fn=retrieval_defaults, schema=RETRIEVAL_SCHEMA,
            active_config=active.retrieval_config if is_draft else None,
        ),
        "generation": _axis_payload(
            revision.generation_config, defaults_fn=generation_defaults, schema=GENERATION_SCHEMA,
            active_config=active.generation_config if is_draft else None,
        ),
        "prompt_policy": _prompt_axis_payload(
            revision.prompt_policy, active_policy=active.prompt_policy if is_draft else None,
        ),
        "validation": {
            "state": revision.validation_state,
            "last_run_uid": str(revision.applied_evaluation_run_uid) if revision.applied_evaluation_run_uid else None,
            "warnings": revision.validation_warnings,
        },
        "created_by": ({"id": creator.id, "display_name": creator.display_name} if creator else None),
        "created_at": revision.created_at.isoformat(),
        "updated_at": revision.updated_at.isoformat(),
        "applied_at": revision.applied_at.isoformat() if revision.applied_at else None,
        "note": revision.note,
    }


def quality_profile_envelope(workspace, *, actor, can_edit: bool = True) -> dict[str, Any]:
    with transaction.atomic():
        from workspaces.models import Workspace

        locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
        active = _active_for_workspace(locked_workspace, actor=actor)
    draft = WorkspaceQualityProfileRevision.objects.filter(
        workspace=workspace,
        status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
    ).select_related("created_by", "based_on").first()
    return {
        "ok": True,
        "workspace_uid": str(workspace.uid),
        "active": _serialize_row(active),
        "draft": _serialize_row(draft, active=active) if draft else None,
        "defaults": {
            "retrieval": retrieval_defaults(),
            "generation": generation_defaults(),
            "prompt_policy": {route: _serialize_route(value) for route, value in prompt_defaults().items()},
        },
        "schema": {"retrieval": RETRIEVAL_SCHEMA, "generation": GENERATION_SCHEMA},
        "capabilities": {
            "schema_version": SCHEMA_VERSION,
            "server_prompt_contract_version": PROMPT_CONTRACT_VERSION,
            "max_instruction_chars": PROMPT_MAX_CHARS,
            "import_extensions": [".txt", ".md"],
        },
        "permissions": {"can_read": True, "can_edit": can_edit, "can_apply": can_edit},
        "fixed_contract": FIXED_CONTRACT_DESCRIPTION,
        "provider_disclosure": "The same assembled system instruction is used for local and external LLM endpoints.",
    }


def _validate_section_shape(name: str, section: dict[str, Any] | None, *, schema=None):
    if section is None:
        return {}, []
    overrides = section.get("overrides", {})
    reset_fields = section.get("reset_fields", [])
    if not isinstance(overrides, dict) or not isinstance(reset_fields, list) or not all(isinstance(item, str) for item in reset_fields):
        raise RetrievalProfileError("INVALID_REQUEST", f"Invalid {name} payload shape.", 400)
    if schema is not None:
        duplicate_fields = sorted(set(overrides) & set(reset_fields))
        unknown_fields = sorted((set(overrides) | set(reset_fields)) - set(schema))
        if duplicate_fields or unknown_fields:
            raise RetrievalProfileError(
                "INVALID_REQUEST",
                f"Invalid {name} fields.",
                400,
                {"duplicate_fields": duplicate_fields, "unknown_fields": unknown_fields},
            )
    return overrides, reset_fields


def save_quality_profile_draft(
    workspace, *, actor, expected_revision,
    retrieval: dict[str, Any] | None = None,
    generation: dict[str, Any] | None = None,
    prompt_policy: dict[str, Any] | None = None,
    note: str,
) -> dict[str, Any]:
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise RetrievalProfileError("INVALID_REQUEST", "expected_revision must be a non-negative integer.", 400)
    if not isinstance(note, str) or len(note) > 500:
        raise RetrievalProfileError("INVALID_REQUEST", "note must be at most 500 characters.", 400)
    if retrieval is None and generation is None and prompt_policy is None:
        raise RetrievalProfileError("INVALID_REQUEST", "At least one of retrieval, generation, or prompt_policy must be provided.", 400)

    retrieval_overrides, retrieval_reset_fields = _validate_section_shape("retrieval", retrieval, schema=RETRIEVAL_SCHEMA)
    generation_overrides, generation_reset_fields = _validate_section_shape("generation", generation, schema=GENERATION_SCHEMA)
    if prompt_policy is not None:
        if not isinstance(prompt_policy, dict):
            raise RetrievalProfileError("INVALID_REQUEST", "prompt_policy must be an object.", 400)
        unknown_routes = sorted(set(prompt_policy) - set(PROMPT_ROUTES))
        if unknown_routes:
            raise RetrievalProfileError("INVALID_REQUEST", "Invalid prompt policy routes.", 400, {"unknown_routes": unknown_routes})

    with transaction.atomic():
        from workspaces.models import Workspace

        locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
        active = _active_for_workspace(locked_workspace, actor=actor, lock=True)
        draft = WorkspaceQualityProfileRevision.objects.select_for_update().filter(
            workspace=locked_workspace,
            status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
        ).first()
        current_revision = draft.revision if draft else 0
        if expected_revision != current_revision:
            raise RetrievalProfileError(
                "PROFILE_REVISION_CONFLICT", "The draft was changed by another request.", 409,
                {"expected_revision": expected_revision, "current_revision": current_revision},
            )

        changed_axes = set(draft.changed_axes) if draft else set()
        retrieval_config = draft.retrieval_config if draft else active.retrieval_config
        generation_config = draft.generation_config if draft else active.generation_config
        prompt_policy_config = draft.prompt_policy if draft else active.prompt_policy
        warnings = list(draft.validation_warnings) if draft else []

        if retrieval is not None:
            defaults = retrieval_defaults()
            candidate = _effective(retrieval_config)
            candidate.update(retrieval_overrides)
            for field in retrieval_reset_fields:
                candidate[field] = defaults[field]
            warnings = validate_retrieval_config(candidate)
            active_effective = _effective(active.retrieval_config)
            if all(candidate[key] == active_effective[key] for key in RETRIEVAL_SCHEMA):
                changed_axes.discard(AXIS_RETRIEVAL)
            else:
                changed_axes.add(AXIS_RETRIEVAL)
            retrieval_config = _sparse_overrides(candidate, defaults)

        if generation is not None:
            defaults = generation_defaults()
            candidate = _effective_generation(generation_config)
            candidate.update(generation_overrides)
            for field in generation_reset_fields:
                candidate[field] = defaults[field]
            validate_generation_config(candidate)
            active_effective = _effective_generation(active.generation_config)
            if all(candidate[key] == active_effective[key] for key in GENERATION_SCHEMA):
                changed_axes.discard(AXIS_GENERATION)
            else:
                changed_axes.add(AXIS_GENERATION)
            generation_config = _sparse_overrides(candidate, defaults)

        if prompt_policy is not None:
            from copy import deepcopy

            candidate = _effective_prompt_policy(prompt_policy_config)
            candidate.update(deepcopy(prompt_policy))
            candidate = validate_prompt_policy(candidate)
            active_effective = _effective_prompt_policy(active.prompt_policy)
            if candidate == active_effective:
                changed_axes.discard(AXIS_PROMPT_POLICY)
            else:
                changed_axes.add(AXIS_PROMPT_POLICY)
            defaults = prompt_defaults()
            prompt_policy_config = {route: value for route, value in candidate.items() if value != defaults[route]}

        if not changed_axes:
            raise RetrievalProfileError("PROFILE_HAS_NO_CHANGES", "The quality profile has no changes.", 409)

        if draft is None:
            draft = WorkspaceQualityProfileRevision.objects.create(
                workspace=locked_workspace,
                version=active.version + 1,
                revision=1,
                status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
                changed_axes=sorted(changed_axes),
                retrieval_config=retrieval_config,
                generation_config=generation_config,
                prompt_policy=prompt_policy_config,
                validation_state="not_run",
                validation_warnings=warnings,
                based_on=active,
                created_by=actor,
                note=note.strip(),
            )
        else:
            draft.revision += 1
            draft.changed_axes = sorted(changed_axes)
            draft.retrieval_config = retrieval_config
            draft.generation_config = generation_config
            draft.prompt_policy = prompt_policy_config
            draft.validation_state = "not_run"
            draft.validation_warnings = warnings
            draft.applied_evaluation_run_uid = None
            draft.note = note.strip()
            draft.save(update_fields=[
                "revision", "changed_axes", "retrieval_config", "generation_config", "prompt_policy",
                "validation_state", "validation_warnings", "applied_evaluation_run_uid", "note", "updated_at",
            ])
        return {"ok": True, "draft": _serialize_row(draft, active=active)}


def discard_quality_profile_draft(workspace, *, expected_revision) -> dict[str, Any]:
    with transaction.atomic():
        draft = WorkspaceQualityProfileRevision.objects.select_for_update().filter(
            workspace=workspace,
            status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
        ).first()
        current_revision = draft.revision if draft else 0
        if draft is None or expected_revision != current_revision:
            raise RetrievalProfileError(
                "PROFILE_REVISION_CONFLICT", "The draft was changed by another request.", 409,
                {"expected_revision": expected_revision, "current_revision": current_revision},
            )
        draft.delete()
    return {"ok": True, "draft": None}


def preview_prompt_draft(workspace, *, expected_revision, route) -> dict[str, Any]:
    from document_ai.rag.prompt_profiles import build_system_prompt
    import hashlib

    if route not in PROMPT_ROUTES:
        raise RetrievalProfileError("INVALID_REQUEST", "route must be document_rag or no_retrieval.", 400)
    draft = WorkspaceQualityProfileRevision.objects.filter(
        workspace=workspace,
        status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
    ).first()
    current_revision = draft.revision if draft else 0
    if draft is None or expected_revision != current_revision:
        raise RetrievalProfileError(
            "PROFILE_REVISION_CONFLICT", "The draft was changed by another request.", 409,
            {"expected_revision": expected_revision, "current_revision": current_revision},
        )
    assembled = build_system_prompt(route=route, language="ko", policy=_effective_prompt_policy(draft.prompt_policy))
    return {
        "ok": True,
        "route": route,
        "assembled_prompt": assembled,
        "sha256": hashlib.sha256(assembled.encode("utf-8")).hexdigest(),
        "character_count": len(assembled),
        "server_prompt_contract_version": PROMPT_CONTRACT_VERSION,
    }


def apply_quality_profile_draft(
    workspace, *, actor, expected_revision, evaluation_run_uid, allow_unverified, note,
) -> dict[str, Any]:
    if not isinstance(allow_unverified, bool):
        raise RetrievalProfileError("INVALID_REQUEST", "allow_unverified must be a boolean.", 400)
    if not isinstance(note, str) or not note.strip() or len(note) > 500:
        raise RetrievalProfileError("INVALID_REQUEST", "A note of at most 500 characters is required.", 400)

    with transaction.atomic():
        from workspaces.models import Workspace

        locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
        active = _active_for_workspace(locked_workspace, actor=actor, lock=True)
        draft = WorkspaceQualityProfileRevision.objects.select_for_update().filter(
            workspace=locked_workspace,
            status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
        ).first()
        current_revision = draft.revision if draft else 0
        if draft is None or expected_revision != current_revision:
            raise RetrievalProfileError(
                "PROFILE_REVISION_CONFLICT", "The draft was changed by another request.", 409,
                {"expected_revision": expected_revision, "current_revision": current_revision},
            )

        validate_retrieval_config(_effective(draft.retrieval_config))
        validate_generation_config(_effective_generation(draft.generation_config))
        validate_prompt_policy(_effective_prompt_policy(draft.prompt_policy))

        changed_axes = set(draft.changed_axes or [])
        retrieval_only = changed_axes == {AXIS_RETRIEVAL}

        if evaluation_run_uid and AXIS_RETRIEVAL not in changed_axes:
            raise RetrievalProfileError(
                "EVALUATION_REQUIRED", "This draft has no retrieval changes to verify.", 409,
            )

        if retrieval_only:
            if not evaluation_run_uid and not allow_unverified:
                raise RetrievalProfileError("EVALUATION_REQUIRED", "A successful evaluation run is required before apply.", 409)
        elif not allow_unverified:
            raise RetrievalProfileError("EVALUATION_REQUIRED", "A successful evaluation run is required before apply.", 409)

        if evaluation_run_uid:
            from document_ai.search.evaluation import resolve_verified_evaluation_run

            run = resolve_verified_evaluation_run(
                locked_workspace, axis=AXIS_RETRIEVAL, draft=draft, evaluation_run_uid=evaluation_run_uid,
            )
            draft.applied_evaluation_run_uid = run.uid
            validation_state = "verified" if retrieval_only else "unverified"
        else:
            validation_state = "unverified"

        active.status = WorkspaceQualityProfileRevision.STATUS_ARCHIVED
        active.save(update_fields=["status", "updated_at"])
        draft.status = WorkspaceQualityProfileRevision.STATUS_ACTIVE
        draft.validation_state = validation_state
        draft.note = note.strip()
        draft.applied_at = timezone.now()
        draft.save(update_fields=["applied_evaluation_run_uid", "status", "validation_state", "note", "applied_at", "updated_at"])
    return quality_profile_envelope(workspace, actor=actor)


def list_quality_profile_versions(workspace, *, page: int = 1, limit: int = 20) -> dict[str, Any]:
    queryset = WorkspaceQualityProfileRevision.objects.filter(
        workspace=workspace,
    ).exclude(status=WorkspaceQualityProfileRevision.STATUS_DRAFT).select_related("created_by")
    start = max(page, 1) - 1
    start *= limit
    results = []
    for revision in queryset[start:start + limit]:
        creator = revision.created_by
        results.append({
            "uid": str(revision.uid),
            "version": revision.version,
            "changed_axes": list(revision.changed_axes or []),
            "validation": {"state": revision.validation_state},
            "note": revision.note,
            "applied_at": revision.applied_at.isoformat() if revision.applied_at else None,
            "created_by": ({"id": creator.id, "display_name": creator.display_name} if creator else None),
        })
    return {"ok": True, "results": results}
