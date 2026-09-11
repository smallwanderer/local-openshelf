from django.contrib import admin

from .models import Workspace, WorkspaceInvitation, WorkspaceInviteCode, WorkspaceMembership


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "created_by", "member_count", "created_at")
    list_filter = ("kind",)
    search_fields = ("name", "uid", "created_by__email")
    readonly_fields = ("uid", "created_at", "updated_at")

    def member_count(self, obj):
        return obj.memberships.filter(status=WorkspaceMembership.STATUS_ACTIVE).count()
    member_count.short_description = "Members"


@admin.register(WorkspaceMembership)
class WorkspaceMembershipAdmin(admin.ModelAdmin):
    list_display = ("workspace", "user", "role", "status", "created_at")
    list_filter = ("role", "status")
    search_fields = ("workspace__name", "user__email")
    readonly_fields = ("created_at", "updated_at")


@admin.register(WorkspaceInviteCode)
class WorkspaceInviteCodeAdmin(admin.ModelAdmin):
    list_display = ("code_prefix", "workspace", "is_active", "use_count", "max_uses", "expires_at", "created_at")
    list_filter = ("is_active",)
    search_fields = ("code_prefix", "workspace__name")
    readonly_fields = ("code_digest", "code_prefix", "created_at")


@admin.register(WorkspaceInvitation)
class WorkspaceInvitationAdmin(admin.ModelAdmin):
    list_display = ("workspace", "invitee", "invited_by", "status", "created_at", "responded_at")
    list_filter = ("status",)
    search_fields = ("workspace__name", "invitee__email", "invited_by__email")
    readonly_fields = ("created_at", "responded_at")
