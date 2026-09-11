from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from workspaces.models import (
    Workspace,
    WorkspaceInviteCode,
    WorkspaceInvitation,
    WorkspaceMembership,
    invite_token_digest,
)


def require_admin(*, workspace, actor) -> WorkspaceMembership:
    membership = WorkspaceMembership.objects.filter(
        workspace=workspace,
        user=actor,
        status=WorkspaceMembership.STATUS_ACTIVE,
    ).first()
    if not membership or membership.role != WorkspaceMembership.ROLE_ADMIN:
        raise PermissionDenied("Workspace admin access is required.")
    return membership


def create_workspace(*, actor, name: str, kind: str = Workspace.KIND_TEAM) -> Workspace:
    if kind not in (Workspace.KIND_PERSONAL, Workspace.KIND_TEAM):
        raise ValidationError("Invalid workspace kind.")
    with transaction.atomic():
        workspace = Workspace.objects.create(
            name=name.strip(),
            kind=kind,
            created_by=actor,
        )
        WorkspaceMembership.objects.create(
            workspace=workspace,
            user=actor,
            role=WorkspaceMembership.ROLE_ADMIN,
            status=WorkspaceMembership.STATUS_ACTIVE,
        )
        return workspace


def create_team_workspace(*, actor, name: str) -> Workspace:
    return create_workspace(actor=actor, name=name, kind=Workspace.KIND_TEAM)


def issue_invite_code(*, workspace, actor, max_uses=None, expires_at=None):
    require_admin(workspace=workspace, actor=actor)
    return WorkspaceInviteCode.issue(
        workspace=workspace,
        created_by=actor,
        max_uses=max_uses,
        expires_at=expires_at,
    )


@transaction.atomic
def invite_existing_user(*, workspace, actor, invitee) -> WorkspaceInvitation:
    require_admin(workspace=workspace, actor=actor)
    if WorkspaceMembership.objects.filter(
        workspace=workspace,
        user=invitee,
        status=WorkspaceMembership.STATUS_ACTIVE,
    ).exists():
        raise ValidationError("The user is already a workspace member.")
    existing = WorkspaceInvitation.objects.select_for_update().filter(
        workspace=workspace,
        invitee=invitee,
        status=WorkspaceInvitation.STATUS_PENDING,
    ).first()
    if existing:
        return existing
    return WorkspaceInvitation.objects.create(
        workspace=workspace,
        invitee=invitee,
        invited_by=actor,
    )


@transaction.atomic
def respond_to_invitation(*, invitation_id: int, actor, accept: bool):
    invitation = WorkspaceInvitation.objects.select_for_update().get(
        pk=invitation_id,
        invitee=actor,
        status=WorkspaceInvitation.STATUS_PENDING,
    )
    invitation.status = (
        WorkspaceInvitation.STATUS_ACCEPTED if accept else WorkspaceInvitation.STATUS_DECLINED
    )
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=["status", "responded_at"])
    membership = None
    if accept:
        membership, _ = WorkspaceMembership.objects.get_or_create(
            workspace=invitation.workspace,
            user=actor,
            defaults={
                "role": WorkspaceMembership.ROLE_MEMBER,
                "status": WorkspaceMembership.STATUS_ACTIVE,
                "invited_by": invitation.invited_by,
            },
        )
        if membership.status != WorkspaceMembership.STATUS_ACTIVE:
            membership.status = WorkspaceMembership.STATUS_ACTIVE
            membership.role = WorkspaceMembership.ROLE_MEMBER
            membership.invited_by = invitation.invited_by
            membership.save(update_fields=["status", "role", "invited_by", "updated_at"])
    return invitation, membership


