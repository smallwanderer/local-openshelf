import json
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods

from accounts.api_responses import api_error_response
from accounts.decorators import email_verification_required
from workspaces import services
from workspaces.decorators import workspace_admin_required, workspace_member_required
from workspaces.models import Workspace, WorkspaceInvitation, WorkspaceMembership

User = get_user_model()


def _json_object(request):
    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _validation_error_response(exc: ValidationError):
    return api_error_response("INVALID_REQUEST", "; ".join(exc.messages), status=400)


def _retrieval_profile_error_response(exc):
    return api_error_response(
        exc.code,
        exc.message,
        status=exc.status,
        details=exc.details or {},
    )


def _serialize_workspace(workspace, membership=None):
    data = {
        "uid": str(workspace.uid),
        "name": workspace.name,
        "kind": workspace.kind,
    }
    if membership is not None:
        data["role"] = membership.role
    return data


def _serialize_member(membership):
    return {
        "user_id": membership.user_id,
        "email": membership.user.email,
        "display_name": membership.user.display_name,
        "role": membership.role,
        "status": membership.status,
        "joined_at": membership.created_at.isoformat(),
    }


def _serialize_invitation(invitation):
    return {
        "id": invitation.id,
        "workspace": _serialize_workspace(invitation.workspace),
        "invited_by": invitation.invited_by.email if invitation.invited_by else None,
        "status": invitation.status,
        "created_at": invitation.created_at.isoformat(),
    }


@workspace_member_required
@email_verification_required
@require_http_methods(["GET", "POST"])
def workspace_collection(request):
    if request.method == "GET":
        memberships = (
            WorkspaceMembership.objects.filter(
                user=request.user,
                status=WorkspaceMembership.STATUS_ACTIVE,
            )
            .select_related("workspace")
            .order_by("workspace__kind", "workspace__name")
        )
        return JsonResponse({
            "ok": True,
            "workspaces": [_serialize_workspace(m.workspace, m) for m in memberships],
            "active_workspace_uid": str(request.workspace.uid),
        })

    payload = _json_object(request)
    if payload is None:
        return api_error_response("INVALID_REQUEST", "Expected a JSON object.", status=400)
    name = str(payload.get("name") or "").strip()
    if not name:
        return api_error_response(
            "INVALID_REQUEST",
            "A workspace name is required.",
            status=400,
            details={"name": ["This field is required."]},
        )
    kind = str(payload.get("kind") or Workspace.KIND_TEAM).strip()
    if kind not in (Workspace.KIND_PERSONAL, Workspace.KIND_TEAM):
        return api_error_response(
            "INVALID_REQUEST",
            "kind must be personal or team.",
            status=400,
            details={"kind": ["Must be 'personal' or 'team'."]},
        )
    workspace = services.create_workspace(actor=request.user, name=name, kind=kind)
    request.session["active_workspace_id"] = workspace.id
    membership = workspace.memberships.get(user=request.user)
    return JsonResponse({"ok": True, "workspace": _serialize_workspace(workspace, membership)}, status=201)


@workspace_member_required
@email_verification_required
@require_http_methods(["GET"])
def current_workspace(request):
    return JsonResponse({
        "ok": True,
        "workspace": _serialize_workspace(request.workspace, request.workspace_membership),
    })


@workspace_member_required
@email_verification_required
@require_http_methods(["POST"])
def switch_workspace(request):
    payload = _json_object(request)
    if payload is None:
        return api_error_response("INVALID_REQUEST", "Expected a JSON object.", status=400)
    raw_uid = str(payload.get("workspace_uid") or "").strip()
    try:
        workspace_uid = uuid.UUID(raw_uid)
    except ValueError:
        return api_error_response("INVALID_REQUEST", "A valid workspace_uid is required.", status=400)

    membership = (
        WorkspaceMembership.objects.filter(
            workspace__uid=workspace_uid,
            user=request.user,
            status=WorkspaceMembership.STATUS_ACTIVE,
        )
        .select_related("workspace")
        .first()
    )
    if membership is None:
        return api_error_response("NOT_FOUND", "No such workspace membership.", status=404)

    request.session["active_workspace_id"] = membership.workspace_id
    return JsonResponse({"ok": True, "workspace": _serialize_workspace(membership.workspace, membership)})


