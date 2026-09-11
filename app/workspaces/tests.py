import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.test import TestCase
from unittest.mock import patch

from files.models import FileBlob, Node, NodeType, UserStorage
from files.services.access import can_modify_subtree
from files.services.storage import validate_upload
from document_ai.search.execution import search_documents_sync
from workspaces.models import Workspace, WorkspaceInvitation, WorkspaceInviteCode, WorkspaceMembership
from workspaces.services import (
    change_member_role,
    create_team_workspace,
    delete_account_with_personal_data,
    invite_existing_user,
    issue_invite_code,
    redeem_invite_code,
    remove_team_member,
    respond_to_invitation,
)


User = get_user_model()


class WorkspaceP0Tests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="admin@example.com", password="test-pass")
        self.member = User.objects.create_user(email="member@example.com", password="test-pass")
        self.other = User.objects.create_user(email="other@example.com", password="test-pass")
        self.team = create_team_workspace(actor=self.admin, name="Team")

    def add_member(self, user, role=WorkspaceMembership.ROLE_MEMBER):
        return WorkspaceMembership.objects.create(
            workspace=self.team,
            user=user,
            role=role,
            status=WorkspaceMembership.STATUS_ACTIVE,
        )

    def test_invite_token_is_only_returned_once_and_redeems_to_member(self):
        invite, token = issue_invite_code(workspace=self.team, actor=self.admin, max_uses=1)
        self.assertNotEqual(invite.code_digest, token)
        self.assertFalse(WorkspaceInviteCode.objects.filter(code_digest=token).exists())

        membership = redeem_invite_code(actor=self.member, token=token)

        self.assertEqual(membership.workspace, self.team)
        self.assertEqual(membership.role, WorkspaceMembership.ROLE_MEMBER)
        invite.refresh_from_db()
        self.assertEqual(invite.use_count, 1)
        self.assertFalse(invite.is_active)

    def test_personal_workspace_can_issue_invite_code(self):
        """`kind` no longer gates invite/member-management behavior -- it's kept
        only as a label for the auto-provisioned default workspace."""
        personal = self.admin.workspace_memberships.get(workspace__kind=Workspace.KIND_PERSONAL).workspace
        invite, token = WorkspaceInviteCode.issue(workspace=personal, created_by=self.admin)
        self.assertTrue(invite.is_redeemable())

    def test_existing_user_accepts_in_app_invitation(self):
        invitation = invite_existing_user(
            workspace=self.team,
            actor=self.admin,
            invitee=self.member,
        )
        answered, membership = respond_to_invitation(
            invitation_id=invitation.id,
            actor=self.member,
            accept=True,
        )
        self.assertEqual(answered.status, WorkspaceInvitation.STATUS_ACCEPTED)
        self.assertEqual(membership.role, WorkspaceMembership.ROLE_MEMBER)

    def test_team_survives_creator_account_deletion(self):
        team_id = self.team.id
        self.admin.delete()
        team = Workspace.objects.get(pk=team_id)
        self.assertIsNone(team.created_by_id)

    def test_non_admin_folder_owner_cannot_mutate_foreign_descendant(self):
        member_membership = self.add_member(self.member)
        self.add_member(self.other)
        folder = Node.objects.create(
            workspace=self.team,
            owner=self.member,
            name="shared",
            ext="",
            node_type=NodeType.FOLDER,
        )
        Node.objects.create(
            workspace=self.team,
            owner=self.other,
            parent=folder,
            name="other.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )
        self.assertFalse(
            can_modify_subtree(actor=self.member, membership=member_membership, node=folder)
        )

    def test_member_removal_keeps_workspace_storage_unchanged(self):
        self.add_member(self.member)
        node = Node.objects.create(
            workspace=self.team,
            owner=self.member,
            name="charged.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )
        blob = FileBlob.objects.create(
            node=node,
            file="",
            original_name="charged.txt",
            size=128,
        )
        remove_team_member(
            workspace=self.team,
            actor=self.admin,
            member=self.member,
        )
        self.assertTrue(FileBlob.objects.filter(pk=blob.pk).exists())
        self.assertFalse(WorkspaceMembership.objects.filter(workspace=self.team, user=self.member).exists())
        self.assertEqual(UserStorage.objects.get(workspace=self.team).used_size, 128)

    def test_personal_and_team_workspaces_receive_the_same_default_quota(self):
        personal = self.admin.workspace_memberships.get(
            workspace__kind=Workspace.KIND_PERSONAL,
        ).workspace
        self.assertEqual(
            UserStorage.objects.get(workspace=personal).total_size,
            UserStorage.objects.get(workspace=self.team).total_size,
        )

    def test_team_upload_checks_shared_workspace_quota_not_member_account(self):
        self.add_member(self.member)
        storage = UserStorage.objects.get(workspace=self.team)
        storage.total_size = 100
        storage.used_size = 90
        storage.save(update_fields=["total_size", "used_size", "updated_at"])

        result = validate_upload(
            self.team,
            self.member,
            SimpleUploadedFile("quota.txt", b"x" * 20, content_type="text/plain"),
        )

        self.assertFalse(result.ok)
        self.assertIn("Not enough storage", result.errors[0])

    def test_last_admin_cannot_be_removed(self):
        with self.assertRaises(ValidationError):
            remove_team_member(
                workspace=self.team,
                actor=self.admin,
                member=self.admin,
            )

    def test_delete_account_removes_solo_workspaces(self):
        solo_user = User.objects.create_user(email="solo@example.com", password="test-pass")
        personal = solo_user.workspace_memberships.get(workspace__kind=Workspace.KIND_PERSONAL).workspace

        delete_account_with_personal_data(user=solo_user)

        self.assertFalse(User.objects.filter(pk=solo_user.pk).exists())
        self.assertFalse(Workspace.objects.filter(pk=personal.pk).exists())

    def test_delete_account_blocks_sole_admin_of_shared_workspace(self):
        self.add_member(self.member)
        with self.assertRaises(ValidationError):
            delete_account_with_personal_data(user=self.admin)
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_delete_account_leaves_workspace_intact_when_another_admin_exists(self):
        self.add_member(self.member)
        change_member_role(workspace=self.team, actor=self.admin, member=self.member, role=WorkspaceMembership.ROLE_ADMIN)

        delete_account_with_personal_data(user=self.admin)

        self.assertFalse(User.objects.filter(pk=self.admin.pk).exists())
        team = Workspace.objects.get(pk=self.team.pk)
        self.assertIsNone(team.created_by_id)
        self.assertFalse(
            WorkspaceMembership.objects.filter(workspace=self.team, user_id=self.admin.pk).exists()
        )

    @patch("document_ai.search.retriever.VectorRetriever.retrieve", return_value=[])
    def test_search_contract_passes_workspace_to_retriever(self, retrieve):
        self.add_member(self.member)
        search_documents_sync(
            owner=self.member,
            workspace=self.team,
            query="shared policy",
        )
        self.assertEqual(retrieve.call_args.kwargs["workspace"], self.team)


class WorkspaceApiV1Tests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin@example.com", password="test-pass", is_active=True, email_verified=True,
        )
        self.member = User.objects.create_user(
            email="member@example.com", password="test-pass", is_active=True, email_verified=True,
        )
        self.outsider = User.objects.create_user(
            email="outsider@example.com", password="test-pass", is_active=True, email_verified=True,
        )
        self.team = create_team_workspace(actor=self.admin, name="Team")
        WorkspaceMembership.objects.create(
            workspace=self.team,
            user=self.member,
            role=WorkspaceMembership.ROLE_MEMBER,
            status=WorkspaceMembership.STATUS_ACTIVE,
        )

    def post_json(self, path, data, **kwargs):
        return self.client.post(path, data=json.dumps(data), content_type="application/json", **kwargs)

    def patch_json(self, path, data, **kwargs):
        return self.client.patch(path, data=json.dumps(data), content_type="application/json", **kwargs)

    def login_in_team(self, user):
        """force_login alone leaves the session's active workspace unset, which
        falls back to the user's personal workspace -- not self.team. Team-scoped
        assertions need the session pointed at self.team first."""
        self.client.force_login(user)
        self.post_json("/api/workspaces/v1/switch/", {"workspace_uid": str(self.team.uid)})

    def test_list_workspaces_returns_personal_and_team(self):
        self.client.force_login(self.admin)
        response = self.client.get("/api/workspaces/v1/")
        self.assertEqual(response.status_code, 200)
        kinds = {w["kind"] for w in response.json()["workspaces"]}
        self.assertEqual(kinds, {Workspace.KIND_PERSONAL, Workspace.KIND_TEAM})

    def test_create_team_workspace_switches_active_workspace(self):
        self.client.force_login(self.admin)
        response = self.post_json("/api/workspaces/v1/", {"name": "New Team"})
        self.assertEqual(response.status_code, 201)
        new_uid = response.json()["workspace"]["uid"]

        current = self.client.get("/api/workspaces/v1/current/")
        self.assertEqual(current.json()["workspace"]["uid"], new_uid)
        self.assertEqual(current.json()["workspace"]["role"], WorkspaceMembership.ROLE_ADMIN)

    def test_switch_workspace_requires_active_membership(self):
        self.client.force_login(self.admin)
        other_admin = User.objects.create_user(email="other-admin@example.com", password="test-pass")
        foreign_team = create_team_workspace(actor=other_admin, name="Foreign")

        response = self.post_json("/api/workspaces/v1/switch/", {"workspace_uid": str(foreign_team.uid)})
        self.assertEqual(response.status_code, 404)

        response = self.post_json("/api/workspaces/v1/switch/", {"workspace_uid": str(self.team.uid)})
        self.assertEqual(response.status_code, 200)

    def test_member_can_list_but_not_change_roles(self):
        self.login_in_team(self.member)

        listing = self.client.get("/api/workspaces/v1/current/members/")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.json()["members"]), 2)

        response = self.patch_json(
            f"/api/workspaces/v1/current/members/{self.admin.id}/",
            {"role": WorkspaceMembership.ROLE_MEMBER},
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_change_role_and_remove_member(self):
        self.login_in_team(self.admin)

        response = self.patch_json(
            f"/api/workspaces/v1/current/members/{self.member.id}/",
            {"role": WorkspaceMembership.ROLE_ADMIN},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["member"]["role"], WorkspaceMembership.ROLE_ADMIN)

        response = self.client.delete(f"/api/workspaces/v1/current/members/{self.member.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            WorkspaceMembership.objects.filter(workspace=self.team, user=self.member).exists()
        )

    def test_invite_code_issue_and_redeem_round_trip(self):
        self.login_in_team(self.admin)
        issued = self.post_json("/api/workspaces/v1/current/invite-code/", {"max_uses": 1})
        self.assertEqual(issued.status_code, 201)
        code = issued.json()["code"]

        self.client.force_login(self.outsider)
        redeemed = self.post_json("/api/workspaces/v1/invite-code/redeem/", {"code": code})
        self.assertEqual(redeemed.status_code, 200)
        self.assertEqual(redeemed.json()["workspace"]["uid"], str(self.team.uid))

    def test_member_cannot_issue_invite_code(self):
        self.login_in_team(self.member)
        response = self.post_json("/api/workspaces/v1/current/invite-code/", {})
        self.assertEqual(response.status_code, 403)

    def test_in_app_invite_flow(self):
        self.login_in_team(self.admin)
        created = self.post_json("/api/workspaces/v1/current/invites/", {"email": self.outsider.email})
        self.assertEqual(created.status_code, 201)
        invitation_id = created.json()["invitation"]["id"]

        self.client.force_login(self.outsider)
        inbox = self.client.get("/api/workspaces/v1/invites/inbox/")
        self.assertEqual(len(inbox.json()["invitations"]), 1)

        accepted = self.post_json(f"/api/workspaces/v1/invites/{invitation_id}/accept/", {})
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(
            WorkspaceMembership.objects.filter(workspace=self.team, user=self.outsider).exists()
        )