@transaction.atomic
def redeem_invite_code(*, actor, token: str) -> WorkspaceMembership:
    digest = invite_token_digest((token or "").strip())
    invite = (
        WorkspaceInviteCode.objects.select_for_update()
        .select_related("workspace")
        .filter(code_digest=digest)
        .first()
    )
    if not invite or not invite.is_redeemable():
        raise ValidationError("The invite code is invalid or expired.")
    membership, created = WorkspaceMembership.objects.get_or_create(
        workspace=invite.workspace,
        user=actor,
        defaults={
            "role": WorkspaceMembership.ROLE_MEMBER,
            "status": WorkspaceMembership.STATUS_ACTIVE,
            "invited_by": invite.created_by,
        },
    )
    if not created and membership.status != WorkspaceMembership.STATUS_ACTIVE:
        membership.status = WorkspaceMembership.STATUS_ACTIVE
        membership.role = WorkspaceMembership.ROLE_MEMBER
        membership.invited_by = invite.created_by
        membership.save(update_fields=["status", "role", "invited_by", "updated_at"])
    invite.use_count += 1
    if invite.max_uses is not None and invite.use_count >= invite.max_uses:
        invite.is_active = False
    invite.save(update_fields=["use_count", "is_active"])
    return membership


@transaction.atomic
def change_member_role(*, workspace, actor, member, role: str) -> WorkspaceMembership:
    require_admin(workspace=workspace, actor=actor)
    if role not in (WorkspaceMembership.ROLE_ADMIN, WorkspaceMembership.ROLE_MEMBER):
        raise ValidationError("Invalid role.")
    membership = WorkspaceMembership.objects.select_for_update().get(
        workspace=workspace,
        user=member,
        status=WorkspaceMembership.STATUS_ACTIVE,
    )
    if membership.role == WorkspaceMembership.ROLE_ADMIN and role == WorkspaceMembership.ROLE_MEMBER:
        other_admins = WorkspaceMembership.objects.filter(
            workspace=workspace,
            status=WorkspaceMembership.STATUS_ACTIVE,
            role=WorkspaceMembership.ROLE_ADMIN,
        ).exclude(pk=membership.pk)
        if not other_admins.exists():
            raise ValidationError("The last workspace admin cannot be demoted.")
    membership.role = role
    membership.save(update_fields=["role", "updated_at"])
    return membership


@transaction.atomic
def remove_team_member(*, workspace, actor, member) -> None:
    """Remove an active member without changing workspace-owned storage."""
    require_admin(workspace=workspace, actor=actor)
    membership = WorkspaceMembership.objects.select_for_update().get(
        workspace=workspace,
        user=member,
        status=WorkspaceMembership.STATUS_ACTIVE,
    )
    if membership.role == WorkspaceMembership.ROLE_ADMIN:
        other_admins = WorkspaceMembership.objects.filter(
            workspace=workspace,
            status=WorkspaceMembership.STATUS_ACTIVE,
            role=WorkspaceMembership.ROLE_ADMIN,
        ).exclude(pk=membership.pk)
        if not other_admins.exists():
            raise ValidationError("The last workspace admin cannot be removed.")

    membership.delete()


@transaction.atomic
def delete_account_with_personal_data(*, user) -> None:
    """Delete an account, cleaning up any workspace the user was the sole member of.

    Files/quota belong to the workspace (Node.workspace, UserStorage.workspace), not
    the account, so they are unaffected by account deletion. The only thing worth
    guarding against is leaving a shared workspace with no admin left to manage it.
    """
    memberships = (
        WorkspaceMembership.objects.select_for_update()
        .filter(user=user, status=WorkspaceMembership.STATUS_ACTIVE)
        .select_related("workspace")
    )
    solo_workspace_ids = []
    for membership in memberships:
        other_active = WorkspaceMembership.objects.filter(
            workspace=membership.workspace,
            status=WorkspaceMembership.STATUS_ACTIVE,
        ).exclude(pk=membership.pk)
        if not other_active.exists():
            solo_workspace_ids.append(membership.workspace_id)
            continue
        if membership.role == WorkspaceMembership.ROLE_ADMIN:
            if not other_active.filter(role=WorkspaceMembership.ROLE_ADMIN).exists():
                raise ValidationError(
                    f"Assign another admin in '{membership.workspace.name}' before deleting the account."
                )
    Workspace.objects.filter(id__in=solo_workspace_ids).delete()
    user.delete()