@workspace_member_required
@email_verification_required
@require_http_methods(["GET"])
def member_list(request):
    memberships = (
        WorkspaceMembership.objects.filter(
            workspace=request.workspace,
            status=WorkspaceMembership.STATUS_ACTIVE,
        )
        .select_related("user")
        .order_by("-role", "user__email")
    )
    return JsonResponse({"ok": True, "members": [_serialize_member(m) for m in memberships]})


@workspace_admin_required
@email_verification_required
@require_http_methods(["PATCH", "DELETE"])
def member_detail(request, user_id):
    member = get_object_or_404(User, pk=user_id)

    if request.method == "DELETE":
        try:
            services.remove_team_member(workspace=request.workspace, actor=request.user, member=member)
        except ValidationError as exc:
            return _validation_error_response(exc)
        except WorkspaceMembership.DoesNotExist:
            return api_error_response("NOT_FOUND", "No such active membership.", status=404)
        return JsonResponse({"ok": True})

    payload = _json_object(request) or {}
    role = str(payload.get("role") or "").strip()
    if role not in (WorkspaceMembership.ROLE_ADMIN, WorkspaceMembership.ROLE_MEMBER):
        return api_error_response("INVALID_REQUEST", "role must be admin or member.", status=400)
    try:
        membership = services.change_member_role(
            workspace=request.workspace, actor=request.user, member=member, role=role,
        )
    except ValidationError as exc:
        return _validation_error_response(exc)
    except WorkspaceMembership.DoesNotExist:
        return api_error_response("NOT_FOUND", "No such active membership.", status=404)
    return JsonResponse({"ok": True, "member": _serialize_member(membership)})


@workspace_admin_required
@email_verification_required
@require_http_methods(["POST"])
def issue_invite_code(request):
    payload = _json_object(request) or {}
    raw_max_uses = payload.get("max_uses")
    try:
        max_uses = int(raw_max_uses) if raw_max_uses is not None else None
    except (TypeError, ValueError):
        return api_error_response("INVALID_REQUEST", "max_uses must be an integer.", status=400)

    try:
        invite, token = services.issue_invite_code(
            workspace=request.workspace, actor=request.user, max_uses=max_uses,
        )
    except ValueError as exc:
        return api_error_response("INVALID_REQUEST", str(exc), status=400)
    except PermissionDenied as exc:
        return api_error_response("PERMISSION_DENIED", str(exc), status=403)

    return JsonResponse({
        "ok": True,
        "code": token,
        "max_uses": invite.max_uses,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
    }, status=201)


@workspace_member_required
@email_verification_required
@require_http_methods(["POST"])
def redeem_invite_code(request):
    payload = _json_object(request)
    token = str((payload or {}).get("code") or "").strip()
    if not token:
        return api_error_response("INVALID_REQUEST", "code is required.", status=400)
    try:
        membership = services.redeem_invite_code(actor=request.user, token=token)
    except ValidationError as exc:
        return _validation_error_response(exc)
    request.session["active_workspace_id"] = membership.workspace_id
    return JsonResponse({"ok": True, "workspace": _serialize_workspace(membership.workspace, membership)})


@workspace_admin_required
@email_verification_required
@require_http_methods(["POST"])
def create_invite(request):
    payload = _json_object(request)
    email = str((payload or {}).get("email") or "").strip().lower()
    if not email:
        return api_error_response("INVALID_REQUEST", "email is required.", status=400)
    invitee = User.objects.filter(email__iexact=email).first()
    if invitee is None:
        return api_error_response("NOT_FOUND", "No account with that email exists.", status=404)
    try:
        invitation = services.invite_existing_user(workspace=request.workspace, actor=request.user, invitee=invitee)
    except ValidationError as exc:
        return _validation_error_response(exc)
    return JsonResponse({"ok": True, "invitation": _serialize_invitation(invitation)}, status=201)


@workspace_member_required
@email_verification_required
@require_http_methods(["GET"])
def invite_inbox(request):
    invitations = (
        WorkspaceInvitation.objects.filter(
            invitee=request.user, status=WorkspaceInvitation.STATUS_PENDING,
        )
        .select_related("workspace", "invited_by")
        .order_by("-created_at")
    )
    return JsonResponse({"ok": True, "invitations": [_serialize_invitation(i) for i in invitations]})


def _respond_invite(request, invitation_id, *, accept):
    try:
        invitation, membership = services.respond_to_invitation(
            invitation_id=invitation_id, actor=request.user, accept=accept,
        )
    except WorkspaceInvitation.DoesNotExist:
        return api_error_response("NOT_FOUND", "No such pending invitation.", status=404)
    return JsonResponse({
        "ok": True,
        "invitation": _serialize_invitation(invitation),
        "workspace": _serialize_workspace(membership.workspace, membership) if membership else None,
    })


@workspace_member_required
@email_verification_required
@require_http_methods(["POST"])
def accept_invite(request, invitation_id):
    return _respond_invite(request, invitation_id, accept=True)


@workspace_member_required
@email_verification_required
@require_http_methods(["POST"])
def decline_invite(request, invitation_id):
    return _respond_invite(request, invitation_id, accept=False)


@workspace_member_required
@email_verification_required
@require_http_methods(["GET"])
def quality_profile(request):
    from document_ai.search.quality_profile import quality_profile_envelope

    can_edit = request.workspace_membership.role == WorkspaceMembership.ROLE_ADMIN
    return JsonResponse(
        quality_profile_envelope(
            request.workspace,
            actor=request.user,
            can_edit=can_edit,
        )
    )


@workspace_admin_required
@email_verification_required
@require_http_methods(["PATCH"])
def quality_profile_draft(request):
    from document_ai.search.profiles import RetrievalProfileError
    from document_ai.search.quality_profile import save_quality_profile_draft

    payload = _json_object(request)
    if payload is None:
        return api_error_response("INVALID_REQUEST", "Expected a JSON object.", status=400)
    try:
        result = save_quality_profile_draft(
            request.workspace,
            actor=request.user,
            expected_revision=payload.get("expected_revision"),
            retrieval=payload.get("retrieval"),
            generation=payload.get("generation"),
            prompt_policy=payload.get("prompt_policy"),
            note=payload.get("note", ""),
        )
    except RetrievalProfileError as exc:
        return _retrieval_profile_error_response(exc)
    return JsonResponse(result)


@workspace_admin_required
@email_verification_required
@require_http_methods(["POST"])
def quality_profile_draft_discard(request):
    from document_ai.search.profiles import RetrievalProfileError
    from document_ai.search.quality_profile import discard_quality_profile_draft

    payload = _json_object(request)
    if payload is None:
        return api_error_response("INVALID_REQUEST", "Expected a JSON object.", status=400)
    try:
        result = discard_quality_profile_draft(
            request.workspace,
            expected_revision=payload.get("expected_revision"),
        )
    except RetrievalProfileError as exc:
        return _retrieval_profile_error_response(exc)
    return JsonResponse(result)


@workspace_admin_required
@email_verification_required
@require_http_methods(["POST"])
def quality_profile_draft_preview_prompt(request):
    from document_ai.search.profiles import RetrievalProfileError
    from document_ai.search.quality_profile import preview_prompt_draft

    payload = _json_object(request)
    if payload is None:
        return api_error_response("INVALID_REQUEST", "Expected a JSON object.", status=400)
    try:
        result = preview_prompt_draft(
            request.workspace,
            expected_revision=payload.get("expected_revision"),
            route=payload.get("route"),
        )
    except RetrievalProfileError as exc:
        return _retrieval_profile_error_response(exc)
    return JsonResponse(result)


@workspace_admin_required
@email_verification_required
@require_http_methods(["POST"])
def quality_profile_apply(request):
    from document_ai.search.profiles import RetrievalProfileError
    from document_ai.search.quality_profile import apply_quality_profile_draft

    payload = _json_object(request)
    if payload is None:
        return api_error_response("INVALID_REQUEST", "Expected a JSON object.", status=400)
    try:
        result = apply_quality_profile_draft(
            request.workspace,
            actor=request.user,
            expected_revision=payload.get("expected_revision"),
            evaluation_run_uid=payload.get("evaluation_run_uid"),
            allow_unverified=payload.get("allow_unverified", False),
            note=payload.get("note", ""),
        )
    except RetrievalProfileError as exc:
        return _retrieval_profile_error_response(exc)
    return JsonResponse(result)


@workspace_member_required
@email_verification_required
@require_http_methods(["GET", "POST"])
def evaluation_datasets(request):
    from document_ai.search.evaluation import create_evaluation_dataset, list_evaluation_datasets
    from document_ai.search.profiles import RetrievalProfileError

    if request.method == "GET":
        axis = request.GET.get("axis", "retrieval")
        return JsonResponse({"ok": True, "axis": axis, "datasets": list_evaluation_datasets(request.workspace, axis=axis)})

    if request.workspace_membership.role != WorkspaceMembership.ROLE_ADMIN:
        return api_error_response("PERMISSION_DENIED", "Only workspace admins can add evaluation datasets.", status=403)
    payload = _json_object(request)
    if payload is None:
        return api_error_response("INVALID_REQUEST", "Expected a JSON object.", status=400)
    try:
        result = create_evaluation_dataset(
            request.workspace,
            actor=request.user,
            axis=payload.get("axis", "retrieval"),
            name=payload.get("name", ""),
            items=payload.get("items", []),
        )
    except RetrievalProfileError as exc:
        return _retrieval_profile_error_response(exc)
    return JsonResponse(result, status=201)


@workspace_member_required
@email_verification_required
@require_http_methods(["GET"])
def evaluation_run_detail(request, run_uid):
    from document_ai.search.evaluation import get_evaluation_run
    from document_ai.search.profiles import RetrievalProfileError

    try:
        result = get_evaluation_run(request.workspace, run_uid=run_uid)
    except RetrievalProfileError as exc:
        return _retrieval_profile_error_response(exc)
    return JsonResponse(result)


@workspace_admin_required
@email_verification_required
@require_http_methods(["POST"])
def quality_profile_evaluate(request):
    from document_ai.search.evaluation import start_retrieval_evaluation
    from document_ai.search.profiles import RetrievalProfileError

    payload = _json_object(request)
    if payload is None:
        return api_error_response("INVALID_REQUEST", "Expected a JSON object.", status=400)
    try:
        result = start_retrieval_evaluation(
            request.workspace,
            actor=request.user,
            expected_revision=payload.get("expected_revision"),
            dataset_uid=payload.get("dataset_uid"),
        )
    except RetrievalProfileError as exc:
        return _retrieval_profile_error_response(exc)
    return JsonResponse(result, status=202)


def _serialize_llm_settings(workspace, *, can_edit):
    from document_ai.services.llm_endpoint_service import (
        get_effective_rag_target,
        get_or_create_llm_preference,
        get_server_rag_default_model,
    )

    preference = get_or_create_llm_preference(workspace)
    endpoints = workspace.llm_endpoints.filter(is_active=True).order_by("name")
    return {
        "ok": True,
        "endpoints": [
            {
                "id": endpoint.id,
                "name": endpoint.name,
                "endpoint_type": endpoint.endpoint_type,
                "endpoint_type_label": endpoint.get_endpoint_type_display(),
                "default_model": endpoint.default_model,
            }
            for endpoint in endpoints
        ],
        "active": get_effective_rag_target(preference),
        "selected_endpoint_id": preference.rag_endpoint_id,
        "server_default_model": get_server_rag_default_model(),
        "can_edit": can_edit,
    }


@workspace_member_required
@email_verification_required
@require_http_methods(["GET"])
def llm_settings(request):
    can_edit = request.workspace_membership.role == WorkspaceMembership.ROLE_ADMIN
    return JsonResponse(_serialize_llm_settings(request.workspace, can_edit=can_edit))


@workspace_admin_required
@email_verification_required
@require_http_methods(["POST"])
def llm_settings_select(request):
    from document_ai.services.llm_endpoint_service import set_user_rag_model

    payload = _json_object(request)
    if payload is None:
        return api_error_response("INVALID_REQUEST", "Expected a JSON object.", status=400)
    endpoint_id = payload.get("endpoint_id")
    if endpoint_id is not None:
        try:
            endpoint_id = int(endpoint_id)
        except (TypeError, ValueError):
            return api_error_response("INVALID_REQUEST", "endpoint_id must be an integer or null.", status=400)
    set_user_rag_model(workspace=request.workspace, owner=request.user, endpoint_id=endpoint_id, rag_model="")
    return JsonResponse(_serialize_llm_settings(request.workspace, can_edit=True))


@workspace_member_required
@email_verification_required
@require_http_methods(["GET"])
def quality_profile_versions(request):
    from document_ai.search.quality_profile import list_quality_profile_versions

    try:
        page = int(request.GET.get("page", 1))
        limit = min(int(request.GET.get("limit", 20)), 100)
    except ValueError:
        return api_error_response("INVALID_REQUEST", "page and limit must be integers.", status=400)
    return JsonResponse(list_quality_profile_versions(request.workspace, page=page, limit=limit))
